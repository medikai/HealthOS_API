"""create SOAP, diagnosis and prescription documents"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_05"
down_revision: Union[str, None] = "20260910_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.add_column("practitioner", sa.Column("user_account_id", uuid, sa.ForeignKey("identity.user_account.id"), unique=True), schema="identity")
    op.create_index("ix_identity_practitioner_user_account_id", "practitioner", ["user_account_id"], schema="identity")
    op.create_table("soap_note", sa.Column("id", uuid, primary_key=True), sa.Column("encounter_id", uuid, sa.ForeignKey("care.encounter.id"), nullable=False), sa.Column("subjective", sa.Text()), sa.Column("objective", sa.Text()), sa.Column("assessment", sa.Text()), sa.Column("plan", sa.Text()), sa.Column("status", sa.String(32), server_default="draft", nullable=False), sa.Column("signed_by_user_id", uuid, sa.ForeignKey("identity.user_account.id")), sa.Column("signed_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("encounter_id", name="uq_care_soap_note_encounter"), schema="care")
    op.create_table("diagnosis", sa.Column("id", uuid, primary_key=True), sa.Column("encounter_id", uuid, sa.ForeignKey("care.encounter.id"), nullable=False), sa.Column("code", sa.String(64), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False), schema="care")
    op.create_index("ix_care_diagnosis_encounter_id", "diagnosis", ["encounter_id"], schema="care")
    op.create_table("prescription", sa.Column("id", uuid, primary_key=True), sa.Column("encounter_id", uuid, sa.ForeignKey("care.encounter.id"), nullable=False), sa.Column("status", sa.String(32), server_default="draft", nullable=False), sa.Column("advice", sa.Text()), sa.Column("signed_by_user_id", uuid, sa.ForeignKey("identity.user_account.id")), sa.Column("signed_at", sa.DateTime(timezone=True)), schema="care")
    op.create_index("ix_care_prescription_encounter_id", "prescription", ["encounter_id"], schema="care")
    op.create_table("prescription_item", sa.Column("id", uuid, primary_key=True), sa.Column("prescription_id", uuid, sa.ForeignKey("care.prescription.id"), nullable=False), sa.Column("medicine_name", sa.String(255), nullable=False), sa.Column("dosage", sa.String(128), nullable=False), sa.Column("frequency", sa.String(128), nullable=False), sa.Column("duration", sa.String(128), nullable=False), schema="care")
    op.create_index("ix_care_prescription_item_prescription_id", "prescription_item", ["prescription_id"], schema="care")


def downgrade() -> None:
    op.drop_table("prescription_item", schema="care")
    op.drop_table("prescription", schema="care")
    op.drop_table("diagnosis", schema="care")
    op.drop_table("soap_note", schema="care")
    op.drop_index("ix_identity_practitioner_user_account_id", table_name="practitioner", schema="identity")
    op.drop_column("practitioner", "user_account_id", schema="identity")
