import json
import unittest
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError

from src.app.api.v1.scheduling import create_exception_booking, reschedule_appointment
from src.app.core.availability import validate_interval
from src.app.schemas.scheduling import (
    AppointmentCreate,
    AppointmentReschedule,
    ExceptionBookingCreate,
)


class ExceptionBookingTests(unittest.IsolatedAsyncioTestCase):
    def _reschedule_fixture(self, *, status="booked"):
        appointment = SimpleNamespace(
            id=UUID(int=40), organization_id=UUID(int=10), facility_id=UUID(int=20),
            practitioner_id=UUID(int=30), patient_id=UUID(int=50), status=status,
            version=1, resource_id=UUID(int=60),
            scheduled_start=datetime(2026, 9, 25, 9, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 25, 9, 30, tzinfo=UTC),
            reason_code=None, reason_text=None,
        )
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[appointment, UUID(int=20), "UTC", SimpleNamespace(id=UUID(int=30))]),
            commit=AsyncMock(), rollback=AsyncMock(),
        )
        return appointment, db

    async def test_reschedule_keeps_id_updates_version_and_returns_affected_ranges(self) -> None:
        appointment, db = self._reschedule_fixture()
        payload = AppointmentReschedule(
            scheduled_start=datetime(2026, 9, 25, 10, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 25, 10, 30, tzinfo=UTC),
            version=1, reason="Patient request",
        )
        with patch("src.app.api.v1.scheduling._scope", AsyncMock()), \
             patch("src.app.api.v1.scheduling.validate_interval", AsyncMock(return_value=UUID(int=61))), \
             patch("src.app.api.v1.scheduling.record_audit", AsyncMock()) as audit:
            response = await reschedule_appointment(payload=payload, appointment_uuid=appointment.id, account=SimpleNamespace(id=UUID(int=70)), db=db)

        self.assertEqual(appointment.id, UUID(int=40))
        self.assertEqual(appointment.version, 2)
        self.assertEqual(response["data"]["uuid"], str(UUID(int=40)))
        self.assertEqual(response["meta"]["affected_ranges"]["old"]["resource_uuid"], str(UUID(int=60)))
        self.assertEqual(response["meta"]["affected_ranges"]["new"]["resource_uuid"], str(UUID(int=61)))
        audit.assert_awaited_once()
        db.commit.assert_awaited_once()

    async def test_reschedule_competing_target_leaves_original_booking_intact(self) -> None:
        appointment, db = self._reschedule_fixture()
        original = (appointment.scheduled_start, appointment.scheduled_end, appointment.resource_id, appointment.version)
        payload = AppointmentReschedule(
            scheduled_start=datetime(2026, 9, 25, 10, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 25, 10, 30, tzinfo=UTC),
            version=1,
        )
        with patch("src.app.api.v1.scheduling._scope", AsyncMock()), \
             patch("src.app.api.v1.scheduling.validate_interval", AsyncMock(side_effect=ValueError("RESOURCE_BOOKED|Target is already booked."))), \
             patch("src.app.api.v1.scheduling.record_audit", AsyncMock()):
            with self.assertRaises(Exception):
                await reschedule_appointment(payload=payload, appointment_uuid=appointment.id, account=SimpleNamespace(id=UUID(int=70)), db=db)
        self.assertEqual((appointment.scheduled_start, appointment.scheduled_end, appointment.resource_id, appointment.version), original)
        db.commit.assert_not_awaited()

    async def test_reschedule_rejects_stale_retry_without_second_audit(self) -> None:
        appointment, db = self._reschedule_fixture()
        payload = AppointmentReschedule(
            scheduled_start=datetime(2026, 9, 25, 10, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 25, 10, 30, tzinfo=UTC),
            version=2,
        )
        with patch("src.app.api.v1.scheduling._scope", AsyncMock()), \
             patch("src.app.api.v1.scheduling.validate_interval", AsyncMock()) as validate, \
             patch("src.app.api.v1.scheduling.record_audit", AsyncMock()) as audit:
            with self.assertRaises(Exception):
                await reschedule_appointment(payload=payload, appointment_uuid=appointment.id, account=SimpleNamespace(id=UUID(int=70)), db=db)
        validate.assert_not_awaited()
        audit.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_reschedule_rolls_back_database_conflict(self) -> None:
        appointment, db = self._reschedule_fixture()
        db.commit.side_effect = IntegrityError("update", {}, Exception("exclusion conflict"))
        payload = AppointmentReschedule(
            scheduled_start=datetime(2026, 9, 25, 10, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 25, 10, 30, tzinfo=UTC),
            version=1,
        )
        with patch("src.app.api.v1.scheduling._scope", AsyncMock()), \
             patch("src.app.api.v1.scheduling.validate_interval", AsyncMock(return_value=None)), \
             patch("src.app.api.v1.scheduling.record_audit", AsyncMock()):
            with self.assertRaises(Exception):
                await reschedule_appointment(payload=payload, appointment_uuid=appointment.id, account=SimpleNamespace(id=UUID(int=70)), db=db)
        db.rollback.assert_awaited_once()

    def test_schema_maps_room_uuid_to_resource_uuid(self) -> None:
        room_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0e")
        payload = AppointmentCreate(
            facility_uuid=UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d"),
            practitioner_uuid=UUID("01a0a022-b31b-728f-b7ae-b06211a65893"),
            patient_uuid=UUID("01a0a94a-a8ca-73ea-880b-a3ea59622542"),
            room_uuid=room_id,
            scheduled_start=datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        )
        self.assertEqual(payload.resource_uuid, room_id)

    def test_schema_accepts_exception_booking_with_room_uuid_null_and_confirmation_acknowledged(self) -> None:
        payload = ExceptionBookingCreate(
            facility_uuid=UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d"),
            practitioner_uuid=UUID("01a0a022-b31b-728f-b7ae-b06211a65893"),
            patient_uuid=UUID("01a0a94a-a8ca-73ea-880b-a3ea59622542"),
            room_uuid=None,
            scheduled_start=datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
            reason="Urgent checkup",
            override_types=["facility_closed"],
            doctor_agreement_recorded=True,
            confirmation_acknowledged=True,
        )
        self.assertIsNone(payload.resource_uuid)
        self.assertEqual(payload.override_types, ["facility_closed"])
        self.assertTrue(payload.confirmation_acknowledged)

    def test_schema_reschedule_maps_room_uuid(self) -> None:
        room_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0e")
        reschedule = AppointmentReschedule(
            scheduled_start=datetime(2026, 9, 20, 13, 30, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
            version=1,
            room_uuid=room_id,
        )
        self.assertEqual(reschedule.resource_uuid, room_id)

    async def test_validate_interval_closed_facility_raises_facility_closed_without_override(self) -> None:
        # Sunday 2026-09-20 (weekday 6)
        facility_schedule = SimpleNamespace(
            operating_days=json.dumps(["mon", "tue", "wed", "thu", "fri", "sat"]),
            operating_start="09:00:00",
            operating_end="21:00:00",
            days_of_week=json.dumps(["mon", "tue", "wed", "thu", "fri", "sat"]),
            timezone="Asia/Kolkata",
        )
        start = datetime.fromisoformat("2026-09-20T19:00:00+05:30")
        end = datetime.fromisoformat("2026-09-20T19:30:00+05:30")

        mock_db = SimpleNamespace(
            scalar=AsyncMock(return_value="Asia/Kolkata"),
            scalars=AsyncMock(),
        )

        context_data = {
            "schedule": facility_schedule,
            "tz": ZoneInfo("Asia/Kolkata"),
            "rules": [],
            "exceptions": [],
            "periods": [],
            "resources": {},
            "appointments": [],
        }

        with patch("src.app.core.availability._context", AsyncMock(return_value=context_data)):
            with self.assertRaises(ValueError) as ctx:
                await validate_interval(
                    mock_db,
                    organization_id=UUID(int=1),
                    facility_id=UUID(int=2),
                    practitioner_id=UUID(int=3),
                    start=start,
                    end=end,
                    resource_id=None,
                    allowed_overrides=set(),
                )
            self.assertIn("FACILITY_CLOSED", str(ctx.exception))

    async def test_validate_interval_closed_facility_succeeds_with_facility_closed_override_even_if_no_rules(self) -> None:
        facility_schedule = SimpleNamespace(
            operating_days=json.dumps(["mon", "tue", "wed", "thu", "fri", "sat"]),
            operating_start="09:00:00",
            operating_end="21:00:00",
            days_of_week=json.dumps(["mon", "tue", "wed", "thu", "fri", "sat"]),
            timezone="Asia/Kolkata",
        )
        start = datetime.fromisoformat("2026-09-20T19:00:00+05:30")
        end = datetime.fromisoformat("2026-09-20T19:30:00+05:30")

        mock_db = SimpleNamespace(
            scalar=AsyncMock(return_value="Asia/Kolkata"),
            scalars=AsyncMock(),
        )

        context_data = {
            "schedule": facility_schedule,
            "tz": ZoneInfo("Asia/Kolkata"),
            "rules": [],
            "exceptions": [],
            "periods": [],
            "resources": {},
            "appointments": [],
        }

        with patch("src.app.core.availability._context", AsyncMock(return_value=context_data)):
            resource = await validate_interval(
                mock_db,
                organization_id=UUID(int=1),
                facility_id=UUID(int=2),
                practitioner_id=UUID(int=3),
                start=start,
                end=end,
                resource_id=None,
                allowed_overrides={"facility_closed"},
            )
            self.assertIsNone(resource)

    async def test_validate_interval_closed_facility_succeeds_when_practitioner_has_rules_on_other_days(self) -> None:
        facility_schedule = SimpleNamespace(
            operating_days=json.dumps(["mon", "tue", "wed", "thu", "fri"]),
            operating_start="09:00:00",
            operating_end="18:00:00",
            days_of_week=json.dumps(["mon", "tue", "wed", "thu", "fri"]),
            timezone="Asia/Kolkata",
        )
        start = datetime.fromisoformat("2026-09-20T19:00:00+05:30")
        end = datetime.fromisoformat("2026-09-20T19:30:00+05:30")

        monday_rule = SimpleNamespace(
            weekday=0,
            valid_from=date(2026, 1, 1),
            valid_until=None,
            start_time=time(9, 0),
            end_time=time(17, 0),
            resource_id=UUID(int=10),
        )

        mock_db = SimpleNamespace(
            scalar=AsyncMock(return_value="Asia/Kolkata"),
            scalars=AsyncMock(),
        )

        context_data = {
            "schedule": facility_schedule,
            "tz": ZoneInfo("Asia/Kolkata"),
            "rules": [monday_rule],
            "exceptions": [],
            "periods": [],
            "resources": {},
            "appointments": [],
        }

        with patch("src.app.core.availability._context", AsyncMock(return_value=context_data)):
            resource = await validate_interval(
                mock_db,
                organization_id=UUID(int=1),
                facility_id=UUID(int=2),
                practitioner_id=UUID(int=3),
                start=start,
                end=end,
                resource_id=None,
                allowed_overrides={"facility_closed"},
            )
            self.assertIsNone(resource)

    async def test_validate_interval_closed_facility_still_checks_conflict(self) -> None:
        facility_schedule = SimpleNamespace(
            operating_days=json.dumps(["mon", "tue"]),
            operating_start="09:00:00",
            operating_end="18:00:00",
            days_of_week=json.dumps(["mon", "tue"]),
            timezone="Asia/Kolkata",
        )
        start = datetime.fromisoformat("2026-09-20T19:00:00+05:30")
        end = datetime.fromisoformat("2026-09-20T19:30:00+05:30")

        existing_appointment = SimpleNamespace(
            id=UUID(int=99),
            practitioner_id=UUID(int=3),
            resource_id=None,
            scheduled_start=start,
            scheduled_end=end,
        )

        mock_db = SimpleNamespace(
            scalar=AsyncMock(return_value="Asia/Kolkata"),
            scalars=AsyncMock(),
        )

        context_data = {
            "schedule": facility_schedule,
            "tz": ZoneInfo("Asia/Kolkata"),
            "rules": [],
            "exceptions": [],
            "periods": [],
            "resources": {},
            "appointments": [existing_appointment],
        }

        with patch("src.app.core.availability._context", AsyncMock(return_value=context_data)):
            with self.assertRaises(ValueError) as ctx:
                await validate_interval(
                    mock_db,
                    organization_id=UUID(int=1),
                    facility_id=UUID(int=2),
                    practitioner_id=UUID(int=3),
                    start=start,
                    end=end,
                    resource_id=None,
                    allowed_overrides={"facility_closed"},
                )
            self.assertIn("BOOKED", str(ctx.exception))

    async def test_create_exception_booking_endpoint_flow(self) -> None:
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        scheduled_start = datetime.combine(today, time(19, 0)).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        scheduled_end = datetime.combine(today, time(19, 30)).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        payload = ExceptionBookingCreate(
            patient_uuid=UUID("01a0a94a-a8ca-73ea-880b-a3ea59622542"),
            practitioner_uuid=UUID("01a0a022-b31b-728f-b7ae-b06211a65893"),
            facility_uuid=UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d"),
            room_uuid=None,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            reason="Urgent checkup",
            override_types=["facility_closed"],
            doctor_agreement_recorded=True,
            confirmation_acknowledged=True,
        )

        mock_db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[
                UUID(int=1),  # Facility.id
                SimpleNamespace(id=payload.practitioner_uuid, organization_id=UUID(int=10), is_active=True),  # practitioner
                SimpleNamespace(id=payload.patient_uuid, organization_id=UUID(int=10), is_active=True),  # patient
                "Asia/Kolkata",  # facility_tz in _facility_timezone
            ]),
            add=Mock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        account = SimpleNamespace(id=UUID(int=999))

        with patch("src.app.api.v1.scheduling._scope", AsyncMock(return_value=(SimpleNamespace(id=UUID(int=10)), None))), \
             patch("src.app.api.v1.scheduling._require_exception_booking_permission", AsyncMock(return_value=None)), \
             patch("src.app.api.v1.scheduling.validate_interval", AsyncMock(side_effect=[
                 ValueError("FACILITY_CLOSED|Facility is closed for the requested interval."),
                 None,
             ])), \
             patch("src.app.api.v1.scheduling.record_audit", AsyncMock()):
            response = await create_exception_booking(payload, account, mock_db)

        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["exception"]["override_types"], ["facility_closed"])
        self.assertEqual(response["data"]["exception"]["reason"], "Urgent checkup")
        self.assertTrue(response["data"]["exception"]["doctor_agreement_recorded"])
        mock_db.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
