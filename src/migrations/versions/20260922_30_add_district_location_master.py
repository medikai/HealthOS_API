"""add district location master

Revision ID: 20260922_30
Revises: 20260922_29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260922_30"
down_revision: str | None = "20260922_29"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "district",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("state_id", uuid, sa.ForeignKey("platform.state.id"), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("state_id", "code", name="uq_platform_district_state_code"),
        schema="platform",
    )
    op.create_index("ix_platform_district_state_id", "district", ["state_id"], schema="platform")
    op.add_column("city", sa.Column("district_id", uuid, sa.ForeignKey("platform.district.id"), nullable=True), schema="platform")
    op.create_index("ix_platform_city_district_id", "city", ["district_id"], schema="platform")
    op.drop_constraint("uq_platform_city_state_name", "city", schema="platform", type_="unique")
    op.create_unique_constraint("uq_platform_city_district_name", "city", ["district_id", "normalized_name"], schema="platform")
    op.add_column("facility", sa.Column("district_id", uuid, sa.ForeignKey("platform.district.id"), nullable=True), schema="organization")
    op.create_index("ix_organization_facility_district_id", "facility", ["district_id"], schema="organization")


def downgrade() -> None:
    op.drop_index("ix_organization_facility_district_id", table_name="facility", schema="organization")
    op.drop_column("facility", "district_id", schema="organization")
    op.drop_constraint("uq_platform_city_district_name", "city", schema="platform", type_="unique")
    op.create_unique_constraint("uq_platform_city_state_name", "city", ["state_id", "normalized_name"], schema="platform")
    op.drop_index("ix_platform_city_district_id", table_name="city", schema="platform")
    op.drop_column("city", "district_id", schema="platform")
    op.drop_table("district", schema="platform")
