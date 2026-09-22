import json
import unittest
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

from src.app.api.v1.frontend_compat import availability
from src.app.core.availability import (
    ACTIVE_APPOINTMENT_STATUSES,
    _categorize_state,
    _overlaps,
    evaluate_availability,
    find_facility_schedule_conflicts,
    find_schedule_conflicts,
    rules_from_facility_schedule,
    validate_interval,
)
from src.app.core.timezones import local_datetime
from src.app.models.care import (
    Appointment,
    PractitionerAvailabilityException,
    PractitionerAvailabilityRule,
    PractitionerSchedule,
)
from src.app.models.organization import (
    FacilityResource,
    FacilitySchedule,
    ProtectedPeriod,
)

FACILITY_ID = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
PRACTITIONER_ID = UUID("01a0a022-b31b-728f-b7ae-b06211a65893")
OTHER_FACILITY_ID = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca99")
ORGANIZATION_ID = UUID("01a0a022-b2aa-7509-aecb-3166583df954")
TZ = ZoneInfo("Asia/Kolkata")


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _AvailabilityDb:
    def __init__(self, *, rules=True, practitioner_schedule=None, exceptions=None, appointments=None, periods=None):
        self.schedule = SimpleNamespace(
            operating_start="08:00",
            operating_end="20:00",
            slot_interval_minutes=30,
            days_of_week=json.dumps(
                ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
            ),
            timezone="Asia/Kolkata",
        )
        self.practitioner_schedule = practitioner_schedule
        self.rules = [
            SimpleNamespace(
                weekday=weekday,
                start_time=time(8),
                end_time=time(20),
                slot_duration_minutes=30,
                valid_from=date(2026, 9, 21),
                valid_until=None,
                resource_id=None,
            )
            for weekday in range(6)
        ] if rules else []
        self.exceptions = exceptions or []
        self.periods = periods or []
        self.appointments = appointments or []

    async def scalar(self, statement):
        desc = statement.column_descriptions[0]
        entity = desc.get("entity")
        if entity is FacilitySchedule:
            return self.schedule
        if entity is PractitionerSchedule:
            return self.practitioner_schedule
        if entity is FacilityResource:
            return None
        return None

    async def scalars(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        values = {
            PractitionerAvailabilityRule: self.rules,
            PractitionerAvailabilityException: self.exceptions,
            ProtectedPeriod: self.periods,
            Appointment: self.appointments,
        }.get(entity, [])
        return _Rows(values)


class IntervalAndStateCategorizationUnitTests(unittest.TestCase):
    """
    Focused unit tests for the foundational primitives used throughout the
    resolver.  These pin down half-open interval semantics and the explicit
    AVAILABLE / BOOKED / BLOCKED / CLOSED state mapping so the frontend never
    has to re-derive business rules from ad-hoc status strings.
    """

    def test_half_open_interval_overlaps_adjacent_no_overlap(self):
        # [09:00, 09:30) abuts [09:30, 10:00) at the boundary — no overlap.
        a_start = datetime(2026, 9, 22, 9, 0, tzinfo=TZ)
        a_end = a_start + timedelta(minutes=30)
        b_start = a_end
        b_end = b_start + timedelta(minutes=30)
        self.assertFalse(_overlaps(a_start, a_end, b_start, b_end))
        self.assertFalse(_overlaps(b_start, b_end, a_start, a_end))

    def test_half_open_interval_partial_overlap_detected(self):
        # [09:00, 09:30) overlaps [09:15, 09:45) regardless of argument order.
        a_start = datetime(2026, 9, 22, 9, 0, tzinfo=TZ)
        a_end = a_start + timedelta(minutes=30)
        b_start = a_start + timedelta(minutes=15)
        b_end = b_start + timedelta(minutes=30)
        self.assertTrue(_overlaps(a_start, a_end, b_start, b_end))
        self.assertTrue(_overlaps(b_start, b_end, a_start, a_end))

    def test_half_open_interval_fully_contained_overlaps(self):
        outer_start = datetime(2026, 9, 22, 9, 0, tzinfo=TZ)
        outer_end = outer_start + timedelta(hours=1)
        inner_start = outer_start + timedelta(minutes=10)
        inner_end = inner_start + timedelta(minutes=10)
        self.assertTrue(_overlaps(outer_start, outer_end, inner_start, inner_end))

    def test_any_partial_overlap_triggers_even_for_one_minute(self):
        # A booking that strays even 1 minute into an unavailable window must
        # be rejected per the “Reject a booking if any part overlaps an
        # unavailable interval” rule.
        slot_start = datetime(2026, 9, 22, 9, 29, tzinfo=TZ)
        slot_end = slot_start + timedelta(minutes=30)
        unavail_start = datetime(2026, 9, 22, 9, 0, tzinfo=TZ)
        unavail_end = unavail_start + timedelta(minutes=30)
        self.assertTrue(_overlaps(slot_start, slot_end, unavail_start, unavail_end))

    def test_categorize_state_maps_explicit_four_buckets(self):
        cases = {
            "AVAILABLE": "AVAILABLE",
            "BOOKED": "BOOKED",
            "CROSS_FACILITY_BOOKED": "BOOKED",
            "RESOURCE_BOOKED": "BOOKED",
            "FULLY_BOOKED": "BOOKED",
            "FACILITY_CLOSED": "CLOSED",
            "BREAK": "BLOCKED",
            "PROTECTED_PERIOD": "BLOCKED",
            "DOCTOR_UNAVAILABLE": "BLOCKED",
            "PAST": "BLOCKED",
            "MISSING_CONFIGURATION": "BLOCKED",
            "__UNKNOWN__": "BLOCKED",
        }
        for status, expected_state in cases.items():
            with self.subTest(status=status):
                self.assertEqual(_categorize_state(status), expected_state)


class ResolverInheritanceAndHierarchyTests(unittest.IsolatedAsyncioTestCase):
    """
    Exercises the hierarchy required by the spec:

      1. Facility opening bounds / hard closures constrain everything.
      2. Date-specific doctor exceptions override
      3. Weekly doctor rules (PractitionerSchedule → legacy rules) override
      4. Inherited practice working hours.

    Each layer is asserted independently and in combination.
    """

    async def test_inherited_practice_hours_when_no_doctor_rules(self):
        # No legacy rules AND no PractitionerSchedule → doctor plan falls
        # back to the facility schedule (practice working hours).
        day = date(2026, 9, 21)  # Monday
        db = _AvailabilityDb(rules=False)
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=day,
        )
        self.assertEqual(result["state"], "AVAILABLE")
        self.assertEqual(result["status"], "AVAILABLE")
        first = result["slots"][0]
        self.assertEqual(first["state"], "AVAILABLE")
        self.assertEqual(first["status"], "AVAILABLE")
        # Inherited 08:00 facility opening with 30-min step → 24 usable slots
        self.assertEqual(len(result["usable_slots"]), 24)

    async def test_weekly_doctor_rules_override_inherited_practice_hours(self):
        # Legacy PractitionerAvailabilityRule narrows hours vs facility.
        db = _AvailabilityDb(rules=False)
        db.rules = [
            SimpleNamespace(
                weekday=0,  # Monday
                start_time=time(10),
                end_time=time(16),
                slot_duration_minutes=30,
                valid_from=date(2026, 1, 1),
                valid_until=None,
                resource_id=None,
            )
        ]
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=date(2026, 9, 21),  # Monday
        )
        self.assertEqual(result["state"], "AVAILABLE")
        starts = [s["start"][11:16] for s in result["slots"]]
        self.assertEqual(starts[0], "10:00")
        self.assertEqual(starts[-1], "15:30")
        self.assertEqual(len(result["usable_slots"]), 12)

    async def test_practitioner_schedule_overrides_both_facility_and_legacy_rules(self):
        # PractitionerSchedule.weekly_hours must win over legacy rules AND the
        # facility default operating hours.
        ps = SimpleNamespace(
            id=UUID(int=1),
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            slot_interval_minutes=30,
            timezone="Asia/Kolkata",
            is_active=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            version=1,
            weekly_hours=[
                {
                    "day_of_week": "monday",
                    "is_working": True,
                    "start_time": "09:30",
                    "end_time": "12:30",
                    "breaks": [],
                }
            ],
            date_exceptions=[],
        )
        db = _AvailabilityDb(rules=True, practitioner_schedule=ps)
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=date(2026, 9, 21),  # Monday
        )
        self.assertEqual(result["state"], "AVAILABLE")
        starts = [s["start"][11:16] for s in result["slots"]]
        self.assertEqual(starts[0], "09:30")
        self.assertEqual(starts[-1], "12:00")
        self.assertEqual(len(result["usable_slots"]), 6)

    async def test_date_exception_custom_hours_overrides_weekly_schedule(self):
        # Date-specific custom hours beat the weekly doctor plan.
        target = date(2026, 9, 22)  # Tuesday
        ps = SimpleNamespace(
            id=UUID(int=1),
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            slot_interval_minutes=30,
            timezone="Asia/Kolkata",
            is_active=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            version=1,
            weekly_hours=[
                {
                    "day_of_week": "tuesday",
                    "is_working": True,
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "breaks": [],
                }
            ],
            date_exceptions=[
                {
                    "date": target.isoformat(),
                    "exception_type": "custom_hours",
                    "start_time": "13:00",
                    "end_time": "15:00",
                    "reason": "Half day clinic",
                }
            ],
        )
        db = _AvailabilityDb(rules=False, practitioner_schedule=ps)
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=target,
        )
        self.assertEqual(result["state"], "AVAILABLE")
        starts = [s["start"][11:16] for s in result["slots"]]
        self.assertEqual(starts[0], "13:00")
        self.assertEqual(starts[-1], "14:30")
        self.assertEqual(len(result["usable_slots"]), 4)

    async def test_date_exception_leave_overrides_everything_else(self):
        # A full-day leave exception must make the entire day
        # DOCTOR_UNAVAILABLE even though weekly schedule is working.
        target = date(2026, 9, 22)  # Tuesday
        ps = SimpleNamespace(
            id=UUID(int=1),
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            slot_interval_minutes=30,
            timezone="Asia/Kolkata",
            is_active=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            version=1,
            weekly_hours=[
                {
                    "day_of_week": "tuesday",
                    "is_working": True,
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "breaks": [],
                }
            ],
            date_exceptions=[
                {
                    "date": target.isoformat(),
                    "exception_type": "leave",
                    "reason": "Medical conference",
                }
            ],
        )
        db = _AvailabilityDb(rules=False, practitioner_schedule=ps)
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=target,
        )
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["status"], "DOCTOR_UNAVAILABLE")
        self.assertEqual(len(result["slots"]), 0)
        self.assertEqual(len(result["usable_slots"]), 0)


class FacilityClosedDayAndHardBoundsTests(unittest.IsolatedAsyncioTestCase):
    """
    Pinpoints the facility-level hard-close semantics:
      * Sundays are not in the facility days_of_week → FACILITY_CLOSED.
      * No slot intervals are emitted for closed days.
      * The coarse-grained `state` for a closed day is `CLOSED`.
    """

    async def test_sunday_is_closed_and_reports_closed_state(self):
        # Sunday 2026-09-20 → day 6; facility schedule only has Mon-Sat.
        db = _AvailabilityDb()
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=date(2026, 9, 20),
        )
        self.assertEqual(result["state"], "CLOSED")
        self.assertEqual(result["status"], "FACILITY_CLOSED")
        self.assertTrue(result["facility_open"] is False)
        self.assertEqual(len(result["slots"]), 0)

    async def test_doctor_hours_outside_facility_bounds_are_blocked(self):
        # Facility is 08–20 but PractitionerSchedule is 06–10 and 18–22;
        # only the intersection with facility hours is available.
        ps = SimpleNamespace(
            id=UUID(int=1),
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            slot_interval_minutes=30,
            timezone="Asia/Kolkata",
            is_active=True,
            effective_from=date(2026, 1, 1),
            effective_to=None,
            version=1,
            weekly_hours=[
                {
                    "day_of_week": "monday",
                    "is_working": True,
                    "start_time": "06:00",
                    "end_time": "22:00",
                    "breaks": [],
                }
            ],
            date_exceptions=[],
        )
        db = _AvailabilityDb(rules=False, practitioner_schedule=ps)
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=date(2026, 9, 21),  # Monday
        )
        self.assertEqual(result["state"], "AVAILABLE")
        starts = [s["start"][11:16] for s in result["slots"]]
        # Clamped to facility 08:00 → 20:00 bounds
        self.assertEqual(starts[0], "08:00")
        self.assertEqual(starts[-1], "19:30")
        self.assertEqual(len(result["usable_slots"]), 24)


class CrossFacilityAndResourceConflictTests(unittest.IsolatedAsyncioTestCase):
    """
    Cross-facility double-booking protection, resource/room conflict
    detection, and the concurrent-booking semantics enforced by the unified
    resolver.
    """

    async def test_cross_facility_double_booking_is_rejected(self):
        # A BOOKED appointment for the same physical practitioner at a
        # *different* facility must surface as CROSS_FACILITY_BOOKED so the
        # frontend cannot silently double-book a doctor across sites.
        overlap_start = local_datetime(date(2026, 9, 21), time(10, 0), TZ)
        overlap_end = overlap_start + timedelta(minutes=30)
        other_facility_appt = SimpleNamespace(
            id=UUID(int=99),
            practitioner_id=PRACTITIONER_ID,
            facility_id=OTHER_FACILITY_ID,
            resource_id=None,
            scheduled_start=overlap_start,
            scheduled_end=overlap_end,
        )
        db = _AvailabilityDb(rules=True, appointments=[other_facility_appt])
        result = await evaluate_availability(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            day=date(2026, 9, 21),
        )
        conflicting = [s for s in result["slots"] if s["status"] == "CROSS_FACILITY_BOOKED"]
        self.assertGreaterEqual(len(conflicting), 1)
        for slot in conflicting:
            self.assertEqual(slot["state"], "BOOKED")
        # validate_interval must also raise the same code
        with self.assertRaises(ValueError) as ctx:
            await validate_interval(
                db,
                organization_id=ORGANIZATION_ID,
                facility_id=FACILITY_ID,
                practitioner_id=PRACTITIONER_ID,
                start=overlap_start,
                end=overlap_end,
            )
        self.assertIn("CROSS_FACILITY_BOOKED", str(ctx.exception))

    async def test_resource_room_booked_conflict_detected(self):
        room_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0e")
        # Legacy rule pins practitioner to that room
        db = _AvailabilityDb(rules=False)
        db.rules = [
            SimpleNamespace(
                weekday=0,
                start_time=time(8),
                end_time=time(20),
                slot_duration_minutes=30,
                valid_from=date(2026, 1, 1),
                valid_until=None,
                resource_id=room_id,
            )
        ]
        overlap_start = local_datetime(date(2026, 9, 21), time(10, 0), TZ)
        overlap_end = overlap_start + timedelta(minutes=30)
        occupant = SimpleNamespace(
            id=UUID(int=77),
            practitioner_id=UUID(int=42),
            facility_id=FACILITY_ID,
            resource_id=room_id,
            scheduled_start=overlap_start,
            scheduled_end=overlap_end,
        )
        db.appointments = [occupant]
        db.resources = {room_id: SimpleNamespace(id=room_id, is_active=True, facility_id=FACILITY_ID)}
        with patch("src.app.core.availability._context") as make_ctx:
            base_ctx = {
                "schedule": db.schedule,
                "tz": TZ,
                "practitioner_schedule": None,
                "rules": db.rules,
                "exceptions": [],
                "periods": [],
                "resources": db.resources,
                "appointments": db.appointments,
            }
            make_ctx.return_value = base_ctx
            with self.assertRaises(ValueError) as ctx:
                await validate_interval(
                    db,
                    organization_id=ORGANIZATION_ID,
                    facility_id=FACILITY_ID,
                    practitioner_id=PRACTITIONER_ID,
                    start=overlap_start,
                    end=overlap_end,
                )
        self.assertIn("RESOURCE_BOOKED", str(ctx.exception))


class ScheduleEditConflictDetectionTests(unittest.IsolatedAsyncioTestCase):
    """
    Schedule edits (doctor weekly hours, facility operating hours, legacy
    rule/exception edits) must identify affected future appointments and
    refuse to proceed until staff explicitly resolves the conflict list.
    This is the server-side guard against silent cancellation/movement.
    """

    def _appt(self, appt_id, local_day, local_start_hour, minutes=30):
        start = local_datetime(local_day, time(local_start_hour, 0), TZ)
        end = start + timedelta(minutes=minutes)
        return SimpleNamespace(
            id=appt_id,
            practitioner_id=PRACTITIONER_ID,
            patient_id=UUID(int=1000 + int(appt_id)),
            scheduled_start=start,
            scheduled_end=end,
        )

    def _mock_scalars(self, appointments_by_entity):
        async def _scalars(statement):
            entity = statement.column_descriptions[0].get("entity")
            return _Rows(appointments_by_entity.get(entity, []))
        return _scalars

    async def test_find_schedule_conflicts_flags_doctor_now_off_day(self):
        # A future appointment booked on a Tuesday when the proposed weekly
        # schedule removes Tuesday working hours entirely must be reported.
        tuesday = date(2026, 10, 6)
        appt = self._appt(UUID(int=1), tuesday, 10)

        db = SimpleNamespace(scalar=AsyncMock(return_value=None), scalars=AsyncMock())
        db.scalars.side_effect = self._mock_scalars({Appointment: [appt]})

        conflicts = await find_schedule_conflicts(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            proposed_weekly_hours=[
                {
                    "day_of_week": "monday",
                    "is_working": True,
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "breaks": [],
                }
            ],
            proposed_date_exceptions=[],
            proposed_timezone="Asia/Kolkata",
            effective_from=date(2026, 10, 1),
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["appointment_uuid"], str(appt.id))
        self.assertEqual(conflicts[0]["conflict_reason"], "DOCTOR_UNAVAILABLE")

    async def test_find_facility_schedule_conflicts_flags_new_closed_day(self):
        # Facility removes Sunday from operating days; existing Sunday
        # appointments must be surfaced in the conflict list so they are
        # never silently orphaned.
        sunday = date(2026, 10, 4)  # Sunday
        appt = self._appt(UUID(int=11), sunday, 11)

        db = SimpleNamespace(
            scalar=AsyncMock(return_value=None),
            scalars=AsyncMock(),
        )
        db.scalars.side_effect = self._mock_scalars({Appointment: [appt], ProtectedPeriod: []})

        conflicts = await find_facility_schedule_conflicts(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            proposed_operating_start="08:00",
            proposed_operating_end="20:00",
            proposed_days_of_week=["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
            proposed_timezone="Asia/Kolkata",
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["appointment_uuid"], str(appt.id))
        self.assertEqual(conflicts[0]["practitioner_uuid"], str(PRACTITIONER_ID))
        self.assertEqual(conflicts[0]["conflict_reason"], "FACILITY_CLOSED")

    async def test_find_facility_schedule_conflicts_shorter_operating_hours(self):
        # Facility closes at 17:00; a 17:30 appointment is now outside the
        # new closing time.
        monday = date(2026, 10, 5)
        late_appt = self._appt(UUID(int=12), monday, 17, minutes=30)
        db = SimpleNamespace(scalar=AsyncMock(return_value=None), scalars=AsyncMock())
        db.scalars.side_effect = self._mock_scalars({Appointment: [late_appt], ProtectedPeriod: []})

        conflicts = await find_facility_schedule_conflicts(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            proposed_operating_start="08:00",
            proposed_operating_end="17:00",
            proposed_days_of_week=["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
            proposed_timezone="Asia/Kolkata",
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["conflict_reason"], "FACILITY_CLOSED")

    async def test_validate_interval_requires_full_appointment_duration(self):
        # A 60-minute booking whose tail overlaps a 13:00 protected lunch
        # window must be rejected — not just the overlapping slot.
        target_day = date(2026, 9, 21)
        start = local_datetime(target_day, time(12, 30), TZ)
        end = local_datetime(target_day, time(13, 30), TZ)

        lunch = SimpleNamespace(
            id=UUID(int=3),
            facility_id=FACILITY_ID,
            start_time="13:00",
            end_time="14:00",
            period_type="protected",
            days_of_week=json.dumps(["monday", "tuesday", "wednesday", "thursday", "friday"]),
        )
        db = _AvailabilityDb(rules=True, periods=[lunch])
        with self.assertRaises(ValueError) as ctx:
            await validate_interval(
                db,
                organization_id=ORGANIZATION_ID,
                facility_id=FACILITY_ID,
                practitioner_id=PRACTITIONER_ID,
                start=start,
                end=end,
            )
        self.assertIn("PROTECTED_PERIOD", str(ctx.exception))


class SchedulingAvailabilityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_facility_schedule_creates_practitioner_rules(self):
        schedule = FacilitySchedule(facility_id=FACILITY_ID)
        rules = rules_from_facility_schedule(
            schedule, ORGANIZATION_ID, FACILITY_ID, PRACTITIONER_ID
        )
        self.assertEqual([rule.weekday for rule in rules], [0, 1, 2, 3, 4, 5])
        self.assertTrue(all(rule.start_time.isoformat() == "08:00:00" for rule in rules))

    async def _request(self, db, day):
        with patch(
            "src.app.api.v1.frontend_compat._scope",
            AsyncMock(return_value=(SimpleNamespace(id=ORGANIZATION_ID), None)),
        ):
            return await availability(
                facility_uuid=FACILITY_ID,
                practitioner_uuid=PRACTITIONER_ID,
                target_date=day,
                account=SimpleNamespace(),
                db=db,
                duration_minutes=30,
                enforce_future=False,
            )

    async def test_closed_date_then_open_date_does_not_reuse_state(self):
        db = _AvailabilityDb()

        closed = (await self._request(db, date(2026, 9, 20)))["data"]
        opened = (await self._request(db, date(2026, 9, 21)))["data"]

        self.assertEqual(closed["status"], "FACILITY_CLOSED")
        self.assertTrue(closed["isClosed"])
        self.assertEqual(opened["status"], "AVAILABLE")
        self.assertTrue(opened["isOpen"])
        self.assertEqual(len(opened["slots"]), 24)

    async def test_consecutive_open_dates_are_recalculated_with_30_minute_slots(self):
        db = _AvailabilityDb()

        responses = [
            (await self._request(db, day))["data"]
            for day in (date(2026, 9, 21), date(2026, 9, 23), date(2026, 9, 24))
        ]

        for response in responses:
            self.assertEqual(response["status"], "AVAILABLE")
            self.assertEqual(len(response["slots"]), 24)
            self.assertEqual(response["slots"][0]["start"], "08:00")
            self.assertEqual(response["slots"][-1]["start"], "19:30")

    async def test_missing_practitioner_rules_do_not_close_open_facility(self):
        response = (await self._request(_AvailabilityDb(rules=False), date(2026, 9, 23)))["data"]

        self.assertEqual(response["status"], "AVAILABLE")
        self.assertTrue(response["isOpen"])
        self.assertFalse(response["isClosed"])

    async def test_each_slot_includes_explicit_state_and_status(self):
        db = _AvailabilityDb(rules=True)
        result = (await self._request(db, date(2026, 9, 21)))["data"]
        for slot in result["slots"]:
            self.assertIn("state", slot)
            self.assertIn("status", slot)
            self.assertIn(slot["state"], {"AVAILABLE", "BOOKED", "BLOCKED", "CLOSED"})
            self.assertTrue(slot["bookable"] == (slot["state"] == "AVAILABLE"))


if __name__ == "__main__":
    unittest.main()
