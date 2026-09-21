"""backfill working hours for accepted practitioner invitations"""

import json
from collections.abc import Sequence
from datetime import datetime, time
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_20"
down_revision: str | None = "20260920_19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _rule_id(facility_id, practitioner_id, weekday: int):
    return uuid5(NAMESPACE_URL, f"healthos:invited:{facility_id}:{practitioner_id}:{weekday}")


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT DISTINCT i.organization_id, a.facility_id,
               p.id AS practitioner_id, s.operating_start, s.operating_end,
               s.slot_interval_minutes, s.days_of_week, s.timezone
        FROM organization.staff_invitation i
        JOIN identity.user_account u ON lower(u.email) = lower(i.email)
        JOIN identity.practitioner p
          ON p.user_account_id = u.id AND p.organization_id = i.organization_id AND p.is_active
        JOIN organization.staff_member m
          ON m.user_account_id = u.id AND m.organization_id = i.organization_id AND m.is_active
        JOIN organization.staff_assignment a
          ON a.staff_member_id = m.id AND a.role_code IN ('practitioner', 'doctor') AND a.is_active
         AND (i.facility_id IS NULL OR a.facility_id = i.facility_id)
        JOIN organization.facility_schedule s
          ON s.facility_id = a.facility_id
        WHERE i.status = 'accepted'
          AND NOT EXISTS (
              SELECT 1 FROM care.practitioner_availability_rule r
              WHERE r.facility_id = a.facility_id
                AND r.practitioner_id = p.id
          )
    """)).mappings()

    for row in rows:
        valid_from = datetime.now(ZoneInfo(row["timezone"])).date()
        for day in json.loads(row["days_of_week"]):
            day = day.lower()
            if day not in WEEKDAYS:
                continue
            weekday = WEEKDAYS.index(day)
            bind.execute(sa.text("""
                INSERT INTO care.practitioner_availability_rule
                    (id, organization_id, facility_id, practitioner_id, weekday,
                     start_time, end_time, slot_duration_minutes, valid_from, valid_until, status)
                VALUES
                    (:id, :organization_id, :facility_id, :practitioner_id, :weekday,
                     :start_time, :end_time, :slot_duration_minutes, :valid_from, NULL, 'active')
                ON CONFLICT (facility_id, practitioner_id, weekday, start_time) DO NOTHING
            """), {
                "id": _rule_id(row["facility_id"], row["practitioner_id"], weekday),
                "organization_id": row["organization_id"],
                "facility_id": row["facility_id"],
                "practitioner_id": row["practitioner_id"],
                "weekday": weekday,
                "start_time": time.fromisoformat(row["operating_start"]),
                "end_time": time.fromisoformat(row["operating_end"]),
                "slot_duration_minutes": row["slot_interval_minutes"],
                "valid_from": valid_from,
            })


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT DISTINCT a.facility_id, p.id AS practitioner_id
        FROM organization.staff_invitation i
        JOIN identity.user_account u ON lower(u.email) = lower(i.email)
        JOIN identity.practitioner p ON p.user_account_id = u.id AND p.organization_id = i.organization_id
        JOIN organization.staff_member m ON m.user_account_id = u.id AND m.organization_id = i.organization_id
        JOIN organization.staff_assignment a
          ON a.staff_member_id = m.id AND a.role_code IN ('practitioner', 'doctor')
         AND (i.facility_id IS NULL OR a.facility_id = i.facility_id)
        WHERE i.status = 'accepted'
    """)).mappings()
    for row in rows:
        for weekday in range(7):
            bind.execute(
                sa.text("DELETE FROM care.practitioner_availability_rule WHERE id = :id"),
                {"id": _rule_id(row["facility_id"], row["practitioner_id"], weekday)},
            )
