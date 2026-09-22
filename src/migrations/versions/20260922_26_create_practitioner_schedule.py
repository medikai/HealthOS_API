"""create practitioner schedule table and indexes

Revision ID: 20260922_26
Revises: 20260922_25
Create Date: 2026-09-22 10:25:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260922_26"
down_revision: str | None = "20260922_25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "practitioner_schedule",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=False),
        sa.Column("timezone", sa.String(64), server_default="Asia/Kolkata", nullable=False),
        sa.Column("slot_interval_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("weekly_hours", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("date_exceptions", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("is_override", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=True),
        schema="care",
    )

    op.create_index(
        "ix_care_practitioner_schedule_facility_practitioner_active",
        "practitioner_schedule",
        ["facility_id", "practitioner_id", "is_active"],
        schema="care",
    )
    op.create_index(
        "ix_care_practitioner_schedule_org_practitioner",
        "practitioner_schedule",
        ["organization_id", "practitioner_id"],
        schema="care",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_care_practitioner_schedule_org_practitioner",
        table_name="practitioner_schedule",
        schema="care",
    )
    op.drop_index(
        "ix_care_practitioner_schedule_facility_practitioner_active",
        table_name="practitioner_schedule",
        schema="care",
    )
    op.drop_table("practitioner_schedule", schema="care")
