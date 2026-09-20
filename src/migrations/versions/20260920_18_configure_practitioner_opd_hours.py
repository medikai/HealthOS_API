"""configure practitioner OPD hours for the affected facility"""

import json
from collections.abc import Sequence
from datetime import date, time
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_18"
down_revision: str | None = "20260920_17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FACILITY_ID = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
PRACTITIONER_ID = UUID("01a0a022-b31b-728f-b7ae-b06211a65893")
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _rule_id(weekday: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"healthos:{FACILITY_ID}:{PRACTITIONER_ID}:{weekday}")


def upgrade() -> None:
    bind = op.get_bind()
    op.alter_column(
        "practitioner_availability_rule",
        "valid_until",
        existing_type=sa.Date(),
        nullable=True,
        schema="care",
    )
    schedule = bind.execute(
        sa.text("""
            SELECT f.organization_id, s.operating_start, s.operating_end, s.days_of_week
            FROM organization.facility_schedule s
            JOIN organization.facility f ON f.id = s.facility_id
            JOIN identity.practitioner p
              ON p.id = :practitioner_id AND p.organization_id = f.organization_id
            WHERE s.facility_id = :facility_id AND f.is_active AND p.is_active
        """),
        {"facility_id": FACILITY_ID, "practitioner_id": PRACTITIONER_ID},
    ).mappings().one_or_none()
    if schedule is None:
        return

    configured_days = {WEEKDAYS[day.lower()] for day in json.loads(schedule["days_of_week"]) if day.lower() in WEEKDAYS}
    for weekday in configured_days:
        bind.execute(
            sa.text("""
                INSERT INTO care.practitioner_availability_rule
                    (id, organization_id, facility_id, practitioner_id, weekday,
                     start_time, end_time, slot_duration_minutes, valid_from, valid_until, status)
                VALUES
                    (:id, :organization_id, :facility_id, :practitioner_id, :weekday,
                     CAST(:start_time AS time), CAST(:end_time AS time), 30, :valid_from, NULL, 'active')
                ON CONFLICT (facility_id, practitioner_id, weekday, start_time) DO NOTHING
            """),
            {
                "id": _rule_id(weekday),
                "organization_id": schedule["organization_id"],
                "facility_id": FACILITY_ID,
                "practitioner_id": PRACTITIONER_ID,
                "weekday": weekday,
                "start_time": time.fromisoformat(schedule["operating_start"]),
                "end_time": time.fromisoformat(schedule["operating_end"]),
                "valid_from": date(2026, 9, 21),
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    for weekday in WEEKDAYS.values():
        bind.execute(
            sa.text("DELETE FROM care.practitioner_availability_rule WHERE id = :id"),
            {"id": _rule_id(weekday)},
        )
    op.alter_column(
        "practitioner_availability_rule",
        "valid_until",
        existing_type=sa.Date(),
        nullable=False,
        schema="care",
    )
