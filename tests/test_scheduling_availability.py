import json
import unittest
from datetime import date, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from src.app.api.v1.frontend_compat import availability
from src.app.core.availability import rules_from_facility_schedule
from src.app.models.care import (
    Appointment,
    PractitionerAvailabilityException,
    PractitionerAvailabilityRule,
)
from src.app.models.organization import FacilitySchedule, ProtectedPeriod

FACILITY_ID = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
PRACTITIONER_ID = UUID("01a0a022-b31b-728f-b7ae-b06211a65893")
ORGANIZATION_ID = UUID("01a0a022-b2aa-7509-aecb-3166583df954")


class _Rows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _AvailabilityDb:
    def __init__(self, *, rules=True):
        self.schedule = SimpleNamespace(
            operating_start="08:00",
            operating_end="20:00",
            slot_interval_minutes=30,
            days_of_week=json.dumps(
                ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
            ),
            timezone="Asia/Kolkata",
        )
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

    async def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        return self.schedule if entity is FacilitySchedule else None

    async def scalars(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        values = {
            PractitionerAvailabilityRule: self.rules,
            PractitionerAvailabilityException: [],
            ProtectedPeriod: [],
            Appointment: [],
        }.get(entity, [])
        return _Rows(values)


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

        self.assertEqual(response["status"], "MISSING_CONFIGURATION")
        self.assertTrue(response["isOpen"])
        self.assertFalse(response["isClosed"])


if __name__ == "__main__":
    unittest.main()
