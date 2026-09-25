"""create communication chat conversations, messages and work status"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_38"
down_revision: Union[str, None] = "20260925_37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "conversation",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("direct_key", sa.String(160), nullable=True),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("created_by_staff_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=True),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "direct_key", name="uq_communication_conversation_direct"),
        schema="communication",
    )
    for name, column in (("organization_id", "organization_id"), ("facility_id", "facility_id")):
        op.create_index(
            f"ix_communication_conversation_{name}", "conversation", [column], schema="communication"
        )
    op.create_index(
        "ix_communication_conversation_org_updated",
        "conversation",
        ["organization_id", "updated_at"],
        schema="communication",
    )

    op.create_table(
        "conversation_member",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "conversation_id",
            uuid,
            sa.ForeignKey("communication.conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("staff_member_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("join_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_read_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_read_message_id", uuid, nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "conversation_id", "staff_member_id", name="uq_communication_conversation_member"
        ),
        schema="communication",
    )
    for name, column in (
        ("conversation_id", "conversation_id"),
        ("organization_id", "organization_id"),
        ("staff_member_id", "staff_member_id"),
    ):
        op.create_index(
            f"ix_communication_conversation_member_{name}",
            "conversation_member",
            [column],
            schema="communication",
        )
    op.create_index(
        "ix_communication_conversation_member_staff",
        "conversation_member",
        ["staff_member_id", "conversation_id"],
        schema="communication",
    )

    op.create_table(
        "message",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "conversation_id",
            uuid,
            sa.ForeignKey("communication.conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("sender_staff_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sender_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=True),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("client_message_id", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="text"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "conversation_id",
            "sender_staff_id",
            "client_message_id",
            name="uq_communication_message_client_id",
        ),
        schema="communication",
    )
    for name, column in (
        ("conversation_id", "conversation_id"),
        ("organization_id", "organization_id"),
        ("sender_staff_id", "sender_staff_id"),
    ):
        op.create_index(
            f"ix_communication_message_{name}", "message", [column], schema="communication"
        )
    op.create_index(
        "ix_communication_message_conversation_sequence",
        "message",
        ["conversation_id", "sequence"],
        schema="communication",
    )

    op.create_table(
        "work_status",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("staff_member_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=False),
        sa.Column("work_state", sa.String(32), nullable=False, server_default="available"),
        sa.Column("duty", sa.String(16), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="staff"),
        sa.Column("return_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id",
            "facility_id",
            "staff_member_id",
            name="uq_communication_work_status",
        ),
        schema="communication",
    )
    for name, column in (
        ("organization_id", "organization_id"),
        ("facility_id", "facility_id"),
        ("staff_member_id", "staff_member_id"),
    ):
        op.create_index(
            f"ix_communication_work_status_{name}", "work_status", [column], schema="communication"
        )


def downgrade() -> None:
    op.drop_table("work_status", schema="communication")
    op.drop_table("message", schema="communication")
    op.drop_table("conversation_member", schema="communication")
    op.drop_table("conversation", schema="communication")
