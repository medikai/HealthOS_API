import json
import unittest
from datetime import date, time, timedelta
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from src.app.core.availability import (
    evaluate_availability,
    evaluate_availability_range,
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


class _RangeDb:
    """Fake session that serves the batched range loader from memory."""

    def __init__(
        self,
        *,
        schedule=None,
        practitioner_schedules=None,
        rules=None,
        exceptions=None,
        periods=None,
        appointments=None,
        resources=None,
    ):
        self.schedule = schedule or SimpleNamespace(
            operating_start="08:00",
            operating_end="20:00",
            slot_interval_minutes=30,
            days_of_week=json.dumps(
                ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
            ),
            timezone="Asia/Kolkata",
        )
        self.practitioner_schedules = practitioner_schedules or []
        self.rules = rules or []
        self.exceptions = exceptions or []
        self.periods = periods or []
        self.appointments = appointments or []
        self.resources = resources or {}
        self.calls = 0

    async def scalar(self, statement):
        self.calls += 1
        entity = statement.column_descriptions[0].get("entity")
        if entity is FacilitySchedule:
            return self.schedule
        if entity is PractitionerSchedule:
            return self.practitioner_schedules[0] if self.practitioner_schedules else None
        return None

    async def scalars(self, statement):
        self.calls += 1
        entity = statement.column_descriptions[0].get("entity")
        values = {
            PractitionerSchedule: self.practitioner_schedules,
            PractitionerAvailabilityRule: self.rules,
            PractitionerAvailabilityException: self.exceptions,
            ProtectedPeriod: self.periods,
            Appointment: self.appointments,
            FacilityResource: list(self.resources.values()),
        }.get(entity, [])
        return _Rows(values)


def _practitioner_schedule(**overrides):
    values = {
        "id": UUID(int=1),
        "organization_id": ORGANIZATION_ID,
        "facility_id": FACILITY_ID,
        "practitioner_id": PRACTITIONER_ID,
        "slot_interval_minutes": 30,
        "timezone": "Asia/Kolkata",
        "is_active": True,
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
        "version": 1,
        "weekly_hours": [
            {
                "day_of_week": "monday",
                "is_working": True,
                "start_time": "09:00",
                "end_time": "17:00",
                "breaks": [{"start_time": "13:00", "end_time": "13:30"}],
            },
            {
                "day_of_week": "tuesday",
                "is_working": False,
                "start_time": None,
                "end_time": None,
                "breaks": [],
            },
            {
                "day_of_week": "wednesday",
                "is_working": True,
                "start_time": "09:00",
                "end_time": "17:00",
                "breaks": [],
            },
        ],
        "date_exceptions": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AvailabilityRangeBatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_range_matches_sequential_day_evaluation(self):
        """Batch output must be identical to one evaluate_availability per day."""
        monday = date(2026, 9, 21)
        target_week = [monday + timedelta(days=offset) for offset in range(7)]

        appointment_start = local_datetime(monday, time(9, 0), TZ)
        shared_appointment = SimpleNamespace(
            id=UUID(int=90),
            practitioner_id=PRACTITIONER_ID,
            facility_id=FACILITY_ID,
            resource_id=None,
            scheduled_start=appointment_start,
            scheduled_end=appointment_start + timedelta(minutes=30),
        )
        lunch = SimpleNamespace(
            id=UUID(int=3),
            facility_id=FACILITY_ID,
            start_time="12:00",
            end_time="12:30",
            period_type="protected",
            days_of_week=json.dumps(["monday"]),
        )

        def build_db():
            return _RangeDb(
                practitioner_schedules=[_practitioner_schedule()],
                appointments=[shared_appointment],
                periods=[lunch],
            )

        sequential_db = build_db()
        sequential = [
            await evaluate_availability(
                sequential_db,
                organization_id=ORGANIZATION_ID,
                facility_id=FACILITY_ID,
                practitioner_id=PRACTITIONER_ID,
                day=day,
            )
            for day in target_week
        ]

        range_db = build_db()
        batched = await evaluate_availability_range(
            range_db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            from_day=target_week[0],
            to_day=target_week[-1],
        )

        self.assertEqual(len(batched), len(sequential))
        for batched_day, sequential_day in zip(batched, sequential):
            self.assertEqual(batched_day["date"], sequential_day["date"])
            self.assertEqual(batched_day["state"], sequential_day["state"])
            self.assertEqual(batched_day["status"], sequential_day["status"])
            self.assertEqual(
                [slot["start"] for slot in batched_day["slots"]],
                [slot["start"] for slot in sequential_day["slots"]],
            )
            self.assertEqual(
                [slot["status"] for slot in batched_day["slots"]],
                [slot["status"] for slot in sequential_day["slots"]],
            )

    async def test_range_scopes_schedule_effective_dates_and_exceptions_to_their_day(self):
        monday = date(2026, 9, 21)
        exception_day = date(2026, 9, 23)
        schedule_start = date(2026, 9, 24)

        late_schedule = _practitioner_schedule(
            effective_from=schedule_start,
            weekly_hours=[
                {
                    "day_of_week": day_name,
                    "is_working": True,
                    "start_time": "10:00",
                    "end_time": "12:00",
                    "breaks": [],
                }
                for day_name in ("monday", "tuesday", "wednesday", "thursday", "friday")
            ],
            date_exceptions=[],
        )
        legacy_rule = SimpleNamespace(
            weekday=0,  # Monday
            start_time=time(8),
            end_time=time(20),
            slot_duration_minutes=30,
            valid_from=date(2026, 1, 1),
            valid_until=None,
            resource_id=None,
        )
        leave = SimpleNamespace(
            exception_date=exception_day,
            exception_type="leave",
            start_time=None,
            end_time=None,
            reason="Conference",
        )
        appointment_start = local_datetime(date(2026, 9, 25), time(11, 0), TZ)
        friday_appointment = SimpleNamespace(
            id=UUID(int=91),
            practitioner_id=PRACTITIONER_ID,
            facility_id=FACILITY_ID,
            resource_id=None,
            scheduled_start=appointment_start,
            scheduled_end=appointment_start + timedelta(minutes=30),
        )

        db = _RangeDb(
            practitioner_schedules=[late_schedule],
            rules=[legacy_rule],
            exceptions=[leave],
            appointments=[friday_appointment],
        )
        results = await evaluate_availability_range(
            db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            from_day=monday,
            to_day=date(2026, 9, 27),
        )
        by_date = {result["date"]: result for result in results}

        # Before the override starts, the Monday legacy rule governs.
        self.assertEqual(by_date["2026-09-21"]["state"], "AVAILABLE")
        self.assertEqual(by_date["2026-09-21"]["slots"][0]["start"][11:16], "08:00")

        # Date-scoped leave blocks only its own day.
        self.assertEqual(by_date["2026-09-23"]["state"], "BLOCKED")
        self.assertEqual(by_date["2026-09-23"]["status"], "DOCTOR_UNAVAILABLE")
        self.assertNotEqual(by_date["2026-09-24"]["status"], "DOCTOR_UNAVAILABLE")

        # Once effective, the practitioner schedule wins over the legacy rule.
        self.assertEqual(by_date["2026-09-24"]["slots"][0]["start"][11:16], "10:00")

        # An appointment blocks only the slot/day it overlaps.
        self.assertNotIn(
            "AVAILABLE",
            {
                slot["status"]
                for slot in by_date["2026-09-25"]["slots"]
                if slot["start"][11:16] == "11:00"
            },
        )
        self.assertNotIn(
            "BOOKED",
            {slot["status"] for slot in by_date["2026-09-24"]["slots"]},
        )

    async def test_range_query_count_is_constant_regardless_of_window_size(self):
        def build_db():
            return _RangeDb(
                practitioner_schedules=[_practitioner_schedule()],
                periods=[
                    SimpleNamespace(
                        id=UUID(int=3),
                        facility_id=FACILITY_ID,
                        start_time="12:00",
                        end_time="12:30",
                        period_type="protected",
                        days_of_week=json.dumps(["monday"]),
                    )
                ],
            )

        short_db = build_db()
        await evaluate_availability_range(
            short_db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            from_day=date(2026, 9, 21),
            to_day=date(2026, 9, 23),
        )

        long_db = build_db()
        await evaluate_availability_range(
            long_db,
            organization_id=ORGANIZATION_ID,
            facility_id=FACILITY_ID,
            practitioner_id=PRACTITIONER_ID,
            from_day=date(2026, 9, 21),
            to_day=date(2026, 10, 21),
        )

        self.assertEqual(short_db.calls, long_db.calls)
        self.assertLessEqual(long_db.calls, 7)


if __name__ == "__main__":
    unittest.main()
