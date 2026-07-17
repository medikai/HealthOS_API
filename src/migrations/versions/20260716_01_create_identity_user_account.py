"""create identity user account

Revision ID: 20260716_01
Revises:
Create Date: 2026-07-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260716_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS identity")
    op.create_table(
        "user_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("logto_user_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("avatar_url", sa.String(length=2048), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("logto_user_id", name="uq_identity_user_account_logto_user_id"),
        schema="identity",
    )
    op.create_index(
        "ix_identity_user_account_logto_user_id", "user_account", ["logto_user_id"], unique=False, schema="identity"
    )


def downgrade() -> None:
    op.drop_index("ix_identity_user_account_logto_user_id", table_name="user_account", schema="identity")
    op.drop_table("user_account", schema="identity")
    op.execute("DROP SCHEMA IF EXISTS identity")
