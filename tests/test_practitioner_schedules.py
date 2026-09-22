import json
import unittest
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError

from src.app.api.v1.practitioner_schedules import (
    _build_inherited_schedule,
    _format_schedule_response,
    availability_permissions,
    get_practitioner_schedule,
    reset_practitioner_schedule,
    update_practitioner_schedule,
)
from src.app.api.v1.scheduling import (
    availability_exceptions,
    availability_rules,
    replace_availability_rule,
    reset_availability_rules,
)
from src.app.models.care import Practitioner, PractitionerSchedule
from src.app.models.identity import UserAccount
from src.app.models.organization import (
    Facility,
    FacilitySchedule,
    StaffAssignment,
    StaffMember,
)
from src.app.schemas.practitioner_schedule import (
    BreakInterval,
    DaySchedule,
    PractitionerScheduleInput,
    PractitionerScheduleResetInput,
    PractitionerScheduleResponse,
    ScheduleDateException,
)
from src.app.schemas.scheduling import (
    AvailabilityRulesReplaceInput,
    AvailabilityRulesResetInput,
)


class PractitionerScheduleSchemaValidationTests(unittest.TestCase):
    def setUp(self):
        self.facility_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")

    def test_valid_schedule_input(self):
        payload = PractitionerScheduleInput(
            facility_uuid=self.facility_id,
            timezone="Asia/Kolkata",
            slot_interval_minutes=30,
            effective_from=date(2026, 10, 1),
            effective_to=date(2026, 12, 31),
            version=1,
            weekly_hours=[
                DaySchedule(
                    day_of_week="monday",
                    is_working=True,
                    start_time="09:00",
                    end_time="17:00",
                    breaks=[
                        BreakInterval(title="Tea", start_time="11:00", end_time="11:15"),
                        BreakInterval(title="Lunch", start_time="13:00", end_time="14:00"),
                    ],
                ),
                DaySchedule(
                    day_of_week="tuesday",
                    is_working=False,
                ),
            ],
            date_exceptions=[
                ScheduleDateException(
                    date=date(2026, 10, 15),
                    exception_type="leave",
                    reason="Medical conference",
                ),
                ScheduleDateException(
                    date=date(2026, 10, 20),
                    exception_type="custom_hours",
                    start_time="10:00",
                    end_time="14:00",
                    reason="Half day morning",
                ),
            ],
        )
        self.assertEqual(payload.timezone, "Asia/Kolkata")
        self.assertEqual(len(payload.weekly_hours), 2)
        self.assertEqual(len(payload.weekly_hours[0].breaks), 2)
        self.assertEqual(len(payload.date_exceptions), 2)

    def test_legacy_batch_contract_accepts_doctor_schedule_payload(self):
        payload = AvailabilityRulesReplaceInput.model_validate({
            "facility_uuid": str(self.facility_id),
            "practitioner_uuid": "01a0c05e-d6b7-7663-a117-5b77f02f9bf5",
            "rules": [{
                "day_of_week": 2,
                "start_local_time": "08:00",
                "end_local_time": "20:00",
                "slot_duration_minutes": 30,
            }],
            "exceptions": [],
            "effective_from": "2026-09-22",
            "force": False,
        })
        self.assertEqual(payload.rules[0].day_of_week, 2)
        self.assertEqual(payload.rules[0].start_local_time, time(8))

    def test_break_outside_working_hours_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            DaySchedule(
                day_of_week="monday",
                is_working=True,
                start_time="09:00",
                end_time="17:00",
                breaks=[
                    BreakInterval(title="Early Break", start_time="08:00", end_time="08:30")
                ],
            )
        self.assertIn("falls outside working hours", str(ctx.exception))

    def test_overlapping_breaks_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            DaySchedule(
                day_of_week="monday",
                is_working=True,
                start_time="09:00",
                end_time="17:00",
                breaks=[
                    BreakInterval(title="Break 1", start_time="12:00", end_time="13:30"),
                    BreakInterval(title="Break 2", start_time="13:00", end_time="14:00"),
                ],
            )
        self.assertIn("Overlapping breaks detected", str(ctx.exception))

    def test_unsupported_overnight_working_hours_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            DaySchedule(
                day_of_week="monday",
                is_working=True,
                start_time="22:00",
                end_time="06:00",
            )
        self.assertIn("UNSUPPORTED_OVERNIGHT_INTERVAL", str(ctx.exception))

    def test_unsupported_overnight_break_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            BreakInterval(title="Night Break", start_time="23:30", end_time="00:30")
        self.assertIn("UNSUPPORTED_OVERNIGHT_INTERVAL", str(ctx.exception))

    def test_unsupported_overnight_date_exception_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            ScheduleDateException(
                date=date(2026, 10, 15),
                exception_type="custom_hours",
                start_time="23:00",
                end_time="03:00",
            )
        self.assertIn("UNSUPPORTED_OVERNIGHT_INTERVAL", str(ctx.exception))

    def test_overlapping_date_exceptions_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PractitionerScheduleInput(
                facility_uuid=self.facility_id,
                weekly_hours=[],
                date_exceptions=[
                    ScheduleDateException(date=date(2026, 10, 15), exception_type="leave"),
                    ScheduleDateException(date=date(2026, 10, 15), exception_type="vacation"),
                ],
            )
        self.assertIn("Multiple overlapping date exceptions", str(ctx.exception))

    def test_invalid_timezone_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            PractitionerScheduleInput(
                facility_uuid=self.facility_id,
                timezone="Invalid/Timezone",
                weekly_hours=[],
            )
        self.assertIn("valid IANA timezone", str(ctx.exception))


class PractitionerScheduleEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.org_id = UUID("01a0a022-b2a0-7000-8000-000000000001")
        self.facility_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
        self.doctor_user_id = UUID("01a0a022-b2aa-7509-aecb-3166583df954")
        self.other_doctor_user_id = UUID("01a0a022-b2aa-7509-aecb-3166583df955")
        self.admin_user_id = UUID("01a0a022-b2aa-7509-aecb-3166583df999")

        self.doctor_practitioner_id = UUID("01a0a022-b31b-728f-b7ae-b06211a65893")
        self.other_practitioner_id = UUID("01a0a022-b31b-728f-b7ae-b06211a65894")

        self.doctor_account = SimpleNamespace(id=self.doctor_user_id)
        self.admin_account = SimpleNamespace(id=self.admin_user_id)

        self.facility = SimpleNamespace(
            id=self.facility_id,
            organization_id=self.org_id,
            name="Main Clinic",
            is_active=True,
        )
        self.doctor_practitioner = SimpleNamespace(
            id=self.doctor_practitioner_id,
            organization_id=self.org_id,
            person_name="Dr. Smith",
            user_account_id=self.doctor_user_id,
            is_active=True,
        )
        self.other_practitioner = SimpleNamespace(
            id=self.other_practitioner_id,
            organization_id=self.org_id,
            person_name="Dr. Jones",
            user_account_id=self.other_doctor_user_id,
            is_active=True,
        )

        self.facility_schedule = SimpleNamespace(
            id=UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0f"),
            facility_id=self.facility_id,
            operating_start="08:00",
            operating_end="18:00",
            slot_interval_minutes=30,
            days_of_week=json.dumps(["monday", "tuesday", "wednesday", "thursday", "friday"]),
            timezone="Asia/Kolkata",
        )

    def _make_db(
        self,
        staff_member: Any,
        assignments: list[Any],
        practitioner: Any,
        practitioner_override: Any = None,
        facility_schedule: Any = None,
    ):
        mock_db = SimpleNamespace(
            scalar=AsyncMock(),
            scalars=AsyncMock(),
            add=Mock(),
            commit=AsyncMock(),
            refresh=AsyncMock(),
        )

        async def scalar_mock(stmt):
            entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
            if entity == StaffMember:
                return staff_member
            if entity == Facility:
                return self.facility
            if entity == Practitioner:
                return practitioner
            if entity == PractitionerSchedule:
                return practitioner_override
            if entity == FacilitySchedule:
                return facility_schedule
            return None

        async def scalars_mock(stmt):
            entity = stmt.column_descriptions[0]["entity"] if stmt.column_descriptions else None
            if entity == StaffAssignment:
                return SimpleNamespace(all=lambda: assignments)
            return SimpleNamespace(all=lambda: [])

        mock_db.scalar.side_effect = scalar_mock
        mock_db.scalars.side_effect = scalars_mock
        return mock_db

    async def test_batch_availability_contract_routes_to_practitioner_schedule(self):
        staff_member_id = UUID("01a0c05e-d6b7-7663-a117-5b77f02f9bf5")
        payload = AvailabilityRulesReplaceInput.model_validate({
            "facility_uuid": str(self.facility_id),
            "practitioner_uuid": str(staff_member_id),
            "rules": [{
                "day_of_week": 2,
                "start_local_time": "08:00",
                "end_local_time": "20:00",
                "slot_duration_minutes": 30,
            }],
            "exceptions": [],
            "effective_from": "2026-09-22",
            "force": False,
        })

        with (
            patch(
                "src.app.api.v1.scheduling._scope",
                new=AsyncMock(return_value=(SimpleNamespace(id=self.org_id), self.facility)),
            ),
            patch(
                "src.app.api.v1.scheduling._canonical_practitioner_id",
                new=AsyncMock(return_value=self.doctor_practitioner_id),
            ),
            patch("src.app.api.v1.scheduling._facility_timezone", new=AsyncMock(return_value=ZoneInfo("Asia/Kolkata"))),
            patch("src.app.api.v1.scheduling.update_practitioner_schedule", new_callable=AsyncMock) as save,
        ):
            save.return_value = {"success": True, "data": {}, "meta": {}}
            await replace_availability_rule(payload, self.doctor_account, SimpleNamespace())

        forwarded = save.call_args.kwargs
        self.assertEqual(forwarded["practitioner_uuid"], self.doctor_practitioner_id)
        self.assertEqual(forwarded["payload"].weekly_hours[0].day_of_week, "tuesday")
        self.assertEqual(forwarded["payload"].weekly_hours[0].start_time, "08:00")

    async def test_legacy_reads_return_saved_practitioner_override(self):
        schedule = SimpleNamespace(
            id=UUID(int=50),
            practitioner_id=self.doctor_practitioner_id,
            slot_interval_minutes=30,
            effective_from=date(2026, 9, 22),
            effective_to=None,
            weekly_hours=[{
                "day_of_week": "tuesday",
                "is_working": True,
                "start_time": "08:00",
                "end_time": "20:00",
            }],
            date_exceptions=[{
                "date": "2026-09-29",
                "exception_type": "leave",
                "reason": "Conference",
            }],
        )
        db = SimpleNamespace(scalar=AsyncMock(return_value=schedule))

        with patch(
            "src.app.api.v1.scheduling._scope",
            new=AsyncMock(return_value=(SimpleNamespace(id=self.org_id), self.facility)),
        ):
            rules = await availability_rules(
                self.doctor_account, db, self.facility_id, self.doctor_practitioner_id
            )
            exceptions = await availability_exceptions(
                self.facility_id, self.doctor_account, db, self.doctor_practitioner_id
            )

        self.assertEqual(rules["data"]["items"][0]["day_of_week"], 2)
        self.assertEqual(exceptions["data"]["items"][0]["start_time"], "2026-09-29T00:00:00")

    async def test_legacy_reset_routes_to_inherited_schedule_reset(self):
        staff_member_id = UUID("01a0c05e-d6b7-7663-a117-5b77f02f9bf5")
        payload = AvailabilityRulesResetInput(
            facility_uuid=self.facility_id,
            practitioner_uuid=staff_member_id,
        )

        with (
            patch(
                "src.app.api.v1.scheduling._scope",
                new=AsyncMock(return_value=(SimpleNamespace(id=self.org_id), self.facility)),
            ),
            patch(
                "src.app.api.v1.scheduling._canonical_practitioner_id",
                new=AsyncMock(return_value=self.doctor_practitioner_id),
            ),
            patch(
                "src.app.api.v1.scheduling.reset_practitioner_schedule",
                new_callable=AsyncMock,
            ) as reset,
        ):
            reset.return_value = {"success": True, "data": {}, "meta": {}}
            await reset_availability_rules(payload, self.doctor_account, SimpleNamespace())

        self.assertEqual(reset.call_args.kwargs["practitioner_uuid"], self.doctor_practitioner_id)

    async def test_inheritance_returns_facility_defaults_when_no_override(self):
        staff_member = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000010"), organization_id=self.org_id, is_active=True)
        assignment = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000020"), role_code="practitioner", facility_id=self.facility_id, is_active=True)

        mock_db = self._make_db(
            staff_member=staff_member,
            assignments=[assignment],
            practitioner=self.doctor_practitioner,
            practitioner_override=None,
            facility_schedule=self.facility_schedule,
        )

        response = await get_practitioner_schedule(
            practitioner_uuid=self.doctor_practitioner_id,
            account=self.doctor_account,
            db=mock_db,
            facility_uuid=self.facility_id,
        )

        self.assertTrue(response["success"])
        data = response["data"]
        self.assertTrue(data["inherited"])
        self.assertFalse(data["is_override"])
        self.assertEqual(data["version"], 0)
        self.assertEqual(data["timezone"], "Asia/Kolkata")
        self.assertEqual(data["slot_interval_minutes"], 30)
        self.assertEqual(len(data["weekly_hours"]), 7)

        monday = next(d for d in data["weekly_hours"] if d["day_of_week"] == "monday")
        self.assertTrue(monday["is_working"])
        self.assertEqual(monday["start_time"], "08:00")
        self.assertEqual(monday["end_time"], "18:00")

        sunday = next(d for d in data["weekly_hours"] if d["day_of_week"] == "sunday")
        self.assertFalse(sunday["is_working"])

    async def test_admin_can_update_any_practitioner_schedule(self):
        admin_staff = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000011"), organization_id=self.org_id, is_active=True)
        admin_assignment = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000021"), role_code="administrator", facility_id=None, is_active=True)

        mock_db = self._make_db(
            staff_member=admin_staff,
            assignments=[admin_assignment],
            practitioner=self.doctor_practitioner,
            practitioner_override=None,
            facility_schedule=self.facility_schedule,
        )

        payload = PractitionerScheduleInput(
            facility_uuid=self.facility_id,
            timezone="Asia/Kolkata",
            slot_interval_minutes=20,
            effective_from=date(2026, 10, 1),
            version=1,
            weekly_hours=[
                DaySchedule(
                    day_of_week="monday",
                    is_working=True,
                    start_time="10:00",
                    end_time="16:00",
                    breaks=[BreakInterval(title="Lunch", start_time="13:00", end_time="14:00")],
                )
            ],
            date_exceptions=[],
        )

        with patch("src.app.api.v1.practitioner_schedules.record_audit", new_callable=AsyncMock) as mock_audit:
            response = await update_practitioner_schedule(
                practitioner_uuid=self.doctor_practitioner_id,
                payload=payload,
                account=self.admin_account,
                db=mock_db,
                facility_uuid=self.facility_id,
            )

        self.assertTrue(response["success"])
        data = response["data"]
        self.assertTrue(data["is_override"])
        self.assertFalse(data["inherited"])
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["slot_interval_minutes"], 20)
        mock_db.add.assert_called_once()
        mock_audit.assert_called_once()

    async def test_admin_schedule_permissions_include_override_version(self):
        admin_staff = SimpleNamespace(
            id=UUID("01a0a022-b2a0-7000-8000-000000000011"),
            organization_id=self.org_id,
            is_active=True,
        )
        admin_assignment = SimpleNamespace(
            role_code="organization_admin",
            facility_id=None,
            is_active=True,
        )
        override = SimpleNamespace(
            version=3,
            effective_from=date(2026, 9, 22),
        )
        mock_db = self._make_db(
            staff_member=admin_staff,
            assignments=[admin_assignment],
            practitioner=self.doctor_practitioner,
            practitioner_override=override,
        )

        response = await availability_permissions(
            facility_uuid=self.facility_id,
            practitioner_uuid=self.doctor_practitioner_id,
            account=self.admin_account,
            db=mock_db,
        )

        data = response["data"]
        self.assertTrue(data["permissions"]["can_edit_other_schedules"])
        self.assertTrue(data["permissions"]["can_force_schedule_conflict"])
        self.assertEqual(data["current_version"], "3")
        self.assertTrue(data["has_overrides"])

    async def test_doctor_can_update_own_schedule(self):
        staff_member = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000010"), organization_id=self.org_id, is_active=True)
        doctor_assignment = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000020"), role_code="doctor", facility_id=self.facility_id, is_active=True)

        mock_db = self._make_db(
            staff_member=staff_member,
            assignments=[doctor_assignment],
            practitioner=self.doctor_practitioner,
            practitioner_override=None,
            facility_schedule=self.facility_schedule,
        )

        payload = PractitionerScheduleInput(
            facility_uuid=self.facility_id,
            timezone="Asia/Kolkata",
            slot_interval_minutes=15,
            weekly_hours=[
                DaySchedule(
                    day_of_week="monday",
                    is_working=True,
                    start_time="09:00",
                    end_time="15:00",
                )
            ],
        )

        with patch("src.app.api.v1.practitioner_schedules.record_audit", new_callable=AsyncMock):
            response = await update_practitioner_schedule(
                practitioner_uuid=self.doctor_practitioner_id,
                payload=payload,
                account=self.doctor_account,
                db=mock_db,
                facility_uuid=self.facility_id,
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["slot_interval_minutes"], 15)

    async def test_doctor_cannot_force_schedule_conflicts(self):
        staff_member = SimpleNamespace(
            id=UUID("01a0a022-b2a0-7000-8000-000000000010"),
            organization_id=self.org_id,
            is_active=True,
        )
        doctor_assignment = SimpleNamespace(
            role_code="doctor",
            facility_id=self.facility_id,
            is_active=True,
        )
        mock_db = self._make_db(
            staff_member=staff_member,
            assignments=[doctor_assignment],
            practitioner=self.doctor_practitioner,
        )

        with self.assertRaises(HTTPException) as ctx:
            await update_practitioner_schedule(
                practitioner_uuid=self.doctor_practitioner_id,
                payload=PractitionerScheduleInput(
                    facility_uuid=self.facility_id,
                    weekly_hours=[],
                ),
                account=self.doctor_account,
                db=mock_db,
                facility_uuid=self.facility_id,
                force=True,
            )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(
            ctx.exception.detail["code"], "SCHEDULE_FORCE_PERMISSION_REQUIRED"
        )

    async def test_doctor_cannot_update_another_doctor_schedule(self):
        staff_member = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000010"), organization_id=self.org_id, is_active=True)
        doctor_assignment = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000020"), role_code="doctor", facility_id=self.facility_id, is_active=True)

        mock_db = self._make_db(
            staff_member=staff_member,
            assignments=[doctor_assignment],
            practitioner=self.other_practitioner,
            practitioner_override=None,
            facility_schedule=self.facility_schedule,
        )

        payload = PractitionerScheduleInput(
            facility_uuid=self.facility_id,
            timezone="Asia/Kolkata",
            weekly_hours=[],
        )

        with self.assertRaises(HTTPException) as ctx:
            await update_practitioner_schedule(
                practitioner_uuid=self.other_practitioner_id,
                payload=payload,
                account=self.doctor_account,
                db=mock_db,
                facility_uuid=self.facility_id,
            )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail["code"], "FORBIDDEN")
        self.assertIn("manage their own schedule", ctx.exception.detail["message"])

    async def test_stale_version_conflict_rejection(self):
        admin_staff = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000011"), organization_id=self.org_id, is_active=True)
        admin_assignment = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000021"), role_code="administrator", facility_id=None, is_active=True)

        existing_override = SimpleNamespace(
            id=UUID("01a0a022-b2c1-76e3-a91d-49f5568eca99"),
            organization_id=self.org_id,
            facility_id=self.facility_id,
            practitioner_id=self.doctor_practitioner_id,
            timezone="Asia/Kolkata",
            slot_interval_minutes=30,
            effective_from=date(2026, 9, 1),
            effective_to=None,
            version=3,  # DB is currently at version 3
            is_active=True,
            weekly_hours=[],
            date_exceptions=[],
            is_override=True,
            updated_at=None,
            updated_by_user_id=None,
        )

        mock_db = self._make_db(
            staff_member=admin_staff,
            assignments=[admin_assignment],
            practitioner=self.doctor_practitioner,
            practitioner_override=existing_override,
            facility_schedule=self.facility_schedule,
        )

        payload = PractitionerScheduleInput(
            facility_uuid=self.facility_id,
            version=2,  # Client submits stale version 2
            weekly_hours=[],
        )

        with self.assertRaises(HTTPException) as ctx:
            await update_practitioner_schedule(
                practitioner_uuid=self.doctor_practitioner_id,
                payload=payload,
                account=self.admin_account,
                db=mock_db,
                facility_uuid=self.facility_id,
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "STALE_VERSION")

    async def test_reset_to_defaults_preserves_history_and_ends_override(self):
        admin_staff = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000011"), organization_id=self.org_id, is_active=True)
        admin_assignment = SimpleNamespace(id=UUID("01a0a022-b2a0-7000-8000-000000000021"), role_code="administrator", facility_id=None, is_active=True)

        existing_override = SimpleNamespace(
            id=UUID("01a0a022-b2c1-76e3-a91d-49f5568eca99"),
            organization_id=self.org_id,
            facility_id=self.facility_id,
            practitioner_id=self.doctor_practitioner_id,
            version=2,
            is_active=True,
            effective_from=date(2026, 9, 1),
            effective_to=None,
            updated_at=None,
            updated_by_user_id=None,
        )

        mock_db = self._make_db(
            staff_member=admin_staff,
            assignments=[admin_assignment],
            practitioner=self.doctor_practitioner,
            practitioner_override=existing_override,
            facility_schedule=self.facility_schedule,
        )

        payload = PractitionerScheduleResetInput(
            effective_date=date(2026, 10, 1)
        )

        with patch("src.app.api.v1.practitioner_schedules.record_audit", new_callable=AsyncMock) as mock_audit:
            response = await reset_practitioner_schedule(
                practitioner_uuid=self.doctor_practitioner_id,
                payload=payload,
                account=self.admin_account,
                db=mock_db,
                facility_uuid=self.facility_id,
            )

        self.assertTrue(response["success"])
        self.assertTrue(response["data"]["inherited"])
        self.assertFalse(response["data"]["is_override"])

        # Check existing override was marked inactive and ended at effective date
        self.assertFalse(existing_override.is_active)
        self.assertEqual(existing_override.effective_to, date(2026, 10, 1))
        self.assertEqual(existing_override.updated_by_user_id, self.admin_user_id)

        mock_db.commit.assert_called_once()
        mock_audit.assert_called_once()
