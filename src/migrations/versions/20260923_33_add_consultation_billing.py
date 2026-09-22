"""add consultation fees and encounter billing

Revision ID: 20260923_33
Revises: 20260923_32
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260923_33"
down_revision: str | None = "20260923_32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "consultation_fee",
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
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id")),
        sa.Column("specialty_id", uuid, sa.ForeignKey("platform.specialty.id")),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount_minor >= 0", name="ck_consultation_fee_nonnegative"),
        sa.CheckConstraint(
            "char_length(currency) = 3", name="ck_consultation_fee_currency"
        ),
        sa.CheckConstraint(
            "(scope_type = 'facility' AND practitioner_id IS NULL AND specialty_id IS NULL) OR (scope_type = 'specialty' AND practitioner_id IS NULL AND specialty_id IS NOT NULL) OR (scope_type = 'practitioner' AND practitioner_id IS NOT NULL AND specialty_id IS NULL)",
            name="ck_consultation_fee_scope",
        ),
        schema="care",
    )
    for columns in (
        ("organization_id",),
        ("facility_id",),
        ("practitioner_id",),
        ("specialty_id",),
        ("created_by_user_id",),
    ):
        op.create_index(
            f"ix_care_consultation_fee_{columns[0]}",
            "consultation_fee",
            list(columns),
            schema="care",
        )
    op.create_index(
        "ix_care_consultation_fee_resolution",
        "consultation_fee",
        ["facility_id", "scope_type", "effective_from", "effective_to"],
        schema="care",
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_care_consultation_fee_active_facility ON care.consultation_fee (facility_id) WHERE is_active AND scope_type = 'facility'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_care_consultation_fee_active_specialty ON care.consultation_fee (facility_id, specialty_id) WHERE is_active AND scope_type = 'specialty'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_care_consultation_fee_active_practitioner ON care.consultation_fee (facility_id, practitioner_id) WHERE is_active AND scope_type = 'practitioner'"
    )

    op.create_table(
        "invoice",
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
            nullable=False,
        ),
        sa.Column(
            "encounter_id", uuid, sa.ForeignKey("care.encounter.id"), nullable=False
        ),
        sa.Column(
            "patient_id", uuid, sa.ForeignKey("identity.patient.id"), nullable=False
        ),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id")),
        sa.Column(
            "consultation_fee_id", uuid, sa.ForeignKey("care.consultation_fee.id")
        ),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(24), server_default="issued", nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=False,
        ),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount_minor >= 0", name="ck_invoice_nonnegative"),
        sa.CheckConstraint("char_length(currency) = 3", name="ck_invoice_currency"),
        sa.CheckConstraint(
            "status IN ('issued', 'partially_paid', 'paid', 'voided')",
            name="ck_invoice_status",
        ),
        sa.UniqueConstraint("encounter_id", name="uq_care_invoice_encounter"),
        schema="care",
    )
    for column in (
        "organization_id",
        "facility_id",
        "encounter_id",
        "patient_id",
        "practitioner_id",
        "created_by_user_id",
        "status",
    ):
        op.create_index(f"ix_care_invoice_{column}", "invoice", [column], schema="care")
    op.create_index(
        "ix_care_invoice_facility_issued",
        "invoice",
        ["facility_id", "issued_at"],
        schema="care",
    )

    for table, timestamp, status_default in (
        ("payment", "received_at", "captured"),
        ("refund", "refunded_at", "completed"),
    ):
        columns = [
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
                nullable=False,
            ),
            sa.Column(
                "invoice_id", uuid, sa.ForeignKey("care.invoice.id"), nullable=False
            ),
        ]
        if table == "refund":
            columns.append(
                sa.Column(
                    "payment_id", uuid, sa.ForeignKey("care.payment.id"), nullable=False
                )
            )
        columns.extend(
            [
                sa.Column("amount_minor", sa.BigInteger(), nullable=False),
                sa.Column("currency", sa.String(3), nullable=False),
                sa.Column("idempotency_key", sa.String(128), nullable=False),
                sa.Column(
                    "status",
                    sa.String(24),
                    server_default=status_default,
                    nullable=False,
                ),
                sa.Column(timestamp, sa.DateTime(timezone=True), nullable=False),
                sa.Column(
                    "created_by_user_id",
                    uuid,
                    sa.ForeignKey("identity.user_account.id"),
                    nullable=False,
                ),
                sa.Column("voided_at", sa.DateTime(timezone=True)),
                sa.UniqueConstraint(
                    "organization_id",
                    "idempotency_key",
                    name=f"uq_care_{table}_org_idempotency",
                ),
                sa.CheckConstraint("amount_minor > 0", name=f"ck_{table}_positive"),
                sa.CheckConstraint(
                    "char_length(currency) = 3", name=f"ck_{table}_currency"
                ),
                sa.CheckConstraint(
                    "status IN ('captured', 'voided')"
                    if table == "payment"
                    else "status IN ('completed', 'voided')",
                    name=f"ck_{table}_status",
                ),
            ]
        )
        if table == "refund":
            columns.append(sa.Column("reason", sa.Text()))
        op.create_table(table, *columns, schema="care")
        for column in (
            "organization_id",
            "facility_id",
            "invoice_id",
            "status",
            "created_by_user_id",
        ):
            op.create_index(f"ix_care_{table}_{column}", table, [column], schema="care")
        if table == "refund":
            op.create_index(
                "ix_care_refund_payment_id", table, ["payment_id"], schema="care"
            )
        op.create_index(
            f"ix_care_{table}_facility_{timestamp.removesuffix('_at')}",
            table,
            ["facility_id", timestamp],
            schema="care",
        )


def downgrade() -> None:
    op.drop_table("refund", schema="care")
    op.drop_table("payment", schema="care")
    op.drop_table("invoice", schema="care")
    op.drop_table("consultation_fee", schema="care")
