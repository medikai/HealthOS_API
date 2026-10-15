"""add ABHA identifiers and per-operator patient view history

Revision ID: 20261015_48
Revises: 20261006_47

Additive change for the Patient Registry operator UX:

- ``identity.person.abha_number`` (14 canonical digits) and
  ``identity.person.abha_address`` (lowercase PHR handle), both nullable.
  Uniqueness is enforced per organization with partial unique indexes so
  existing rows without ABHA are unaffected.
- ``governance.patient_view`` stores the most recent access per
  (operator, patient) for the "Recently Viewed" tab. Per-view history remains
  in ``governance.audit_log``; no backfill is possible or required.

No existing values are replaced and no data is modified.

Manual DataGrip equivalent (run, then `alembic stamp 20261015_48`):

    ALTER TABLE identity.person ADD COLUMN IF NOT EXISTS abha_number varchar(14);
    ALTER TABLE identity.person ADD COLUMN IF NOT EXISTS abha_address varchar(64);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_person_org_abha_number
        ON identity.person (organization_id, abha_number)
        WHERE abha_number IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_person_org_abha_address
        ON identity.person (organization_id, lower(abha_address))
        WHERE abha_address IS NOT NULL;
    CREATE TABLE IF NOT EXISTS governance.patient_view (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL REFERENCES organization.organization,
        user_account_id uuid NOT NULL REFERENCES identity.user_account,
        patient_id uuid NOT NULL REFERENCES identity.patient,
        facility_id uuid REFERENCES organization.facility,
        last_viewed_at timestamptz NOT NULL
    );
    ALTER TABLE governance.patient_view
        ADD CONSTRAINT uq_governance_patient_view_user_patient
        UNIQUE (user_account_id, patient_id);
    CREATE INDEX IF NOT EXISTS ix_governance_patient_view_user_recent
        ON governance.patient_view (user_account_id, last_viewed_at);
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261015_48"
down_revision: str | None = "20261006_47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "person",
        sa.Column("abha_number", sa.String(length=14), nullable=True),
        schema="identity",
    )
    op.add_column(
        "person",
        sa.Column("abha_address", sa.String(length=64), nullable=True),
        schema="identity",
    )
    op.create_index(
        "uq_identity_person_org_abha_number",
        "person",
        ["organization_id", "abha_number"],
        unique=True,
        schema="identity",
        postgresql_where=sa.text("abha_number IS NOT NULL"),
    )
    op.create_index(
        "uq_identity_person_org_abha_address",
        "person",
        ["organization_id", sa.text("lower(abha_address)")],
        unique=True,
        schema="identity",
        postgresql_where=sa.text("abha_address IS NOT NULL"),
    )
    op.create_table(
        "patient_view",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("user_account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("identity.user_account.id"), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("identity.patient.id"), nullable=False),
        sa.Column("facility_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_account_id", "patient_id", name="uq_governance_patient_view_user_patient"),
        schema="governance",
    )
    op.create_index(
        "ix_governance_patient_view_user_recent",
        "patient_view",
        ["user_account_id", "last_viewed_at"],
        schema="governance",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_governance_patient_view_user_recent",
        table_name="patient_view",
        schema="governance",
        if_exists=True,
    )
    op.drop_table("patient_view", schema="governance")
    op.drop_index(
        "uq_identity_person_org_abha_address",
        table_name="person",
        schema="identity",
        if_exists=True,
    )
    op.drop_index(
        "uq_identity_person_org_abha_number",
        table_name="person",
        schema="identity",
        if_exists=True,
    )
    op.drop_column("person", "abha_address", schema="identity")
    op.drop_column("person", "abha_number", schema="identity")
