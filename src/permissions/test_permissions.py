from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from src.permissions import PermissionService
from src.shared import Identifier, Principal, PublicError, ResourceAccess


NOW = datetime(2026, 8, 27, tzinfo=UTC)


class Documents:
    def __init__(self) -> None:
        self.rows: dict[Identifier, ResourceAccess] = {}

    def add(
        self,
        document_id: Identifier,
        owner_user_id: int = 1,
        visibility: str = "PRIVATE",
        status: str = "INDEXED",
    ) -> None:
        self.rows[document_id] = ResourceAccess(
            document_id, owner_user_id, visibility, status
        )

    def document_access(
        self, document_id: Identifier, *, include_deleted: bool = False
    ) -> ResourceAccess | None:
        row = self.rows.get(document_id)
        if row is None or (row.status == "DELETED" and not include_deleted):
            return None
        return row

    def document_ids(self) -> frozenset[Identifier]:
        return frozenset(self.rows)


class Collections:
    def collection_access(self, collection_id, *, include_deleted=False):
        return None

    def collection_ids_for_document(self, document_id):
        return frozenset()

    def document_ids_in_collection(self, collection_id):
        return frozenset()


def principal(
    user_id: int,
    *,
    roles: tuple[str, ...] = ("USER",),
    department_id: int | None = None,
) -> Principal:
    return Principal(
        f"user-{user_id}",
        frozenset(roles),
        user_id=user_id,
        department_id=department_id,
    )


class PermissionAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = Documents()
        for document_id in range(1, 8):
            self.documents.add(document_id)
        self.service = PermissionService(
            self.documents, Collections(), clock=lambda: NOW
        )
        self.owner = principal(1)

    def test_AC_PERM_001_USER_role_target_is_rejected(self):
        with self.assertRaises(PublicError) as raised:
            self.service.grant(
                self.owner,
                "DOCUMENT",
                1,
                "READ",
                target_type="ROLE",
                role_code="USER",
            )
        self.assertEqual("PERMISSION-004", raised.exception.code)

    def test_AC_PERM_002_target_and_identifier_must_match(self):
        invalid = (
            {"target_type": "USER"},
            {"target_type": "USER", "user_id": 2, "role_code": "READER"},
            {"target_type": "DEPARTMENT", "user_id": 2},
            {"target_type": "ROLE", "role_code": "READER", "department_id": 3},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(PublicError) as raised:
                self.service.grant(
                    self.owner, "DOCUMENT", 1, "READ", **values
                )
            self.assertEqual("PERMISSION-001", raised.exception.code)

    def test_AC_PERM_003_expired_direct_permission_remains_listed(self):
        expired = self.service.grant(
            self.owner,
            "DOCUMENT",
            1,
            "READ",
            target_type="USER",
            user_id=2,
            expires_at=NOW - timedelta(seconds=1),
        )
        self.assertEqual((expired,), self.service.list_direct(self.owner, "DOCUMENT", 1))
        effective = self.service.effective_document(principal(2), 1)
        self.assertFalse(effective.can_read)
        self.assertEqual((), effective.sources)

    def test_AC_PERM_004_user_revoke_invalidates_cache_and_live_check_bypasses_it(self):
        user = principal(2)
        permission = self.service.grant(
            self.owner,
            "DOCUMENT",
            1,
            "READ",
            target_type="USER",
            user_id=2,
        )
        self.assertEqual(frozenset({1}), self.service.readable_document_ids(user, [1]))
        self.assertIn(permission.permission_id, self.service._user_cache[(2, 1)])

        self.service.revoke(self.owner, "DOCUMENT", 1, permission.permission_id)
        self.assertNotIn((2, 1), self.service._user_cache)
        self.assertEqual(frozenset(), self.service.readable_document_ids(user, [1]))
        self.assertFalse(self.service.can_read_document(user, 1))

        role_permission = self.service.grant(
            self.owner,
            "DOCUMENT",
            2,
            "READ",
            target_type="ROLE",
            role_code="READER",
        )
        department_permission = self.service.grant(
            self.owner,
            "DOCUMENT",
            3,
            "READ",
            target_type="DEPARTMENT",
            department_id=7,
        )
        self.assertTrue(self.service.can_read_document(principal(3, roles=("READER",)), 2))
        self.assertTrue(self.service.can_read_document(principal(4, department_id=7), 3))
        self.assertFalse(
            any(
                item.permission_id in {role_permission.permission_id, department_permission.permission_id}
                for grants in self.service._user_cache.values()
                for item in grants.values()
            )
        )

        stale = self.service.grant(
            self.owner,
            "DOCUMENT",
            4,
            "READ",
            target_type="USER",
            user_id=2,
        )
        self.assertEqual(frozenset({4}), self.service.readable_document_ids(user, [4]))
        self.service._permissions.pop(stale.permission_id)
        self.assertEqual(frozenset({4}), self.service.readable_document_ids(user, [4]))
        self.assertFalse(self.service.can_read_document(user, 4))

        self.documents.add(5, owner_user_id=2, visibility="PUBLIC", status="DELETED")
        self.assertFalse(self.service.can_read_document(user, 5))
        with self.assertRaises(PublicError) as deleted:
            self.service.effective_document(user, 5)
        self.assertEqual("DOCUMENT-001", deleted.exception.code)

    def test_AC_PERM_005_owner_has_all_permissions_from_owner_only(self):
        effective = self.service.effective_document(self.owner, 1)
        self.assertTrue(effective.can_read)
        self.assertTrue(effective.can_write)
        self.assertTrue(effective.can_admin)
        self.assertEqual(("OWNER",), effective.sources)

    def test_AC_PERM_006_public_document_grants_read_only(self):
        self.documents.add(7, owner_user_id=1, visibility="PUBLIC")
        effective = self.service.effective_document(principal(2), 7)
        self.assertTrue(effective.can_read)
        self.assertFalse(effective.can_write)
        self.assertFalse(effective.can_admin)
        self.assertIn("PUBLIC", effective.sources)


if __name__ == "__main__":
    unittest.main()
