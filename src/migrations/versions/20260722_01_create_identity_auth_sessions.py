"""create identity authentication sessions

Revision ID: 20260722_01
Revises: 20260720_01
Create Date: 2026-07-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260722_01"
down_revision: Union[str, None] = "20260720_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "login_transaction",
        sa.Column("state", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("nonce", sa.String(length=255), nullable=False),
        sa.Column("code_verifier", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="identity",
    )
    op.create_index("ix_identity_login_transaction_expires_at", "login_transaction", ["expires_at"], schema="identity")
    op.create_table(
        "auth_session",
        sa.Column("id", sa.String(length=255), primary_key=True, nullable=False),
        sa.Column("user_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("id_token", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_account_id"], ["identity.user_account.id"], ondelete="CASCADE"),
        schema="identity",
    )
    op.create_index("ix_identity_auth_session_user_account_id", "auth_session", ["user_account_id"], schema="identity")
    op.create_index("ix_identity_auth_session_expires_at", "auth_session", ["expires_at"], schema="identity")


def downgrade() -> None:
    op.drop_index("ix_identity_auth_session_expires_at", table_name="auth_session", schema="identity")
    op.drop_index("ix_identity_auth_session_user_account_id", table_name="auth_session", schema="identity")
    op.drop_table("auth_session", schema="identity")
    op.drop_index("ix_identity_login_transaction_expires_at", table_name="login_transaction", schema="identity")
    op.drop_table("login_transaction", schema="identity")
