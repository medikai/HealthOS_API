"""create staff_invitation table"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260914_10"
down_revision: Union[str, None] = "20260914_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "staff_invitation",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role_code", sa.String(64), nullable=False),
        sa.Column("token", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token", name="uq_organization_staff_invitation_token"),
        schema="organization",
    )
    op.create_index("ix_organization_staff_invitation_org_id", "staff_invitation", ["organization_id"], schema="organization")
    op.create_index("ix_organization_staff_invitation_email", "staff_invitation", ["email"], schema="organization")
    op.create_index("ix_organization_staff_invitation_token", "staff_invitation", ["token"], schema="organization")


def downgrade() -> None:
    op.drop_table("staff_invitation", schema="organization")
