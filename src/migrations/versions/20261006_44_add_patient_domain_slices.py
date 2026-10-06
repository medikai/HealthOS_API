"""add patient-domain slices: appointment requests, record releases, receipts,
patient notifications/push, invoice discounts and facility portal auto-confirm

Revision ID: 20261006_44
Revises: 20261005_43
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261006_44"
down_revision: str | None = "20261005_43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB()

    op.create_table(
        "appointment_request",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("patient_id", uuid, sa.ForeignKey("identity.patient.id"), nullable=False),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=True),
        sa.Column("appointment_id", uuid, sa.ForeignKey("care.appointment.id"), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False, server_default="new"),
        sa.Column("status", sa.String(24), nullable=False, server_default="requested"),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alternative_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alternative_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alternative_resource_id", uuid, sa.ForeignKey("organization.facility_resource.id"), nullable=True),
        sa.Column("alternative_note", sa.String(500), nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
        sa.Column(
            "decided_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('new', 'reschedule', 'cancel')",
            name="ck_care_appointment_request_kind",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'alternative_proposed', 'withdrawn', 'expired')",
            name="ck_care_appointment_request_status",
        ),
        schema="care",
    )
    op.create_index(
        "ix_care_appointment_request_org_status",
        "appointment_request",
        ["organization_id", "status", "created_at"],
        schema="care",
    )
    op.create_index(
        "ix_care_appointment_request_account_status",
        "appointment_request",
        ["patient_account_id", "status"],
        schema="care",
    )
    op.create_index(
        "ix_care_appointment_request_patient",
        "appointment_request",
        ["patient_id", "status"],
        schema="care",
    )
    op.create_index(
        "ix_care_appointment_request_facility",
        "appointment_request",
        ["facility_id", "status"],
        schema="care",
    )
    op.create_index(
        "uq_care_appointment_request_idempotency",
        "appointment_request",
        ["organization_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        schema="care",
    )

    op.create_table(
        "record_release",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("patient_id", uuid, sa.ForeignKey("identity.patient.id"), nullable=False),
        sa.Column("encounter_id", uuid, sa.ForeignKey("care.encounter.id"), nullable=True),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", uuid, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("snapshot", jsonb, nullable=False),
        sa.Column("released_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "resource_type IN ('encounter_summary', 'vitals', 'prescription', 'patient_document')",
            name="ck_care_record_release_type",
        ),
        schema="care",
    )
    op.create_index(
        "ix_care_record_release_patient_type",
        "record_release",
        ["patient_id", "resource_type"],
        schema="care",
    )
    op.create_index(
        "ix_care_record_release_org",
        "record_release",
        ["organization_id", "released_at"],
        schema="care",
    )
    op.create_index(
        "uq_care_record_release_active",
        "record_release",
        ["resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        schema="care",
    )

    op.create_table(
        "receipt",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("invoice_id", uuid, sa.ForeignKey("care.invoice.id"), nullable=False),
        sa.Column("payment_id", uuid, sa.ForeignKey("care.payment.id"), nullable=False),
        sa.Column("receipt_number", sa.String(64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("issued_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("payment_id", name="uq_care_receipt_payment"),
        sa.UniqueConstraint("receipt_number", name="uq_care_receipt_number"),
        schema="care",
    )
    op.create_index("ix_care_receipt_invoice", "receipt", ["invoice_id"], schema="care")

    op.add_column("invoice", sa.Column("gross_amount_minor", sa.BigInteger(), nullable=True), schema="care")
    op.add_column("invoice", sa.Column("discount_minor", sa.BigInteger(), nullable=True), schema="care")
    op.add_column("invoice", sa.Column("discount_kind", sa.String(16), nullable=True), schema="care")
    op.add_column("invoice", sa.Column("discount_bp", sa.Integer(), nullable=True), schema="care")
    op.add_column("invoice", sa.Column("discount_fixed_minor", sa.BigInteger(), nullable=True), schema="care")
    op.add_column("invoice", sa.Column("discount_reason", sa.String(500), nullable=True), schema="care")
    op.add_column(
        "invoice",
        sa.Column("discounted_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=True),
        schema="care",
    )
    op.add_column("invoice", sa.Column("discounted_at", sa.DateTime(timezone=True), nullable=True), schema="care")

    op.add_column(
        "facility",
        sa.Column(
            "portal_auto_confirm",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema="organization",
    )

    op.create_table(
        "patient_notification",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=True),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("category", sa.String(48), nullable=False, server_default="general"),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("deep_link", sa.String(255), nullable=True),
        sa.Column("dedup_key", sa.String(160), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_communication_patient_notification_dedup"),
        schema="communication",
    )
    op.create_index(
        "ix_communication_patient_notification_account",
        "patient_notification",
        ["patient_account_id", "created_at"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_patient_notification_unread",
        "patient_notification",
        ["patient_account_id", "read_at"],
        schema="communication",
    )

    op.create_table(
        "patient_notification_preference",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("in_app", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("push", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("patient_account_id", name="uq_communication_patient_preference_account"),
        schema="communication",
    )

    op.create_table(
        "patient_push_device",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("installation_id", sa.String(128), nullable=False),
        sa.Column("token", sa.String(512), nullable=False),
        sa.Column("platform", sa.String(24), nullable=False, server_default="web"),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token", name="uq_communication_patient_push_token"),
        sa.UniqueConstraint(
            "patient_account_id", "installation_id", name="uq_communication_patient_push_install"
        ),
        schema="communication",
    )
    op.create_index(
        "ix_communication_patient_push_account_active",
        "patient_push_device",
        ["patient_account_id", "is_active"],
        schema="communication",
    )


def downgrade() -> None:
    op.drop_table("patient_push_device", schema="communication")
    op.drop_table("patient_notification_preference", schema="communication")
    op.drop_table("patient_notification", schema="communication")

    op.drop_column("facility", "portal_auto_confirm", schema="organization")

    for column in (
        "discounted_at",
        "discounted_by_user_id",
        "discount_reason",
        "discount_fixed_minor",
        "discount_bp",
        "discount_kind",
        "discount_minor",
        "gross_amount_minor",
    ):
        op.drop_column("invoice", column, schema="care")

    op.drop_table("receipt", schema="care")
    op.drop_table("record_release", schema="care")
    op.drop_table("appointment_request", schema="care")
