import unittest
from datetime import UTC, date, datetime, time, timedelta

from pydantic import ValidationError

from src.app.core.timezones import (
    local_datetime,
    normalize_range,
    reinterpret_utc_wall_time,
    timezone,
    to_timezone,
)
from src.app.schemas.access import FacilityCreate, OrganizationCreate
from src.app.schemas.facility_schedule import FacilityScheduleInput


class TimezoneTests(unittest.TestCase):
    def test_organization_ignores_legacy_timezone_but_facility_validates_it(self) -> None:
        organization = OrganizationCreate(name="NS Clinic", code="ns_clinic", timezone="Asia/Kolkata (IST • UTC+5:30)")
        self.assertNotIn("timezone", organization.model_dump())
        with self.assertRaises(ValidationError):
            OrganizationCreate(name="NS Clinic", code="ns_clinic", unknown="value")
        with self.assertRaises(ValidationError):
            FacilityCreate(name="NS Clinic", code="ns_clinic", timezone="Asia/Kolkata (IST • UTC+5:30)")

    def test_clinic_slots_and_appointment_inputs_share_one_instant(self) -> None:
        tz = timezone("Asia/Kolkata")
        slot_start = local_datetime(date(2026, 9, 16), time(9, 30), tz)
        naive_start, naive_end = normalize_range(
            datetime(2026, 9, 16, 9, 30), datetime(2026, 9, 16, 10), tz
        )
        aware_start, aware_end = normalize_range(
            datetime.fromisoformat("2026-09-16T09:30:00+05:30"),
            datetime.fromisoformat("2026-09-16T10:00:00+05:30"),
            tz,
        )

        self.assertEqual(slot_start.isoformat(), "2026-09-16T09:30:00+05:30")
        self.assertEqual(naive_start, aware_start)
        self.assertEqual(naive_end, aware_end)
        self.assertEqual(naive_start, datetime(2026, 9, 16, 4, 0, tzinfo=UTC))

    def test_aware_utc_appointment_compares_with_local_slot(self) -> None:
        tz = timezone("Asia/Kolkata")
        current = local_datetime(date(2026, 9, 16), time(18, 30), tz)
        slot_end = current + timedelta(minutes=30)
        booked_start = datetime(2026, 9, 16, 13, 0, tzinfo=UTC)
        booked_end = datetime(2026, 9, 16, 13, 30, tzinfo=UTC)

        self.assertTrue(current < booked_end and slot_end > booked_start)
        self.assertEqual(
            to_timezone(booked_start, tz).isoformat(), "2026-09-16T18:30:00+05:30"
        )

    def test_facility_day_boundary_is_an_aware_instant(self) -> None:
        boundary = local_datetime(date(2026, 9, 16), time.min, timezone("Asia/Kolkata"))
        self.assertEqual(
            boundary.astimezone(UTC), datetime(2026, 9, 15, 18, 30, tzinfo=UTC)
        )

    def test_reversed_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "scheduled_end"):
            normalize_range(
                datetime(2026, 9, 16, 10), datetime(2026, 9, 16, 9), timezone()
            )

    def test_facility_timezone_validation_accepts_alias_and_rejects_unknown(
        self,
    ) -> None:
        values = {
            "operating_start": "08:00",
            "operating_end": "20:00",
            "days_of_week": ["monday"],
        }
        self.assertEqual(
            FacilityScheduleInput(**values, timezone="Asia/Calcutta").timezone,
            "Asia/Calcutta",
        )
        with self.assertRaises(ValidationError):
            FacilityScheduleInput(**values, timezone="Mars/Olympus_Mons")

    def test_repair_reinterprets_buggy_utc_wall_time(self) -> None:
        repaired = reinterpret_utc_wall_time(
            datetime(2026, 9, 16, 13, 0, tzinfo=UTC), timezone("Asia/Kolkata")
        )
        self.assertEqual(repaired, datetime(2026, 9, 16, 7, 30, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
