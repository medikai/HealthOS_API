"""create patient document attachments

Revision ID: 20260921_21
Revises: 20260921_20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260921_21"
down_revision: str | None = "20260921_20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "patient_document",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "organization_id",
            uuid,
            sa.ForeignKey("organization.organization.id"),
            nullable=False,
        ),
        sa.Column(
            "patient_id", uuid, sa.ForeignKey("identity.patient.id"), nullable=False
        ),
        sa.Column(
            "encounter_id", uuid, sa.ForeignKey("care.encounter.id"), nullable=False
        ),
        sa.Column(
            "uploaded_by_user_id",
            uuid,
            sa.ForeignKey("identity.user_account.id"),
            nullable=False,
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("declared_mime_type", sa.String(64), nullable=False),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="uploading"),
        sa.Column("mime_type", sa.String(64)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("sha256", sa.String(64)),
        sa.Column("storage_generation", sa.String(64)),
        sa.Column("rejection_reason", sa.String(255)),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("scanned_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "deleted_by_user_id", uuid, sa.ForeignKey("identity.user_account.id")
        ),
        sa.Column("storage_deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "category IN ('lab_report', 'imaging_report', 'prescription', 'discharge_summary', 'other')",
            name="ck_care_patient_document_category",
        ),
        sa.CheckConstraint(
            "status IN ('uploading', 'processing', 'available', 'rejected')",
            name="ck_care_patient_document_status",
        ),
        schema="care",
    )
    for column in (
        "organization_id",
        "patient_id",
        "encounter_id",
        "uploaded_by_user_id",
        "status",
        "deleted_at",
    ):
        op.create_index(
            f"ix_care_patient_document_{column}",
            "patient_document",
            [column],
            schema="care",
        )
    op.create_index(
        "ix_care_patient_document_patient_date",
        "patient_document",
        ["patient_id", "document_date"],
        schema="care",
    )


def downgrade() -> None:
    op.drop_table("patient_document", schema="care")
