"""create communication email message and delivery metadata"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_36"
down_revision: Union[str, None] = "20260925_35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "email_message",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=True),
        sa.Column("dedup_key", sa.String(320), nullable=False),
        sa.Column("template_code", sa.String(64), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("recipient_name", sa.String(255), nullable=True),
        sa.Column("from_email", sa.String(320), nullable=False),
        sa.Column("from_name", sa.String(255), nullable=True),
        sa.Column("subject", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("provider_request_id", sa.String(255), nullable=True),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("context", jsonb, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("encrypted_context", sa.Text(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_communication_email_message_dedup"),
        schema="communication",
    )
    op.create_index(
        "ix_communication_email_message_organization_id",
        "email_message",
        ["organization_id"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_email_message_status",
        "email_message",
        ["status", "created_at"],
        schema="communication",
    )


def downgrade() -> None:
    op.drop_table("email_message", schema="communication")
