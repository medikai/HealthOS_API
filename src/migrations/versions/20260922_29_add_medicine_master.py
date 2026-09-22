"""add searchable medicine master

Revision ID: 20260922_29
Revises: 20260922_28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260922_29"
down_revision: str | None = "20260922_28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "medicine",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("source", sa.String(64), nullable=False, server_default="manual"),
        sa.Column("source_id", sa.String(128), nullable=True),
        sa.Column("manufacturer_name", sa.String(500), nullable=True),
        sa.Column("medicine_type", sa.String(64), nullable=True),
        sa.Column("pack_size_label", sa.String(255), nullable=True),
        sa.Column("composition1", sa.String(500), nullable=True),
        sa.Column("composition2", sa.String(500), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("is_discontinued", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("price IS NULL OR price >= 0", name="ck_platform_medicine_price"),
        sa.UniqueConstraint("source", "source_id", name="uq_platform_medicine_source_id"),
        schema="platform",
    )
    op.execute(sa.text("""
        CREATE INDEX ix_platform_medicine_trgm_name
        ON platform.medicine USING gin (lower(name) gin_trgm_ops)
    """))
    op.execute(sa.text("""
        CREATE INDEX ix_platform_medicine_trgm_composition
        ON platform.medicine USING gin (
            lower(coalesce(composition1, '') || ' ' || coalesce(composition2, '')) gin_trgm_ops
        )
    """))
    op.alter_column("prescription_item", "medicine_name", type_=sa.String(500), schema="care")
    op.alter_column("prescription_item", "brand", type_=sa.String(500), schema="care")
    op.add_column(
        "prescription_item",
        sa.Column("medicine_id", uuid, sa.ForeignKey("platform.medicine.id", ondelete="SET NULL"), nullable=True),
        schema="care",
    )
    op.create_index("ix_care_prescription_item_medicine_id", "prescription_item", ["medicine_id"], schema="care")


def downgrade() -> None:
    op.drop_index("ix_care_prescription_item_medicine_id", table_name="prescription_item", schema="care")
    op.drop_column("prescription_item", "medicine_id", schema="care")
    op.alter_column("prescription_item", "brand", type_=sa.String(255), schema="care")
    op.alter_column("prescription_item", "medicine_name", type_=sa.String(255), schema="care")
    op.drop_table("medicine", schema="platform")
