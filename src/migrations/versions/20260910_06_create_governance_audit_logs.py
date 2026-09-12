"""create append-only governance audit logs"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_06"
down_revision: Union[str, None] = "20260910_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS governance")
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table("audit_log", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id")), sa.Column("actor_user_id", uuid, sa.ForeignKey("identity.user_account.id")), sa.Column("action", sa.String(128), nullable=False), sa.Column("resource_type", sa.String(64), nullable=False), sa.Column("resource_id", sa.String(128)), sa.Column("patient_id", sa.String(128)), sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False), schema="governance")
    for name, column in (("organization_id", "organization_id"), ("facility_id", "facility_id"), ("actor_user_id", "actor_user_id"), ("action", "action"), ("occurred_at", "occurred_at")):
        op.create_index(f"ix_governance_audit_log_{name}", "audit_log", [column], schema="governance")


def downgrade() -> None:
    op.drop_table("audit_log", schema="governance")
