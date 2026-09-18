"""Audit or repair appointments persisted from naive clinic-local timestamps.

Audit candidates created since the faulty endpoint was introduced:

    python src/scripts/repair_appointment_timezones.py

Review the output, then repair one confirmed appointment at a time:

    python src/scripts/repair_appointment_timezones.py --appointment-id UUID --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.db.database import local_session
from app.core.timezones import (
    DEFAULT_TIMEZONE,
    reinterpret_utc_wall_time,
    timezone,
    to_timezone,
)
from app.models.care import Appointment
from app.models.organization import FacilitySchedule

ACTIVE_STATUSES = ("booked", "confirmed", "checked_in", "in_consultation")


async def run(since: date, appointment_ids: list[UUID], apply: bool) -> None:
    if apply and len(appointment_ids) != 1:
        raise SystemExit("--apply requires exactly one confirmed --appointment-id")

    async with local_session() as db:
        query = (
            select(Appointment)
            .where(
                Appointment.created_at
                >= datetime.combine(since, datetime.min.time(), tzinfo=UTC)
            )
            .order_by(Appointment.created_at)
        )
        if appointment_ids:
            query = query.where(Appointment.id.in_(appointment_ids))
        appointments = (await db.scalars(query)).all()
        if apply and not appointments:
            raise SystemExit("Confirmed appointment was not found in the audit window")

        # ponytail: one schedule lookup per audit row; batch it if repair volumes become large.
        for appointment in appointments:
            name = await db.scalar(
                select(FacilitySchedule.timezone).where(
                    FacilitySchedule.facility_id == appointment.facility_id
                )
            )
            tz = timezone(name or DEFAULT_TIMEZONE)
            proposed_start = reinterpret_utc_wall_time(appointment.scheduled_start, tz)
            proposed_end = reinterpret_utc_wall_time(appointment.scheduled_end, tz)
            print(
                json.dumps(
                    {
                        "appointment_id": str(appointment.id),
                        "facility_timezone": tz.key,
                        "created_at": appointment.created_at.isoformat(),
                        "current_utc": [
                            appointment.scheduled_start.isoformat(),
                            appointment.scheduled_end.isoformat(),
                        ],
                        "current_local": [
                            to_timezone(appointment.scheduled_start, tz).isoformat(),
                            to_timezone(appointment.scheduled_end, tz).isoformat(),
                        ],
                        "proposed_utc": [
                            proposed_start.isoformat(),
                            proposed_end.isoformat(),
                        ],
                        "proposed_local": [
                            to_timezone(proposed_start, tz).isoformat(),
                            to_timezone(proposed_end, tz).isoformat(),
                        ],
                    }
                )
            )

            if not apply:
                continue
            conflict = await db.scalar(
                select(Appointment.id).where(
                    Appointment.id != appointment.id,
                    Appointment.facility_id == appointment.facility_id,
                    Appointment.practitioner_id == appointment.practitioner_id,
                    Appointment.status.in_(ACTIVE_STATUSES),
                    Appointment.scheduled_start < proposed_end,
                    Appointment.scheduled_end > proposed_start,
                )
            )
            if conflict:
                raise SystemExit(
                    f"Refusing repair: proposed time conflicts with appointment {conflict}"
                )
            appointment.scheduled_start, appointment.scheduled_end = (
                proposed_start,
                proposed_end,
            )

        if apply:
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise SystemExit(
                    f"Repair refused by database constraint: {exc.orig}"
                ) from None
            print(f"Repaired appointment {appointment_ids[0]}")
        elif not appointments:
            print("No matching appointments found.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=date.fromisoformat, default=date(2026, 9, 12))
    parser.add_argument("--appointment-id", action="append", type=UUID, default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.since, args.appointment_id, args.apply))


if __name__ == "__main__":
    main()
