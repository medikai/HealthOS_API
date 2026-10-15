import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from src.app.api.v1.events_stream import _matches_scope
from src.app.api.v1.patients import (
    _abha_search_keys,
    _duplicate_match_reason,
    _normalize_abha_address,
    _normalize_abha_number,
    create_patient,
    duplicate_patients,
    recently_viewed_patients,
    record_patient_view,
    search_patients,
    update_patient,
)
from src.app.api.v1.scheduling import list_appointments
from src.app.core.events import make_event
from src.app.schemas.patients import PatientCreate, PatientUpdate, PatientViewRequest


def entity(number: int, **values):
    id_val = values.pop("id", UUID(int=number))
    return SimpleNamespace(id=id_val, **values)


class PatientRegistryC1Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.org_id = UUID("01a0a022-b2aa-7509-aecb-3166583df954")
        self.facility_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
        self.account = entity(100, logto_user_id="user-1")
        self.organization = entity(
            200,
            id=self.org_id,
            name="Test Medical Center",
            code="TMC",
            is_active=True,
        )

    # ------------------------------------------------------------------
    # ABHA canonicalization
    # ------------------------------------------------------------------
    def test_normalize_abha_number(self) -> None:
        self.assertEqual(_normalize_abha_number("12-3456-7890-1234"), "12345678901234")
        self.assertEqual(_normalize_abha_number("1234 5678 9012 34"), "12345678901234")
        self.assertIsNone(_normalize_abha_number(None))
        self.assertIsNone(_normalize_abha_number(""))
        with self.assertRaises(HTTPException) as ctx:
            _normalize_abha_number("123-456")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_normalize_abha_address(self) -> None:
        self.assertEqual(_normalize_abha_address(" Aarav@ABDM "), "aarav@abdm")
        self.assertIsNone(_normalize_abha_address(None))
        for invalid in ("invalid", "a@b@c", "@abdm", "name@"):
            with self.assertRaises(HTTPException):
                _normalize_abha_address(invalid)

    def test_abha_search_keys(self) -> None:
        self.assertEqual(
            _abha_search_keys("12 3456 7890 1234"), ("12345678901234", None)
        )
        self.assertEqual(_abha_search_keys("aarav@abdm"), (None, "aarav@abdm"))
        self.assertEqual(_abha_search_keys("9876543210"), (None, None))

    def test_patient_create_schema_accepts_abha_and_override(self) -> None:
        nested = PatientCreate.model_validate(
            {
                "person": {"first_name": "Jacob"},
                "duplicate_override_reason": "Confirmed distinct at reception",
            }
        )
        normalized = nested.normalized()
        self.assertEqual(normalized.first_name, "Jacob")
        self.assertEqual(
            normalized.duplicate_override_reason, "Confirmed distinct at reception"
        )
        flat = PatientCreate.model_validate(
            {
                "first_name": "Jacob",
                "abha_number": "12-3456-7890-1234",
                "abha_address": "Jacob@abdm",
            }
        )
        self.assertEqual(flat.normalized().abha_number, "12-3456-7890-1234")

    # ------------------------------------------------------------------
    # Search scope + ABHA + last visit
    # ------------------------------------------------------------------
    async def _compiled_search(self, **kwargs) -> str:
        captured = {}

        async def fake_execute(statement):
            captured["query"] = statement
            return SimpleNamespace(all=list)

        db = SimpleNamespace(execute=AsyncMock(side_effect=fake_execute))
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            await search_patients(account=self.account, db=db, **kwargs)
        return str(
            captured["query"].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )

    async def test_search_scope_name_excludes_identifier_columns(self) -> None:
        compiled = await self._compiled_search(q="Aarav Sharma", scope="name")
        self.assertIn("lower(identity.person.first_name)", compiled)
        self.assertNotIn("person.phone ILIKE", compiled)
        self.assertNotIn("person.phone IN", compiled)
        self.assertNotIn("patient.mrn ILIKE", compiled)
        self.assertNotIn("abha_number =", compiled)

    async def test_search_scope_abha_targets_abha_columns(self) -> None:
        compiled = await self._compiled_search(q="12-3456-7890-1234", scope="abha")
        self.assertIn("abha_number =", compiled)
        self.assertIn("abha_number ILIKE", compiled)
        self.assertNotIn("person.phone ILIKE", compiled)
        self.assertNotIn("patient.mrn ILIKE", compiled)

    async def test_search_all_scope_includes_abha_and_last_visit(self) -> None:
        compiled = await self._compiled_search(q="Aarav")
        self.assertIn("abha_number", compiled)
        self.assertIn("care.encounter", compiled)
        self.assertIn("max(", compiled.lower())

    async def test_search_rejects_invalid_scope(self) -> None:
        db = AsyncMock()
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with self.assertRaises(HTTPException) as ctx:
                await search_patients(
                    q="Aarav",
                    account=self.account,
                    db=db,
                    scope="everything",
                )
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Invalid search scope", ctx.exception.detail)

    # ------------------------------------------------------------------
    # Duplicate probe
    # ------------------------------------------------------------------
    def test_duplicate_match_reason_precedence(self) -> None:
        person = entity(
            1,
            first_name="Aarav",
            last_name="Sharma",
            phone="+919876543210",
            date_of_birth="1990-01-01",
            abha_number="12345678901234",
            abha_address="aarav@abdm",
        )
        self.assertEqual(
            _duplicate_match_reason(
                person,
                phone=None,
                first_name=None,
                last_name=None,
                date_of_birth=None,
                abha_number="12345678901234",
                abha_address=None,
            ),
            "abha",
        )
        self.assertEqual(
            _duplicate_match_reason(
                person,
                phone="9876543210",
                first_name=None,
                last_name=None,
                date_of_birth=None,
                abha_number=None,
                abha_address=None,
            ),
            "phone",
        )
        self.assertEqual(
            _duplicate_match_reason(
                person,
                phone=None,
                first_name="Aarav",
                last_name="Sharma",
                date_of_birth="1990-01-01",
                abha_number=None,
                abha_address=None,
            ),
            "name_dob",
        )
        self.assertEqual(
            _duplicate_match_reason(
                person,
                phone=None,
                first_name="Aarav",
                last_name="Sharma",
                date_of_birth=None,
                abha_number=None,
                abha_address=None,
            ),
            "name",
        )

    async def test_duplicate_patients_accepts_dob_and_reports_reason(self) -> None:
        p1 = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        person1 = entity(
            11,
            first_name="Aarav",
            last_name="Sharma",
            phone="+919876543210",
            email=None,
            date_of_birth="1990-01-01",
            gender="male",
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(p1, person1)]))
        )
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await duplicate_patients(
                first_name="Aarav",
                last_name="Sharma",
                date_of_birth="1990-01-01",
                account=self.account,
                db=db,
            )
        item = res["data"]["items"][0]
        self.assertEqual(item["match_reason"], "name_dob")

    # ------------------------------------------------------------------
    # Registration duplicate rerun + override audit
    # ------------------------------------------------------------------
    async def test_create_patient_blocks_potential_duplicate(self) -> None:
        candidates = [{"uuid": "p1", "match_reason": "phone"}]
        db = AsyncMock()
        with (
            patch("src.app.api.v1.patients._org", return_value=self.organization),
            patch(
                "src.app.api.v1.patients._duplicate_candidates",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "src.app.api.v1.patients.resolve_active_salutation",
                new=AsyncMock(return_value=None),
            ),self.assertRaises(HTTPException) as ctx
        ):
            await create_patient(
                payload=PatientCreate(first_name="Aarav", phone="9876543210"),
                account=self.account,
                db=db,
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "POTENTIAL_DUPLICATE")
        self.assertEqual(ctx.exception.detail["candidates"], candidates)
        db.commit.assert_not_awaited()

    async def test_create_patient_override_records_audit_details(self) -> None:
        candidates = [{"uuid": "p1", "match_reason": "name_dob"}]
        audit = AsyncMock()
        db = AsyncMock()
        db.add = Mock()
        with (
            patch("src.app.api.v1.patients._org", return_value=self.organization),
            patch(
                "src.app.api.v1.patients._duplicate_candidates",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "src.app.api.v1.patients.resolve_active_salutation",
                new=AsyncMock(return_value=None),
            ),
            patch("src.app.api.v1.patients.record_audit", new=audit),
        ):
            res = await create_patient(
                payload=PatientCreate(
                    first_name="Aarav",
                    last_name="Sharma",
                    date_of_birth="1990-01-01",
                    duplicate_override_reason="Confirmed distinct at reception",
                ),
                account=self.account,
                db=db,
            )
        self.assertTrue(res["success"])
        db.commit.assert_awaited_once()
        audit.assert_awaited_once()
        details = audit.await_args.kwargs["details"]
        self.assertTrue(details["duplicate_override"])
        self.assertEqual(details["matched_patients"], [{"uuid": "p1", "match_reason": "name_dob"}])

    async def test_create_patient_maps_identifier_conflict(self) -> None:
        db = AsyncMock()
        db.add = Mock()
        db.flush = AsyncMock(
            side_effect=IntegrityError("INSERT", {}, Exception("duplicate key"))
        )
        with (
            patch("src.app.api.v1.patients._org", return_value=self.organization),
            patch(
                "src.app.api.v1.patients._duplicate_candidates",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "src.app.api.v1.patients.resolve_active_salutation",
                new=AsyncMock(return_value=None),
            ),self.assertRaises(HTTPException) as ctx
        ):
            await create_patient(
                payload=PatientCreate(first_name="Aarav", phone="9876543210"),
                account=self.account,
                db=db,
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "PATIENT_IDENTIFIER_CONFLICT")
        db.rollback.assert_awaited_once()

    async def test_update_patient_rejects_phone_conflict(self) -> None:
        patient = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        person = entity(11, first_name="Aarav", last_name="Sharma", phone=None)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=SimpleNamespace(first=lambda: (patient, person)))
        db.scalar = AsyncMock(return_value=UUID(int=99))
        with (
            patch("src.app.api.v1.patients._org", return_value=self.organization),
            self.assertRaises(HTTPException) as ctx,
        ):
            await update_patient(
                patient_uuid=patient.id,
                payload=PatientUpdate(first_name="Aarav", phone="9876543210"),
                account=self.account,
                db=db,
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["fields"], ["phone"])

    # ------------------------------------------------------------------
    # Recently viewed
    # ------------------------------------------------------------------
    async def test_recently_viewed_returns_view_metadata(self) -> None:
        p1 = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        person1 = entity(
            11,
            first_name="Aarav",
            last_name="Sharma",
            phone=None,
            email=None,
            date_of_birth=None,
            gender=None,
            abha_number=None,
            abha_address=None,
        )
        viewed_at = datetime(2026, 10, 15, 4, 30, tzinfo=UTC)
        facility_id = UUID(int=500)
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    all=lambda: [(p1, person1, viewed_at, facility_id)]
                )
            )
        )
        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await recently_viewed_patients(
                limit=20, account=self.account, db=db
            )
        item = res["data"]["items"][0]
        self.assertEqual(item["uuid"], str(p1.id))
        self.assertEqual(item["viewed_at"], viewed_at.isoformat())
        self.assertEqual(item["last_viewed_facility_uuid"], str(facility_id))

    async def test_record_patient_view_audits_and_publishes_targeted_event(self) -> None:
        patient = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        db = AsyncMock()
        db.add = Mock()
        db.scalar = AsyncMock(side_effect=[patient, None])
        audit = AsyncMock()
        publisher = AsyncMock()
        with (
            patch("src.app.api.v1.patients._org", return_value=self.organization),
            patch("src.app.api.v1.patients.record_audit", new=audit),
            patch("src.app.api.v1.patients.publish", new=publisher),
        ):
            res = await record_patient_view(
                patient_uuid=patient.id,
                payload=PatientViewRequest(),
                account=self.account,
                db=db,
            )
        self.assertEqual(res["data"]["patient_uuid"], str(patient.id))
        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        self.assertEqual(audit.await_args.kwargs["action"], "patient.viewed")
        event = publisher.await_args.args[0]
        self.assertEqual(event["event_type"], "patient.viewed")
        self.assertEqual(event["target_user_id"], str(self.account.id))
        self.assertIsNone(event["facility_id"])

    # ------------------------------------------------------------------
    # Realtime scope filtering
    # ------------------------------------------------------------------
    def test_make_event_supports_target_user_id(self) -> None:
        event = make_event(
            "patient.viewed",
            entity_id=UUID(int=1),
            entity_version=None,
            organization_id=self.org_id,
            facility_id=None,
            target_user_id=self.account.id,
        )
        self.assertEqual(event["target_user_id"], str(self.account.id))
        self.assertIsNone(event["facility_id"])

    def test_matches_scope_targeted_events_bypass_facility_filter(self) -> None:
        scope = {
            "account_id": self.account.id,
            "organization_id": self.org_id,
            "facilities": {self.facility_id},
            "practitioner_id": None,
        }
        targeted = make_event(
            "patient.viewed",
            entity_id=UUID(int=1),
            entity_version=None,
            organization_id=self.org_id,
            facility_id=None,
            target_user_id=self.account.id,
        )
        self.assertTrue(_matches_scope(targeted, scope))

        other_account = make_event(
            "patient.viewed",
            entity_id=UUID(int=1),
            entity_version=None,
            organization_id=self.org_id,
            facility_id=None,
            target_user_id=UUID(int=999),
        )
        self.assertFalse(_matches_scope(other_account, scope))

    def test_matches_scope_filters_regular_events(self) -> None:
        scope = {
            "account_id": self.account.id,
            "organization_id": self.org_id,
            "facilities": {self.facility_id},
            "practitioner_id": None,
        }
        in_scope = make_event(
            "queue.changed",
            entity_id=UUID(int=1),
            entity_version=None,
            organization_id=self.org_id,
            facility_id=self.facility_id,
        )
        self.assertTrue(_matches_scope(in_scope, scope))

        other_facility = make_event(
            "queue.changed",
            entity_id=UUID(int=1),
            entity_version=None,
            organization_id=self.org_id,
            facility_id=UUID(int=777),
        )
        self.assertFalse(_matches_scope(other_facility, scope))

        no_facility = make_event(
            "queue.changed",
            entity_id=UUID(int=1),
            entity_version=None,
            organization_id=self.org_id,
            facility_id=None,
        )
        self.assertFalse(_matches_scope(no_facility, scope))

    # ------------------------------------------------------------------
    # Appointment range
    # ------------------------------------------------------------------
    async def _compiled_appointments(self, **kwargs) -> str:
        captured = {}

        async def fake_execute(statement):
            captured["query"] = statement
            return SimpleNamespace(all=list)

        db = SimpleNamespace(execute=AsyncMock(side_effect=fake_execute))
        await list_appointments(account=self.account, db=db, **kwargs)
        return str(
            captured["query"].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )

    async def test_list_appointments_date_range_and_limit(self) -> None:
        from_datetime = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
        to_datetime = from_datetime.replace(hour=23, minute=59, second=59)
        compiled = await self._compiled_appointments(
            facility_uuid=None,
            status_filter=None,
            patient_uuid=None,
            order="asc",
            from_datetime=from_datetime,
            to_datetime=to_datetime,
            limit=2,
            offset=0,
        )
        self.assertIn("scheduled_start >=", compiled)
        self.assertIn("scheduled_start <", compiled)
        self.assertIn("LIMIT 3", compiled)

    async def test_list_appointments_without_limit_keeps_legacy_behavior(self) -> None:
        compiled = await self._compiled_appointments(
            facility_uuid=None,
            status_filter=None,
            patient_uuid=None,
            order="asc",
            from_datetime=None,
            to_datetime=None,
            limit=None,
            offset=0,
        )
        self.assertNotIn("LIMIT", compiled)


if __name__ == "__main__":
    unittest.main()
