"""create care scheduling and queue foundation"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_01"
down_revision: Union[str, None] = "20260723_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS identity")
    op.execute("CREATE SCHEMA IF NOT EXISTS care")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table("practitioner", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("person_name", sa.String(255), nullable=False), sa.Column("specialty", sa.String(255)), sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), schema="identity")
    op.create_index("ix_identity_practitioner_organization_id", "practitioner", ["organization_id"], schema="identity")
    op.create_table("appointment", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False), sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=False), sa.Column("patient_id", uuid, nullable=False), sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False), sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False), sa.Column("status", sa.String(32), server_default="booked", nullable=False), sa.Column("reason_code", sa.String(64)), sa.Column("reason_text", sa.Text()), sa.Column("idempotency_key", sa.String(255)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_appointment_org_idempotency"), schema="care")
    op.create_index("ix_care_appointment_facility_id", "appointment", ["facility_id"], schema="care")
    op.create_index("ix_care_appointment_practitioner_id", "appointment", ["practitioner_id"], schema="care")
    op.create_index("ix_care_appointment_scheduled_start", "appointment", ["scheduled_start"], schema="care")
    op.create_index("ix_care_appointment_status", "appointment", ["status"], schema="care")
    op.execute("""
        ALTER TABLE care.appointment
        ADD CONSTRAINT ex_appointment_active_practitioner_slot
        EXCLUDE USING gist (
            practitioner_id WITH =,
            tstzrange(scheduled_start, scheduled_end, '[)') WITH &&
        )
        WHERE (status IN ('booked', 'checked_in', 'in_consultation'))
    """)
    for name, columns in (("practitioner_availability_rule", [("organization_id", uuid, "organization.organization.id"), ("facility_id", uuid, "organization.facility.id"), ("practitioner_id", uuid, "identity.practitioner.id"), ("weekday", sa.Integer(), None), ("start_time", sa.Time(), None), ("end_time", sa.Time(), None), ("slot_duration_minutes", sa.Integer(), None), ("valid_from", sa.Date(), None), ("valid_until", sa.Date(), None), ("status", sa.String(32), None)]),):
        cols = [sa.Column("id", uuid, primary_key=True)]
        for col, typ, fk in columns:
            cols.append(sa.Column(col, typ, sa.ForeignKey(fk) if fk else None, nullable=col not in {"valid_until"}))
        cols.append(sa.UniqueConstraint("facility_id", "practitioner_id", "weekday", "start_time", name="uq_care_availability_rule"))
        op.create_table(name, *cols, schema="care")
    op.create_table("practitioner_availability_exception", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False), sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=False), sa.Column("exception_date", sa.Date(), nullable=False), sa.Column("start_time", sa.Time()), sa.Column("end_time", sa.Time()), sa.Column("exception_type", sa.String(32), nullable=False), sa.Column("reason", sa.Text()), schema="care")
    op.create_table("queue_counter", sa.Column("id", uuid, primary_key=True), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False), sa.Column("queue_date", sa.Date(), nullable=False), sa.Column("last_token_number", sa.Integer(), server_default="0", nullable=False), sa.UniqueConstraint("facility_id", "queue_date", name="uq_care_queue_counter_facility_date"), schema="care")
    op.create_table("queue_entry", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False), sa.Column("appointment_id", uuid, sa.ForeignKey("care.appointment.id")), sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id")), sa.Column("patient_id", uuid, nullable=False), sa.Column("queue_date", sa.Date(), nullable=False), sa.Column("token_number", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), server_default="waiting", nullable=False), sa.Column("reason_code", sa.String(64)), sa.Column("reason_text", sa.Text()), sa.Column("called_at", sa.DateTime(timezone=True)), sa.Column("skip_count", sa.Integer(), server_default="0", nullable=False), schema="care")
    for table, cols in (("practitioner_availability_rule", ["facility_id", "practitioner_id"]), ("practitioner_availability_exception", ["facility_id", "practitioner_id", "exception_date"]), ("queue_counter", ["facility_id"]), ("queue_entry", ["facility_id", "queue_date", "status"])):
        for col in cols:
            op.create_index(f"ix_care_{table}_{col}", table, [col], schema="care")


def downgrade() -> None:
    op.drop_table("queue_entry", schema="care")
    op.drop_table("queue_counter", schema="care")
    op.drop_table("practitioner_availability_exception", schema="care")
    op.drop_table("practitioner_availability_rule", schema="care")
    op.execute("ALTER TABLE care.appointment DROP CONSTRAINT IF EXISTS ex_appointment_active_practitioner_slot")
    op.drop_table("appointment", schema="care")
    op.drop_table("practitioner", schema="identity")
