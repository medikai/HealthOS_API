"""add local password recovery challenges, grants and throttling"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_39"
down_revision: Union[str, None] = "20260925_38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.add_column(
        "user_account",
        sa.Column("credentials_version", sa.Integer(), nullable=False, server_default="1"),
        schema="identity",
    )

    op.create_table(
        "password_recovery_challenge",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "user_account_id",
            uuid,
            sa.ForeignKey("identity.user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email_hash", sa.String(128), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="password_reset"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("ip_hash", sa.String(128), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="identity",
    )
    op.create_index(
        "ix_identity_password_recovery_challenge_user_account_id",
        "password_recovery_challenge",
        ["user_account_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_recovery_challenge_account",
        "password_recovery_challenge",
        ["user_account_id", "created_at"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_recovery_challenge_expires",
        "password_recovery_challenge",
        ["expires_at"],
        schema="identity",
    )

    op.create_table(
        "password_recovery_grant",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "challenge_id",
            uuid,
            sa.ForeignKey("identity.password_recovery_challenge.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_account_id",
            uuid,
            sa.ForeignKey("identity.user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("token_hash", name="uq_identity_recovery_grant_token"),
        schema="identity",
    )
    op.create_index(
        "ix_identity_password_recovery_grant_challenge_id",
        "password_recovery_grant",
        ["challenge_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_password_recovery_grant_user_account_id",
        "password_recovery_grant",
        ["user_account_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_recovery_grant_expires",
        "password_recovery_grant",
        ["expires_at"],
        schema="identity",
    )

    op.create_table(
        "password_recovery_throttle",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("scope", "key_hash", name="uq_identity_recovery_throttle"),
        schema="identity",
    )


def downgrade() -> None:
    op.drop_table("password_recovery_throttle", schema="identity")
    op.drop_table("password_recovery_grant", schema="identity")
    op.drop_table("password_recovery_challenge", schema="identity")
    op.drop_column("user_account", "credentials_version", schema="identity")
