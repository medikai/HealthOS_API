"""add booking availability controls"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260920_17"
down_revision: str | None = "20260919_16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "facility_resource",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("resource_type", sa.String(64), server_default="room", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.UniqueConstraint("facility_id", "name", name="uq_organization_facility_resource"),
        schema="organization",
    )
    op.create_index("ix_organization_facility_resource_facility_id", "facility_resource", ["facility_id"], schema="organization")

    op.add_column("appointment", sa.Column("resource_id", uuid, sa.ForeignKey("organization.facility_resource.id")), schema="care")
    op.create_index("ix_care_appointment_resource_id", "appointment", ["resource_id"], schema="care")
    op.add_column("practitioner_availability_rule", sa.Column("resource_id", uuid, sa.ForeignKey("organization.facility_resource.id")), schema="care")
    op.create_index("ix_care_practitioner_availability_rule_resource_id", "practitioner_availability_rule", ["resource_id"], schema="care")
    op.execute("""
        ALTER TABLE care.appointment
        ADD CONSTRAINT ex_appointment_active_resource_slot
        EXCLUDE USING gist (
            resource_id WITH =,
            tstzrange(scheduled_start, scheduled_end, '[)') WITH &&
        )
        WHERE (resource_id IS NOT NULL AND status IN ('booked', 'confirmed', 'checked_in', 'in_consultation'))
    """)

    op.create_table(
        "appointment_booking_exception",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("appointment_id", uuid, sa.ForeignKey("care.appointment.id"), nullable=False, unique=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=False),
        sa.Column("patient_id", uuid, nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("override_types", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("doctor_agreement_recorded", sa.Boolean(), nullable=False),
        sa.Column("actor_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scheduled_end > scheduled_start", name="ck_booking_exception_time_range"),
        sa.CheckConstraint("doctor_agreement_recorded", name="ck_booking_exception_doctor_agreement"),
        schema="care",
    )
    for column in ("organization_id", "facility_id", "practitioner_id", "patient_id", "actor_user_id"):
        op.create_index(f"ix_care_appointment_booking_exception_{column}", "appointment_booking_exception", [column], schema="care")


def downgrade() -> None:
    op.drop_table("appointment_booking_exception", schema="care")
    op.execute("ALTER TABLE care.appointment DROP CONSTRAINT IF EXISTS ex_appointment_active_resource_slot")
    op.drop_index("ix_care_practitioner_availability_rule_resource_id", table_name="practitioner_availability_rule", schema="care")
    op.drop_column("practitioner_availability_rule", "resource_id", schema="care")
    op.drop_index("ix_care_appointment_resource_id", table_name="appointment", schema="care")
    op.drop_column("appointment", "resource_id", schema="care")
    op.drop_table("facility_resource", schema="organization")
