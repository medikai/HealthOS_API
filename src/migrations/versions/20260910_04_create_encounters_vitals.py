"""create encounter and vital foundation"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_04"
down_revision: Union[str, None] = "20260910_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table("encounter", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False), sa.Column("patient_id", uuid, nullable=False), sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id")), sa.Column("appointment_id", uuid, sa.ForeignKey("care.appointment.id"), unique=True), sa.Column("queue_entry_id", uuid, sa.ForeignKey("care.queue_entry.id"), unique=True), sa.Column("status", sa.String(32), server_default="in_progress", nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)), schema="care")
    op.create_index("ix_care_encounter_organization_id", "encounter", ["organization_id"], schema="care")
    op.create_index("ix_care_encounter_facility_id", "encounter", ["facility_id"], schema="care")
    op.create_index("ix_care_encounter_patient_id", "encounter", ["patient_id"], schema="care")
    op.create_table("vital", sa.Column("id", uuid, primary_key=True), sa.Column("encounter_id", uuid, sa.ForeignKey("care.encounter.id"), nullable=False), sa.Column("recorded_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=False), sa.Column("name", sa.String(64), nullable=False), sa.Column("value", sa.String(64), nullable=False), sa.Column("unit", sa.String(32)), sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False), schema="care")
    op.create_index("ix_care_vital_encounter_id", "vital", ["encounter_id"], schema="care")


def downgrade() -> None:
    op.drop_table("vital", schema="care")
    op.drop_table("encounter", schema="care")
