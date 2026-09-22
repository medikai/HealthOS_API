import base64
import json
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.app.api.v1.patients import (
    _decode_cursor,
    _encode_cursor,
    _escape_like,
    _phone_search_variants,
    list_patients,
    search_patients,
)
from src.app.models.identity import Patient, Person, UserAccount
from src.app.models.organization import Organization


def entity(number: int, **values):
    id_val = values.pop("id", UUID(int=number))
    return SimpleNamespace(id=id_val, **values)


class PatientSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.org_id = UUID("01a0a022-b2aa-7509-aecb-3166583df954")
        self.facility_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
        self.account = entity(
            100,
            logto_user_id="user-1",
        )
        self.organization = entity(
            200,
            id=self.org_id,
            name="Test Medical Center",
            code="TMC",
            is_active=True,
        )

    def test_phone_search_variants_normalization(self) -> None:
        # Standard 10-digit number
        variants = _phone_search_variants("9876543210")
        self.assertIn("9876543210", variants)
        self.assertIn("+919876543210", variants)
        self.assertIn("09876543210", variants)

        # Number with +91 country code and formatting
        variants_formatted = _phone_search_variants("+91 (987) 654-3210")
        self.assertIn("9876543210", variants_formatted)
        self.assertIn("+919876543210", variants_formatted)

        # Leading 0
        variants_zero = _phone_search_variants("09876543210")
        self.assertIn("9876543210", variants_zero)
        self.assertIn("+919876543210", variants_zero)

    def test_escape_like_wildcards(self) -> None:
        self.assertEqual(_escape_like("test%user_1\\2"), "test\\%user\\_1\\\\2")
        self.assertEqual(_escape_like("plain"), "plain")

    def test_cursor_encoding_and_decoding(self) -> None:
        last_values = (1, "aarav", "mehta", str(UUID(int=10)))
        cursor = _encode_cursor(
            v=1,
            org_id=self.org_id,
            facility_id=self.facility_id,
            q="aarav",
            last_values=last_values,
        )
        self.assertIsInstance(cursor, str)

        rank, first, last, pat_id = _decode_cursor(
            cursor, self.org_id, self.facility_id, "aarav"
        )
        self.assertEqual(rank, 1)
        self.assertEqual(first, "aarav")
        self.assertEqual(last, "mehta")
        self.assertEqual(pat_id, UUID(int=10))

    def test_cursor_rejects_scope_mismatch(self) -> None:
        last_values = (1, "aarav", "mehta", str(UUID(int=10)))
        cursor = _encode_cursor(
            v=1,
            org_id=self.org_id,
            facility_id=self.facility_id,
            q="aarav",
            last_values=last_values,
        )
        other_org_id = UUID(int=999)
        with self.assertRaises(HTTPException) as ctx:
            _decode_cursor(cursor, other_org_id, self.facility_id, "aarav")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("organization scope", ctx.exception.detail)

    def test_cursor_rejects_query_mismatch(self) -> None:
        last_values = (1, "aarav", "mehta", str(UUID(int=10)))
        cursor = _encode_cursor(
            v=1,
            org_id=self.org_id,
            facility_id=None,
            q="aarav",
            last_values=last_values,
        )
        with self.assertRaises(HTTPException) as ctx:
            _decode_cursor(cursor, self.org_id, None, "rohan")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("search query", ctx.exception.detail)

    async def test_search_rejects_ambiguous_cursor_and_offset(self) -> None:
        db = AsyncMock()
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with self.assertRaises(HTTPException) as ctx:
                await search_patients(
                    q="test",
                    account=self.account,
                    db=db,
                    limit=20,
                    offset=10,
                    cursor="some_cursor",
                )
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Ambiguous pagination", ctx.exception.detail)

    async def test_search_returns_minimal_permitted_identity_fields(self) -> None:
        p1 = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        person1 = entity(
            11,
            first_name="Aarav",
            last_name="Sharma",
            phone="+919876543210",
            email="aarav@example.com",
            date_of_birth="1990-01-01",
            gender="male",
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(p1, person1, 1)]))
        )

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await search_patients(
                q="Aarav",
                account=self.account,
                db=db,
                limit=20,
                offset=0,
            )

        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]["items"]), 1)
        item = res["data"]["items"][0]
        self.assertEqual(item["uuid"], str(p1.id))
        self.assertEqual(item["mrn"], "MRN-001")
        self.assertEqual(item["medical_record_number"], "MRN-001")
        self.assertEqual(item["display_name"], "Aarav Sharma")
        self.assertEqual(item["first_name"], "Aarav")
        self.assertEqual(item["last_name"], "Sharma")
        self.assertEqual(item["phone"], "+919876543210")
        self.assertEqual(item["email"], "aarav@example.com")
        self.assertEqual(item["status"], "active")
        self.assertFalse(res["data"]["has_more"])
        self.assertIsNone(res["data"]["next_cursor"])

    async def test_search_keyset_continuation_has_more(self) -> None:
        p1 = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        person1 = entity(
            11,
            first_name="Aarav",
            last_name="Sharma",
            phone="+919876543210",
            email="aarav@example.com",
            date_of_birth="1990-01-01",
            gender="male",
        )
        p2 = entity(2, mrn="MRN-002", is_active=True, organization_id=self.org_id)
        person2 = entity(
            12,
            first_name="Aarav",
            last_name="Patel",
            phone="+919876543211",
            email="aarav.p@example.com",
            date_of_birth="1992-02-02",
            gender="male",
        )
        # 3 rows returned for limit=2 (means has_more=True)
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    all=lambda: [(p1, person1, 1), (p2, person2, 2), (entity(3), entity(13), 3)]
                )
            )
        )

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await list_patients(
                q="Aarav",
                account=self.account,
                db=db,
                limit=2,
                offset=0,
            )

        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]["items"]), 2)
        self.assertTrue(res["data"]["has_more"])
        self.assertIsNotNone(res["data"]["next_cursor"])

        # Decode generated next cursor and verify it points to last item on page (p2)
        rank, first, last, pat_id = _decode_cursor(
            res["data"]["next_cursor"], self.org_id, None, "Aarav"
        )
        self.assertEqual(rank, 2)
        self.assertEqual(first, "aarav")
        self.assertEqual(last, "patel")
        self.assertEqual(pat_id, p2.id)

    async def test_facility_scoping_verified_and_filtered(self) -> None:
        captured_query = None

        async def fake_execute(statement):
            nonlocal captured_query
            captured_query = statement
            return SimpleNamespace(all=lambda: [])

        db = SimpleNamespace(execute=AsyncMock(side_effect=fake_execute))

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with patch("src.app.api.v1.patients._scope", new_callable=AsyncMock) as mock_scope:
                mock_scope.return_value = (self.organization, SimpleNamespace(id=self.facility_id))
                res = await search_patients(
                    q="Sharma",
                    account=self.account,
                    db=db,
                    facility_id=self.facility_id,
                )

                mock_scope.assert_awaited_once_with(db, self.account, self.facility_id)
                self.assertTrue(res["success"])
                compiled = str(captured_query.compile(dialect=postgresql.dialect()))
                self.assertIn("care.appointment", compiled.lower())
                self.assertIn("care.encounter", compiled.lower())
                self.assertEqual(compiled.upper().count("EXISTS (SELECT"), 3)
                self.assertNotIn("care.appointment, care.encounter", compiled.lower())

    async def test_facility_uuid_scoping_verified_and_filtered(self) -> None:
        db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with patch("src.app.api.v1.patients._scope", new_callable=AsyncMock) as mock_scope:
                mock_scope.return_value = (self.organization, SimpleNamespace(id=self.facility_id))
                res = await search_patients(
                    q="Sharma",
                    account=self.account,
                    db=db,
                    facility_uuid=self.facility_id,
                )

                mock_scope.assert_awaited_once_with(db, self.account, self.facility_id)
                self.assertTrue(res["success"])

    async def test_short_query_gating_compilation(self) -> None:
        captured_query = None

        async def fake_execute(statement):
            nonlocal captured_query
            captured_query = statement
            return SimpleNamespace(all=lambda: [])

        db = SimpleNamespace(execute=AsyncMock(side_effect=fake_execute))

        # 1. Short query "aa" (< 3 characters)
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            await search_patients(
                q="aa",
                account=self.account,
                db=db,
            )
        compiled_short = str(
            captured_query.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        # Should have prefix matching 'aa%%' but not contains '%%aa%%'
        self.assertIn("ILIKE 'aa%%'", compiled_short)
        self.assertNotIn("%%aa%%", compiled_short)

        # 2. Long query "aarav" (>= 3 characters)
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            await search_patients(
                q="aarav",
                account=self.account,
                db=db,
            )
        compiled_long = str(
            captured_query.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        # Should have both prefix and contains '%%aarav%%'
        self.assertIn("ILIKE 'aarav%%'", compiled_long)
        self.assertIn("ILIKE '%%aarav%%'", compiled_long)

    async def test_search_rejects_excessive_offset(self) -> None:
        db = AsyncMock()
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with self.assertRaises(HTTPException) as ctx:
                await search_patients(
                    q="test",
                    account=self.account,
                    db=db,
                    offset=10001,
                )
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Offset exceeds maximum allowed limit", ctx.exception.detail)

    async def test_duplicate_patients_endpoint(self) -> None:
        p1 = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        person1 = entity(
            11,
            first_name="Aarav",
            last_name="Sharma",
            phone="+919876543210",
            email="aarav@example.com",
            date_of_birth="1990-01-01",
            gender="male",
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(p1, person1)]))
        )
        from src.app.api.v1.patients import duplicate_patients

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await duplicate_patients(
                phone="9876543210",
                first_name="Aarav",
                last_name="Sharma",
                account=self.account,
                db=db,
            )
        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]["items"]), 1)
        self.assertEqual(res["data"]["items"][0]["mrn"], "MRN-001")


if __name__ == "__main__":
    unittest.main()
