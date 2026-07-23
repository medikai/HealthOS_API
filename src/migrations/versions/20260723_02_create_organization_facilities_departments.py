"""create organization facilities and departments

Revision ID: 20260723_02
Revises: 20260723_01
Create Date: 2026-07-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260723_02"
down_revision: Union[str, None] = "20260723_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "facility",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.organization.id"]),
        sa.UniqueConstraint("organization_id", "code", name="uq_facility_organization_code"),
        schema="organization",
    )
    op.create_index("ix_organization_facility_organization_id", "facility", ["organization_id"], schema="organization")
    op.create_index("ix_organization_facility_code", "facility", ["code"], schema="organization")
    op.create_table(
        "department",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("facility_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.organization.id"]),
        sa.ForeignKeyConstraint(["facility_id"], ["organization.facility.id"]),
        sa.UniqueConstraint("organization_id", "code", name="uq_department_organization_code"),
        schema="organization",
    )
    op.create_index("ix_organization_department_organization_id", "department", ["organization_id"], schema="organization")
    op.create_index("ix_organization_department_facility_id", "department", ["facility_id"], schema="organization")
    op.create_index("ix_organization_department_code", "department", ["code"], schema="organization")


def downgrade() -> None:
    op.drop_index("ix_organization_department_code", table_name="department", schema="organization")
    op.drop_index("ix_organization_department_facility_id", table_name="department", schema="organization")
    op.drop_index("ix_organization_department_organization_id", table_name="department", schema="organization")
    op.drop_table("department", schema="organization")
    op.drop_index("ix_organization_facility_code", table_name="facility", schema="organization")
    op.drop_index("ix_organization_facility_organization_id", table_name="facility", schema="organization")
    op.drop_table("facility", schema="organization")
