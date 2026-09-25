"""create communication push device bindings"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_40"
down_revision: Union[str, None] = "20260925_39"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "push_device",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("staff_member_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=False),
        sa.Column("user_account_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=False),
        sa.Column("installation_id", sa.String(128), nullable=False),
        sa.Column("token", sa.String(512), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False, server_default="web"),
        sa.Column("environment", sa.String(32), nullable=False, server_default="dev"),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token", name="uq_communication_push_device_token"),
        sa.UniqueConstraint(
            "organization_id",
            "staff_member_id",
            "installation_id",
            name="uq_communication_push_device_installation",
        ),
        schema="communication",
    )
    for name, column in (
        ("organization_id", "organization_id"),
        ("staff_member_id", "staff_member_id"),
        ("user_account_id", "user_account_id"),
    ):
        op.create_index(
            f"ix_communication_push_device_{name}",
            "push_device",
            [column],
            schema="communication",
        )
    op.create_index(
        "ix_communication_push_device_staff_active",
        "push_device",
        ["staff_member_id", "is_active"],
        schema="communication",
    )


def downgrade() -> None:
    op.drop_table("push_device", schema="communication")
