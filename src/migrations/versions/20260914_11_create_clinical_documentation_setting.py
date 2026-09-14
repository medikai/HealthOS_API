"""create clinical_documentation_setting table"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260914_11"
down_revision: Union[str, None] = "20260914_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")

    op.create_table(
        "clinical_documentation_setting",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("triage_expanded_default", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("vitals_config", json_type, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("soap_config", json_type, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("custom_sections", json_type, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema="care",
    )
    op.create_index(
        "ix_care_clinical_doc_setting_org_id",
        "clinical_documentation_setting",
        ["organization_id"],
        schema="care",
    )
    op.create_index(
        "ix_care_clinical_doc_setting_facility_id",
        "clinical_documentation_setting",
        ["facility_id"],
        schema="care",
    )
    op.create_index(
        "uq_care_doc_setting_org_default",
        "clinical_documentation_setting",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("facility_id IS NULL"),
        schema="care",
    )
    op.create_index(
        "uq_care_doc_setting_org_facility",
        "clinical_documentation_setting",
        ["organization_id", "facility_id"],
        unique=True,
        postgresql_where=sa.text("facility_id IS NOT NULL"),
        schema="care",
    )


def downgrade() -> None:
    op.drop_table("clinical_documentation_setting", schema="care")

