from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID

from src.indexing import IndexingService, register_indexing_routes
from src.platform import PlatformApp
from src.shared import Principal, PublicError, Request


NOW = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)
TOKEN_ONE = UUID("11111111-1111-4111-8111-111111111111")
TOKEN_TWO = UUID("22222222-2222-4222-8222-222222222222")
ADMIN = Principal("admin@example.com", frozenset({"ADMIN"}), user_id=1)
USER = Principal("user@example.com", frozenset({"USER"}), user_id=2)
JOB_KEYS = {
    "jobId",
    "documentId",
    "documentVersionId",
    "status",
    "priority",
    "retryCount",
    "maxRetries",
    "workerId",
    "lockedAt",
    "leaseExpiresAt",
    "firstStartedAt",
    "nextRunAt",
    "completedAt",
    "failedAt",
    "failureType",
    "errorMessage",
}
ATTEMPT_KEYS = {
    "attemptId",
    "jobId",
    "attemptNo",
    "workerId",
    "status",
    "startedAt",
    "endedAt",
    "durationMs",
    "failureType",
    "errorMessage",
}
CLAIM_KEYS = {
    "jobId",
    "workerId",
    "documentVersionId",
    "status",
    "claimToken",
    "lockedAt",
    "leaseExpiresAt",
}


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def _data(response):
    return _payload(response)["data"]


def _request(app, method: str, path: str, body: object | None = None):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if raw else {}
    return app.handle(Request(method, path, headers=headers, body=raw))


class _RouteRecorder:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, object]] = []

    def add_route(self, method: str, path: str, handler, **_options) -> None:
        self.routes.append((method, path, handler))


class IndexingRouteAcceptanceTests(unittest.TestCase):
    def app(self, service, *, chunk_producer=None, embedder=None):
        app = PlatformApp(lambda _request: ADMIN, lambda: NOW)
        register_indexing_routes(
            app,
            service,
            chunk_producer=chunk_producer or (lambda *_claim: ("text",)),
            embedder=embedder or (lambda chunks, _model: ((1.0, 2.0, 3.0),) * len(chunks)),
        )
        return app

    def seed(self, service, *, max_retries: int = 3):
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id, max_retries=max_retries)
        service.add_model(dimension=3)
        worker = service.register_worker("worker-one")
        return document, version, job, worker

    def claim(self, app, worker_id: int):
        response = _request(
            app,
            "POST",
            f"/admin/indexing-jobs/claim?workerId={worker_id}",
        )
        self.assertEqual(200, response.status)
        return _data(response)

    def test_AC_IDX_001_all_thirteen_routes_revalidate_admin(self):
        recorder = _RouteRecorder()
        api = register_indexing_routes(
            recorder,
            IndexingService(clock=lambda: NOW),
            chunk_producer=lambda *_claim: ("text",),
            embedder=lambda chunks, _model: ((1.0, 2.0, 3.0),) * len(chunks),
        )
        expected = {
            ("GET", "/admin/indexing-jobs"),
            ("GET", "/admin/indexing-jobs/{jobId}"),
            ("GET", "/admin/indexing-jobs/{jobId}/attempts"),
            ("GET", "/admin/indexing-jobs/{jobId}/events"),
            ("POST", "/admin/indexing-jobs/claim"),
            ("POST", "/admin/indexing-jobs/{jobId}/lease/renew"),
            ("POST", "/admin/indexing-jobs/{jobId}/attempts"),
            (
                "POST",
                "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/chunks",
            ),
            (
                "POST",
                "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/embeddings",
            ),
            (
                "POST",
                "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/complete",
            ),
            (
                "POST",
                "/admin/indexing-jobs/{jobId}/attempts/{attemptId}/fail",
            ),
            ("POST", "/admin/indexing-jobs/{jobId}/retry"),
            ("GET", "/admin/workers"),
        }
        self.assertEqual(expected, {(method, path) for method, path, _ in recorder.routes})
        self.assertEqual(13, len(recorder.routes))

        for method, path, handler in recorder.routes:
            with self.subTest(method=method, path=path):
                with self.assertRaises(PublicError) as caught:
                    handler(Request(method, path, principal=USER))
                self.assertEqual("ROLE-002", caught.exception.code)
        with self.assertRaises(PublicError) as caught:
            api.workers(Request("GET", "/admin/workers"))
        self.assertEqual("COMMON-007", caught.exception.code)

    def test_AC_IDX_002_path_query_body_and_UUID_token_validation(self):
        app = self.app(IndexingService(clock=lambda: NOW))
        valid = {
            "workerId": 1,
            "claimToken": str(TOKEN_ONE),
        }
        invalid_requests = (
            ("GET", "/admin/indexing-jobs/not-an-id", None),
            ("GET", "/admin/indexing-jobs?status=UNKNOWN", None),
            ("GET", "/admin/indexing-jobs?documentId=-1", None),
            ("GET", "/admin/indexing-jobs?workerId=0", None),
            ("GET", "/admin/indexing-jobs?page=-1", None),
            ("GET", "/admin/indexing-jobs?size=101", None),
            ("POST", "/admin/indexing-jobs/claim", None),
            ("POST", "/admin/indexing-jobs/claim?workerId=no", None),
            ("POST", "/admin/indexing-jobs/1/attempts", None),
            ("POST", "/admin/indexing-jobs/1/attempts", []),
            (
                "POST",
                "/admin/indexing-jobs/1/attempts",
                {"workerId": "1", "claimToken": str(TOKEN_ONE)},
            ),
            (
                "POST",
                "/admin/indexing-jobs/1/attempts",
                {"workerId": 1, "claimToken": "not-a-uuid"},
            ),
            (
                "POST",
                "/admin/indexing-jobs/1/attempts/bad/chunks",
                valid,
            ),
            (
                "POST",
                "/admin/indexing-jobs/1/attempts/1/fail",
                {**valid, "failureType": 7, "errorMessage": "failed"},
            ),
            (
                "POST",
                "/admin/indexing-jobs/1/attempts/1/fail",
                {
                    **valid,
                    "failureType": "WORKER_INTERNAL_ERROR",
                    "errorMessage": "failed",
                    "retryAfter": True,
                },
            ),
        )
        for method, path, body in invalid_requests:
            with self.subTest(method=method, path=path, body=body):
                response = _request(app, method, path, body)
                self.assertEqual((400, "COMMON-002"), (response.status, _payload(response)["code"]))

    def test_AC_SYS_007_indexing_reports_first_ownership_body_field(self):
        app = self.app(IndexingService(clock=lambda: NOW))
        response = _request(
            app,
            "POST",
            "/admin/indexing-jobs/1/attempts",
            {"workerId": 0, "claimToken": "not-a-uuid"},
        )

        body = _payload(response)
        self.assertEqual((400, "COMMON-002"), (response.status, body["code"]))
        self.assertEqual("workerId: 1 이상의 정수여야 합니다.", body["message"])
        self.assertNotIn("claimToken", body["message"])

    def test_AC_IDX_003_AC_IDX_008_claim_204_renew_and_public_projections(self):
        clock = [NOW]
        service = IndexingService(clock=lambda: clock[0], uuid_factory=lambda: TOKEN_ONE)
        worker = service.register_worker("worker-one")
        app = self.app(service)

        empty = _request(
            app,
            "POST",
            f"/admin/indexing-jobs/claim?workerId={worker.id}",
        )
        self.assertEqual((204, b"", ()), (empty.status, empty.body, empty.headers))

        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id)
        claimed = self.claim(app, worker.id)
        UUID(claimed["claimToken"])
        self.assertEqual(job.id, claimed["jobId"])
        self.assertEqual(CLAIM_KEYS, set(claimed))

        service.state.events[-1].metadata.update(
            {"claimToken": claimed["claimToken"], "connection": "internal"}
        )
        detail = _data(_request(app, "GET", f"/admin/indexing-jobs/{job.id}"))
        events = _data(_request(app, "GET", f"/admin/indexing-jobs/{job.id}/events"))
        self.assertEqual(JOB_KEYS, set(detail))
        self.assertEqual(
            {"eventId", "jobId", "eventType", "occurredAt"},
            set(events[0]),
        )

        clock[0] = NOW + timedelta(seconds=1)
        service.heartbeat(worker.id)
        renewed = _request(
            app,
            "POST",
            f"/admin/indexing-jobs/{job.id}/lease/renew",
            {"workerId": worker.id, "claimToken": claimed["claimToken"]},
        )
        self.assertEqual(200, renewed.status)
        self.assertEqual({"jobId", "leaseExpiresAt"}, set(_data(renewed)))

    def test_AC_IDX_020_AC_IDX_021_AC_IDX_022_first_and_replay_statuses(self):
        clock = [NOW]
        chunk_calls = [0]
        embed_calls = [0]

        def chunks(*_claim):
            chunk_calls[0] += 1
            return ("text",)

        def embedder(records, _model):
            embed_calls[0] += 1
            return ((1.0, 2.0, 3.0),) * len(records)

        service = IndexingService(clock=lambda: clock[0], uuid_factory=lambda: TOKEN_ONE)
        _, _, job, worker = self.seed(service)
        app = self.app(service, chunk_producer=chunks, embedder=embedder)
        claim = self.claim(app, worker.id)
        owned = {"workerId": worker.id, "claimToken": claim["claimToken"]}

        attempt_path = f"/admin/indexing-jobs/{job.id}/attempts"
        first_attempt = _request(app, "POST", attempt_path, owned)
        replay_attempt = _request(app, "POST", attempt_path, owned)
        attempt_id = _data(first_attempt)["attemptId"]
        self.assertEqual((201, 200), (first_attempt.status, replay_attempt.status))
        self.assertEqual(ATTEMPT_KEYS, set(_data(first_attempt)))
        self.assertEqual(ATTEMPT_KEYS, set(_data(replay_attempt)))

        clock[0] = NOW + timedelta(seconds=1)
        chunk_path = f"{attempt_path}/{attempt_id}/chunks"
        first_chunks = _request(app, "POST", chunk_path, owned)
        replay_chunks = _request(app, "POST", chunk_path, owned)
        self.assertEqual((201, 200, 1), (first_chunks.status, replay_chunks.status, chunk_calls[0]))
        self.assertEqual({"documentVersionId", "chunkCount"}, set(_data(first_chunks)))

        clock[0] = NOW + timedelta(seconds=2)
        embedding_path = f"{attempt_path}/{attempt_id}/embeddings"
        first_embeddings = _request(app, "POST", embedding_path, owned)
        replay_embeddings = _request(app, "POST", embedding_path, owned)
        self.assertEqual(
            (201, 200, 1),
            (first_embeddings.status, replay_embeddings.status, embed_calls[0]),
        )
        self.assertEqual(
            {"documentVersionId", "embeddingModelId", "embeddingCount"},
            set(_data(first_embeddings)),
        )

    def test_AC_IDX_023_completion_replay_keeps_http_200(self):
        clock = [NOW]
        service = IndexingService(clock=lambda: clock[0], uuid_factory=lambda: TOKEN_ONE)
        _, _, job, worker = self.seed(service)
        app = self.app(service)
        claim = self.claim(app, worker.id)
        owned = {"workerId": worker.id, "claimToken": claim["claimToken"]}
        attempt_path = f"/admin/indexing-jobs/{job.id}/attempts"
        attempt_id = _data(_request(app, "POST", attempt_path, owned))["attemptId"]
        clock[0] = NOW + timedelta(seconds=1)
        _request(app, "POST", f"{attempt_path}/{attempt_id}/chunks", owned)
        clock[0] = NOW + timedelta(seconds=2)
        _request(app, "POST", f"{attempt_path}/{attempt_id}/embeddings", owned)
        clock[0] = NOW + timedelta(seconds=3)
        path = f"{attempt_path}/{attempt_id}/complete"
        first = _request(app, "POST", path, owned)
        clock[0] = NOW + timedelta(seconds=4)
        replay = _request(app, "POST", path, owned)
        self.assertEqual((200, 200), (first.status, replay.status))
        self.assertEqual(_data(first), _data(replay))
        self.assertEqual(
            {"jobId", "attemptId", "documentVersionId", "status", "completedAt"},
            set(_data(first)),
        )

    def test_AC_IDX_025_AC_IDX_060_failure_replay_and_manual_retry_routes(self):
        clock = [NOW]
        service = IndexingService(clock=lambda: clock[0], uuid_factory=lambda: TOKEN_ONE)
        _, _, job, worker = self.seed(service, max_retries=0)
        app = self.app(service)
        claim = self.claim(app, worker.id)
        owned = {"workerId": worker.id, "claimToken": claim["claimToken"]}
        attempt_path = f"/admin/indexing-jobs/{job.id}/attempts"
        attempt_id = _data(_request(app, "POST", attempt_path, owned))["attemptId"]
        failure = {
            **owned,
            "failureType": "DOCUMENT_CONTENT_INVALID",
            "errorMessage": "invalid content",
        }
        path = f"{attempt_path}/{attempt_id}/fail"
        clock[0] = NOW + timedelta(seconds=1)
        first = _request(app, "POST", path, failure)
        replay = _request(app, "POST", path, failure)
        self.assertEqual((200, 200), (first.status, replay.status))
        self.assertEqual(_data(first), _data(replay))
        self.assertEqual(
            {
                "jobId",
                "attemptId",
                "status",
                "retryCount",
                "nextRunAt",
                "failureType",
                "errorMessage",
            },
            set(_data(first)),
        )

        retried = _request(app, "POST", f"/admin/indexing-jobs/{job.id}/retry")
        self.assertEqual(200, retried.status)
        self.assertEqual("PENDING", _data(retried)["status"])
        self.assertEqual(JOB_KEYS, set(_data(retried)))

    def test_AC_IDX_004_job_pages_filters_and_attempts_are_recent_first(self):
        tokens = iter((TOKEN_ONE, TOKEN_TWO))
        clock = [NOW]
        service = IndexingService(
            clock=lambda: clock[0],
            uuid_factory=lambda: next(tokens),
            retry_jitter=0,
        )
        document = service.add_document()
        version = service.add_version(document.id, 1)
        job = service.create_job(version.id, max_retries=1)
        other_version = service.add_version(document.id, 2)
        failed = service.create_job(other_version.id, status="FAILED")
        other_document = service.add_document()
        third_version = service.add_version(other_document.id, 1)
        processing = service.create_job(third_version.id, status="PROCESSING")
        worker = service.register_worker("worker-one")
        processing.worker_id = worker.id
        processing.claim_token = str(TOKEN_TWO)
        app = self.app(service)

        page = _data(_request(app, "GET", "/admin/indexing-jobs?page=1&size=1"))
        filtered = _data(
            _request(
                app,
                "GET",
                f"/admin/indexing-jobs?status=FAILED&documentId={document.id}",
            )
        )
        owned = _data(
            _request(app, "GET", f"/admin/indexing-jobs?workerId={worker.id}")
        )
        self.assertEqual((3, failed.id), (page["totalElements"], page["content"][0]["jobId"]))
        self.assertEqual([failed.id], [row["jobId"] for row in filtered["content"]])
        self.assertEqual([processing.id], [row["jobId"] for row in owned["content"]])
        self.assertEqual(JOB_KEYS, set(owned["content"][0]))

        processing.status = "FAILED"
        processing.worker_id = None
        processing.claim_token = None
        claim = self.claim(app, worker.id)
        first_owned = {"workerId": worker.id, "claimToken": claim["claimToken"]}
        attempt_path = f"/admin/indexing-jobs/{job.id}/attempts"
        _request(app, "POST", attempt_path, first_owned)
        clock[0] = NOW + timedelta(seconds=1)
        _request(
            app,
            "POST",
            f"{attempt_path}/1/fail",
            {
                **first_owned,
                "failureType": "WORKER_INTERNAL_ERROR",
                "errorMessage": "retry",
            },
        )
        clock[0] = NOW + timedelta(seconds=11)
        service.heartbeat(worker.id)
        second_claim = self.claim(app, worker.id)
        second_owned = {
            "workerId": worker.id,
            "claimToken": second_claim["claimToken"],
        }
        _request(app, "POST", attempt_path, second_owned)
        attempts = _data(_request(app, "GET", attempt_path))
        self.assertEqual([2, 1], [row["attemptNo"] for row in attempts])
        self.assertTrue(all(set(row) == ATTEMPT_KEYS for row in attempts))

    def test_AC_IDX_006_workers_route_uses_effective_liveness(self):
        clock = [NOW]
        service = IndexingService(clock=lambda: clock[0])
        worker = service.register_worker("worker-one")
        app = self.app(service)
        clock[0] = NOW + timedelta(seconds=31)
        rows = _data(_request(app, "GET", "/admin/workers"))
        self.assertEqual((worker.id, "DEAD"), (rows[0]["workerId"], rows[0]["status"]))
        self.assertEqual(
            {"workerId", "instanceId", "name", "status", "lastHeartbeat"},
            set(rows[0]),
        )


if __name__ == "__main__":
    unittest.main()
