from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, datetime
from io import BytesIO

from src.documents import (
    DocumentWorkspace,
    StoredChunk,
    register_document_routes,
)
from src.documents.testing import MemoryStorage
from src.platform import PlatformApp
from src.shared import Principal


NOW = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
OWNER = Principal("owner@example.com", user_id=1, display_name="Owner")
OTHER = Principal("other@example.com", user_id=2, display_name="Other")


def _resolver(request):
    return {
        "Bearer owner": OWNER,
        "Bearer other": OTHER,
    }.get(request.header("authorization"))


def _call(app, method: str, target: str, *, headers=None, body=b""):
    path, _, query = target.partition("?")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": BytesIO(body),
    }
    for name, value in (headers or {}).items():
        key = name.upper().replace("-", "_")
        environ[key if key in {"CONTENT_TYPE", "CONTENT_LENGTH"} else f"HTTP_{key}"] = value
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(response_headers)

    raw = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], raw


def _multipart(
    data: bytes | None,
    *,
    filename: str = "original.txt",
    content_type: str = "text/plain",
    fields: dict[str, str] | None = None,
):
    boundary = "vectorshelf-boundary"
    body = bytearray()
    for name, value in (fields or {}).items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")
    if data is not None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
        body.extend(data)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class DocumentRouteAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.workspace = DocumentWorkspace(self.storage, clock=lambda: NOW)
        self.app = PlatformApp(_resolver, lambda: NOW)
        register_document_routes(self.app, self.workspace)

    def _upload(self, data: bytes = b"document bytes") -> dict[str, object]:
        body, content_type = _multipart(
            data,
            fields={
                "title": "Title",
                "description": "Description",
                "visibility": "PRIVATE",
            },
        )
        status, _, raw = _call(
            self.app,
            "POST",
            "/api/documents",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=body,
        )
        self.assertEqual(201, status)
        return json.loads(raw)["data"]

    def test_AC_DOC_014_multipart_upload_preserves_file_and_detail_contract(self):
        binary = b"\x00first\r\n\xfflast"
        result = self._upload(binary)
        self.assertEqual(
            {
                "documentId",
                "documentVersionId",
                "fileObjectId",
                "embeddingJobId",
                "documentStatus",
                "jobStatus",
            },
            set(result),
        )
        stored = self.workspace.state.files[result["fileObjectId"]]
        self.assertEqual("original.txt", stored.filename)
        self.assertEqual("text/plain", stored.content_type)
        self.assertEqual(binary, self.storage.get(stored.location))

        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{result['documentId']}",
            headers={"Authorization": "Bearer owner"},
        )
        detail = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertEqual(
            {
                "documentId",
                "title",
                "description",
                "documentType",
                "sourceType",
                "status",
                "visibility",
                "ownerUserId",
                "ownerName",
                "currentVersion",
                "contentAvailable",
                "createdAt",
                "updatedAt",
            },
            set(detail),
        )

    def test_AC_DOC_001_missing_file_uses_fixed_upload_error(self):
        body, content_type = _multipart(
            None,
            fields={"title": "Title", "visibility": "PRIVATE"},
        )
        status, _, raw = _call(
            self.app,
            "POST",
            "/api/documents",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=body,
        )
        error = json.loads(raw)
        self.assertEqual((400, "DOCUMENT-FILE-001"), (status, error["code"]))

    def test_AC_DOC_027_AC_DOC_031_version_and_status_routes(self):
        result = self._upload()
        document_id = result["documentId"]
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{document_id}/status",
            headers={"Authorization": "Bearer owner"},
        )
        state = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertIsNone(state["currentVersion"])
        self.assertEqual(
            {"versionNo": 1, "status": "UPLOADED", "jobStatus": "PENDING"},
            state["processingVersion"],
        )

        self.workspace.set_version_state(document_id, "INDEXED")
        body, content_type = _multipart(b"second version")
        status, _, raw = _call(
            self.app,
            "POST",
            f"/api/documents/{document_id}/versions",
            headers={"Authorization": "Bearer owner", "Content-Type": content_type},
            body=body,
        )
        version = json.loads(raw)["data"]
        self.assertEqual(201, status)
        self.assertEqual(2, version["versionNo"])
        self.assertEqual(
            {
                "documentId",
                "documentVersionId",
                "versionNo",
                "embeddingJobId",
                "currentVersionId",
                "documentStatus",
                "versionStatus",
                "jobStatus",
            },
            set(version),
        )

    def test_AC_DOC_030_detail_route_enforces_read_permission(self):
        result = self._upload()
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{result['documentId']}",
            headers={"Authorization": "Bearer other"},
        )
        self.assertEqual((403, "ROLE-002"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_034_content_route_rebuilds_overlapping_chunks(self):
        result = self._upload(b"abcdefgh")
        chunks = (
            StoredChunk(0, 0, 5, "abcde", hashlib.sha256(b"abcde").hexdigest(), 1),
            StoredChunk(1, 3, 8, "defgh", hashlib.sha256(b"defgh").hexdigest(), 1),
        )
        self.workspace.put_chunks(result["documentVersionId"], chunks)
        status, _, raw = _call(
            self.app,
            "GET",
            f"/api/documents/{result['documentId']}/content",
            headers={"Authorization": "Bearer owner"},
        )
        content = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertEqual("abcdefgh", content["content"])
        self.assertEqual(
            {"documentId", "documentVersionId", "versionNo", "content", "chunkCount"},
            set(content),
        )

    def test_AC_DOC_035_AC_DOC_036_file_route_handles_disposition_and_raw_binary(self):
        binary = b"raw\x00file\r\nbytes"
        result = self._upload(binary)
        path = f"/api/documents/{result['documentId']}/file"
        status, headers, raw = _call(
            self.app,
            "GET",
            path,
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((200, binary), (status, raw))
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        self.assertEqual("text/plain", headers["Content-Type"])

        status, headers, raw = _call(
            self.app,
            "GET",
            path + "?disposition=attachment",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((200, binary), (status, raw))
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))

        status, _, raw = _call(
            self.app,
            "GET",
            path + "?disposition=download",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((400, "COMMON-002"), (status, json.loads(raw)["code"]))

        status, _, raw = _call(
            self.app,
            "GET",
            path + "?disposition=",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((400, "COMMON-002"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_039_list_route_parses_and_validates_query_parameters(self):
        first = self._upload(b"first")
        second = self._upload(b"second")
        status, _, raw = _call(
            self.app,
            "GET",
            "/api/documents?status=UPLOADED&page=1&size=1",
            headers={"Authorization": "Bearer owner"},
        )
        page = json.loads(raw)["data"]
        self.assertEqual(200, status)
        self.assertEqual(2, page["totalElements"])
        self.assertEqual(second["documentId"], page["content"][0]["documentId"])
        self.assertNotEqual(first["documentId"], second["documentId"])

        status, _, raw = _call(
            self.app,
            "GET",
            "/api/documents?page=-1",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((400, "COMMON-002"), (status, json.loads(raw)["code"]))

    def test_AC_DOC_040_metadata_visibility_delete_and_default_list_routes(self):
        result = self._upload()
        document_id = result["documentId"]
        auth_json = {
            "Authorization": "Bearer owner",
            "Content-Type": "application/json",
        }
        status, _, raw = _call(
            self.app,
            "PATCH",
            f"/api/documents/{document_id}",
            headers=auth_json,
            body=json.dumps({"title": "Changed", "description": None}).encode(),
        )
        self.assertEqual((204, b""), (status, raw))

        status, _, raw = _call(
            self.app,
            "PATCH",
            f"/api/documents/{document_id}/visibility",
            headers=auth_json,
            body=b'{"visibility":"PUBLIC"}',
        )
        self.assertEqual((204, b""), (status, raw))
        detail = json.loads(
            _call(
                self.app,
                "GET",
                f"/api/documents/{document_id}",
                headers={"Authorization": "Bearer owner"},
            )[2]
        )["data"]
        self.assertEqual(("Changed", None, "PUBLIC"), (
            detail["title"], detail["description"], detail["visibility"]
        ))

        status, _, raw = _call(
            self.app,
            "DELETE",
            f"/api/documents/{document_id}",
            headers={"Authorization": "Bearer owner"},
        )
        self.assertEqual((204, b""), (status, raw))
        page = json.loads(
            _call(
                self.app,
                "GET",
                "/api/documents",
                headers={"Authorization": "Bearer owner"},
            )[2]
        )["data"]
        self.assertEqual([], page["content"])

    def test_AC_SYS_007_document_metadata_reports_first_body_field(self):
        document_id = self._upload()["documentId"]
        status, _, raw = _call(
            self.app,
            "PATCH",
            f"/api/documents/{document_id}",
            headers={
                "Authorization": "Bearer owner",
                "Content-Type": "application/json",
            },
            body=b'{"title":7,"description":8}',
        )

        error = json.loads(raw)
        self.assertEqual((400, "COMMON-002"), (status, error["code"]))
        self.assertEqual("title: 필수 문자열이며 공백일 수 없습니다.", error["message"])
        self.assertNotIn("description", error["message"])


if __name__ == "__main__":
    unittest.main()
