"""add composite indexes for post-login operational queries

Revision ID: 20260918_15
Revises: 20260918_14
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260918_15"
down_revision: str | None = "20260918_14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_care_appointment_facility_status_start",
        "appointment",
        ["facility_id", "status", "scheduled_start"],
        schema="care",
        if_not_exists=True,
    )
    op.create_index(
        "ix_care_queue_entry_facility_date_status",
        "queue_entry",
        ["facility_id", "queue_date", "status"],
        schema="care",
        if_not_exists=True,
    )
    op.create_index(
        "ix_organization_facility_org_active",
        "facility",
        ["organization_id", "is_active"],
        schema="organization",
        if_not_exists=True,
    )
    op.create_index(
        "ix_organization_staff_assignment_facility_active",
        "staff_assignment",
        ["facility_id", "is_active", "role_code"],
        schema="organization",
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_staff_assignment_facility_active",
        table_name="staff_assignment",
        schema="organization",
    )
    op.drop_index(
        "ix_organization_facility_org_active",
        table_name="facility",
        schema="organization",
    )
    op.drop_index(
        "ix_care_queue_entry_facility_date_status",
        table_name="queue_entry",
        schema="care",
    )
    op.drop_index(
        "ix_care_appointment_facility_status_start",
        table_name="appointment",
        schema="care",
    )
