"""add patient portal identity, sessions, phone OTP and record links

Revision ID: 20261005_43
Revises: 20260929_42
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261005_43"
down_revision: str | None = "20260929_42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "patient_portal_account",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("date_of_birth", sa.String(10), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("phone", name="uq_identity_patient_portal_account_phone"),
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_portal_account_phone",
        "patient_portal_account",
        ["phone"],
        schema="identity",
    )

    op.create_table(
        "patient_portal_session",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_identity_patient_portal_session_token"),
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_portal_session_patient_account_id",
        "patient_portal_session",
        ["patient_account_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_portal_session_account",
        "patient_portal_session",
        ["patient_account_id", "created_at"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_portal_session_expires",
        "patient_portal_session",
        ["expires_at"],
        schema="identity",
    )

    op.create_table(
        "patient_otp_challenge",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("phone_hash", sa.String(128), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="login"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("ip_hash", sa.String(128), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_otp_challenge_patient_account_id",
        "patient_otp_challenge",
        ["patient_account_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_otp_challenge_phone_hash",
        "patient_otp_challenge",
        ["phone_hash"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_otp_challenge_phone",
        "patient_otp_challenge",
        ["phone_hash", "created_at"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_otp_challenge_expires",
        "patient_otp_challenge",
        ["expires_at"],
        schema="identity",
    )

    op.create_table(
        "patient_otp_throttle",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("scope", "key_hash", name="uq_identity_patient_otp_throttle"),
        schema="identity",
    )

    op.create_table(
        "patient_record_link",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            uuid,
            sa.ForeignKey("organization.organization.id"),
            nullable=False,
        ),
        sa.Column(
            "facility_id",
            uuid,
            sa.ForeignKey("organization.facility.id"),
            nullable=True,
        ),
        sa.Column(
            "patient_id",
            uuid,
            sa.ForeignKey("identity.patient.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("request_kind", sa.String(32), nullable=False, server_default="clinic_assisted"),
        sa.Column("verification_source", sa.String(32), nullable=True),
        sa.Column("claimed_name", sa.String(255), nullable=True),
        sa.Column("claimed_date_of_birth", sa.String(10), nullable=True),
        sa.Column(
            "reviewed_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(255), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=True,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_record_link_patient_account_id",
        "patient_record_link",
        ["patient_account_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_record_link_organization_id",
        "patient_record_link",
        ["organization_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_record_link_facility_id",
        "patient_record_link",
        ["facility_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_record_link_patient_id",
        "patient_record_link",
        ["patient_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_record_link_status",
        "patient_record_link",
        ["status"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_account_status",
        "patient_record_link",
        ["patient_account_id", "status"],
        schema="identity",
    )
    op.create_index(
        "uq_identity_patient_link_account_org_active",
        "patient_record_link",
        ["patient_account_id", "organization_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'verified')"),
        schema="identity",
    )
    op.create_index(
        "uq_identity_patient_link_patient_active",
        "patient_record_link",
        ["patient_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'verified')"),
        schema="identity",
    )

    op.create_table(
        "patient_link_invitation",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "organization_id",
            uuid,
            sa.ForeignKey("organization.organization.id"),
            nullable=False,
        ),
        sa.Column(
            "facility_id",
            uuid,
            sa.ForeignKey("organization.facility.id"),
            nullable=True,
        ),
        sa.Column(
            "patient_id",
            uuid,
            sa.ForeignKey("identity.patient.id"),
            nullable=False,
        ),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False, server_default="portal_link"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=True,
        ),
        sa.Column(
            "accepted_by_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "accepted_link_id",
            uuid,
            sa.ForeignKey("identity.patient_record_link.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("token_hash", name="uq_identity_patient_link_invitation_token"),
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_invitation_organization_id",
        "patient_link_invitation",
        ["organization_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_invitation_facility_id",
        "patient_link_invitation",
        ["facility_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_invitation_patient_id",
        "patient_link_invitation",
        ["patient_id"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_invitation_phone",
        "patient_link_invitation",
        ["phone"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_invitation_status",
        "patient_link_invitation",
        ["status"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_invitation_expires",
        "patient_link_invitation",
        ["expires_at"],
        schema="identity",
    )
    op.create_index(
        "ix_identity_patient_link_invitation_org_status",
        "patient_link_invitation",
        ["organization_id", "status"],
        schema="identity",
    )

    op.add_column(
        "organization",
        sa.Column(
            "portal_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema="organization",
    )
    op.create_index(
        "ix_organization_organization_portal_enabled",
        "organization",
        ["portal_enabled"],
        schema="organization",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_organization_portal_enabled",
        table_name="organization",
        schema="organization",
    )
    op.drop_column("organization", "portal_enabled", schema="organization")

    op.drop_table("patient_link_invitation", schema="identity")
    op.drop_table("patient_record_link", schema="identity")
    op.drop_table("patient_otp_throttle", schema="identity")
    op.drop_table("patient_otp_challenge", schema="identity")
    op.drop_table("patient_portal_session", schema="identity")
    op.drop_table("patient_portal_account", schema="identity")
