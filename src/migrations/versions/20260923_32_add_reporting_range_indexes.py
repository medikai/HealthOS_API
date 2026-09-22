"""add reporting range indexes

Revision ID: 20260923_32
Revises: 20260922_31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260923_32"
down_revision: str | None = "20260922_31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_care_encounter_facility_started",
        "encounter",
        ["facility_id", "started_at"],
        schema="care",
    )
    op.create_index(
        "ix_care_appointment_facility_start",
        "appointment",
        ["facility_id", "scheduled_start"],
        schema="care",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_care_appointment_facility_start",
        table_name="appointment",
        schema="care",
    )
    op.drop_index(
        "ix_care_encounter_facility_started",
        table_name="encounter",
        schema="care",
    )
