"""Add explicit consultation compensation and recorded payout ledger.

Revision ID: 20260923_34
Revises: 20260923_33
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260923_34"
down_revision = "20260923_33"
branch_labels = depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.add_column("payment", sa.Column("provenance", sa.String(24), nullable=False,
                                       server_default="unknown"), schema="care")
    op.create_check_constraint("ck_payment_provenance", "payment",
                               "provenance IN ('unknown', 'recorded', 'synthetic_demo')", schema="care")
    op.execute("UPDATE care.payment AS p SET provenance = 'synthetic_demo' FROM care.invoice AS i WHERE p.invoice_id = i.id AND p.idempotency_key = 'demo-encounter-' || i.encounter_id::text")
    op.create_table("compensation_policy",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("basis_points", sa.Integer, nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),
        sa.Column("created_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("basis_points BETWEEN 1 AND 10000", name="ck_compensation_policy_rate"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="ck_compensation_policy_dates"),
        schema="care")
    op.create_index("ix_compensation_policy_scope", "compensation_policy",
                    ["organization_id", "facility_id", "practitioner_id", "currency", "effective_from"], schema="care")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("ALTER TABLE care.compensation_policy ADD CONSTRAINT ex_compensation_policy_period EXCLUDE USING gist (organization_id WITH =, facility_id WITH =, practitioner_id WITH =, currency WITH =, daterange(effective_from, effective_to + 1, '[)') WITH &&)")
    op.create_table("earning_entry",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("source_id", uuid, nullable=False),
        sa.Column("payment_id", uuid, sa.ForeignKey("care.payment.id"), nullable=False),
        sa.Column("policy_id", uuid, sa.ForeignKey("care.compensation_policy.id"), nullable=False),
        sa.Column("basis_points", sa.Integer, nullable=False),
        sa.Column("base_minor", sa.BigInteger, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_type", "source_id", name="uq_earning_source"),
        sa.CheckConstraint("source_type IN ('payment', 'refund', 'payment_void', 'refund_void')", name="ck_earning_source"),
        schema="care")
    op.create_index("ix_earning_scope_time", "earning_entry",
                    ["organization_id", "facility_id", "practitioner_id", "currency", "occurred_at"], schema="care")
    op.create_index("ix_earning_payment", "earning_entry", ["payment_id"], schema="care")
    op.create_table("payout_entry",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=False),
        sa.Column("practitioner_id", uuid, sa.ForeignKey("identity.practitioner.id"), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method", sa.String(80), nullable=False),
        sa.Column("reference", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_by_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=False),
        sa.Column("reverses_id", uuid, sa.ForeignKey("care.payout_entry.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_payout_idempotency"),
        sa.CheckConstraint("amount_minor <> 0", name="ck_payout_nonzero"),
        sa.CheckConstraint("kind IN ('payout', 'reversal')", name="ck_payout_kind"), schema="care")
    op.create_index("ix_payout_scope_time", "payout_entry",
                    ["organization_id", "facility_id", "practitioner_id", "currency", "paid_at"], schema="care")
    op.execute("CREATE UNIQUE INDEX uq_payout_one_reversal ON care.payout_entry (reverses_id) WHERE reverses_id IS NOT NULL")
    op.create_table("payout_allocation",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("payout_id", uuid, sa.ForeignKey("care.payout_entry.id"), nullable=False),
        sa.Column("earning_id", uuid, sa.ForeignKey("care.earning_entry.id"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("payout_id", "earning_id", name="uq_payout_allocation_pair"),
        sa.CheckConstraint("amount_minor > 0", name="ck_payout_allocation_positive"), schema="care")
    op.create_index("ix_payout_allocation_earning", "payout_allocation", ["earning_id"], schema="care")


def downgrade() -> None:
    op.drop_table("payout_allocation", schema="care")
    op.drop_table("payout_entry", schema="care")
    op.drop_table("earning_entry", schema="care")
    op.drop_table("compensation_policy", schema="care")
    op.drop_column("payment", "provenance", schema="care")
