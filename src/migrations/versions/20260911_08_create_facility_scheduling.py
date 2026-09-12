"""create facility schedules and protected periods"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260911_08"
down_revision: Union[str, None] = "20260911_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table("facility_schedule", sa.Column("id", uuid, primary_key=True), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False), sa.Column("operating_start", sa.String(5), server_default="08:00", nullable=False), sa.Column("operating_end", sa.String(5), server_default="20:00", nullable=False), sa.Column("slot_interval_minutes", sa.Integer(), server_default="30", nullable=False), sa.Column("days_of_week", sa.Text(), server_default='["monday","tuesday","wednesday","thursday","friday","saturday"]', nullable=False), sa.Column("timezone", sa.String(64), server_default="Asia/Kolkata", nullable=False), sa.UniqueConstraint("facility_id", name="uq_organization_facility_schedule"), schema="organization")
    op.create_table("protected_period", sa.Column("id", uuid, primary_key=True), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False), sa.Column("title", sa.String(255), nullable=False), sa.Column("start_time", sa.String(5), nullable=False), sa.Column("end_time", sa.String(5), nullable=False), sa.Column("period_type", sa.String(64), server_default="protected", nullable=False), sa.Column("days_of_week", sa.Text(), server_default='["monday","tuesday","wednesday","thursday","friday","saturday"]', nullable=False), sa.Column("is_recurring", sa.Boolean(), server_default=sa.true(), nullable=False), schema="organization")
    op.create_index("ix_organization_protected_period_facility_id", "protected_period", ["facility_id"], schema="organization")

def downgrade() -> None:
    op.drop_table("protected_period", schema="organization")
    op.drop_table("facility_schedule", schema="organization")
