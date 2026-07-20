"""create organization and platform access tables

Revision ID: 20260720_01
Revises: 20260716_01
Create Date: 2026-07-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260720_01"
down_revision: Union[str, None] = "20260716_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS organization")
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    op.create_table(
        "organization",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_organization_name"),
        sa.UniqueConstraint("code", name="uq_organization_code"),
        schema="organization",
    )
    op.create_index("ix_organization_name", "organization", ["name"], schema="organization")
    op.create_index("ix_organization_code", "organization", ["code"], schema="organization")
    op.create_table(
        "staff_member",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.organization.id"]),
        sa.ForeignKeyConstraint(["user_account_id"], ["identity.user_account.id"]),
        sa.UniqueConstraint("organization_id", "user_account_id", name="uq_staff_member_organization_user"),
        schema="organization",
    )
    op.create_index("ix_staff_member_organization_id", "staff_member", ["organization_id"], schema="organization")
    op.create_index("ix_staff_member_user_account_id", "staff_member", ["user_account_id"], schema="organization")
    op.create_table(
        "staff_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("staff_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_code", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["staff_member_id"], ["organization.staff_member.id"]),
        sa.UniqueConstraint("staff_member_id", "role_code", name="uq_staff_assignment_member_role"),
        schema="organization",
    )
    op.create_index("ix_staff_assignment_staff_member_id", "staff_assignment", ["staff_member_id"], schema="organization")
    op.create_index("ix_staff_assignment_role_code", "staff_assignment", ["role_code"], schema="organization")
    op.create_table(
        "feature",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_feature_code"),
        schema="platform",
    )
    op.create_index("ix_feature_code", "feature", ["code"], schema="platform")
    op.create_table(
        "feature_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("feature_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.organization.id"]),
        sa.ForeignKeyConstraint(["feature_id"], ["platform.feature.id"]),
        sa.UniqueConstraint("organization_id", "feature_id", name="uq_feature_assignment_organization_feature"),
        schema="platform",
    )
    op.create_index("ix_feature_assignment_organization_id", "feature_assignment", ["organization_id"], schema="platform")
    op.create_index("ix_feature_assignment_feature_id", "feature_assignment", ["feature_id"], schema="platform")


def downgrade() -> None:
    op.drop_table("feature_assignment", schema="platform")
    op.drop_table("feature", schema="platform")
    op.drop_table("staff_assignment", schema="organization")
    op.drop_table("staff_member", schema="organization")
    op.drop_table("organization", schema="organization")
    op.execute("DROP SCHEMA IF EXISTS platform")
    op.execute("DROP SCHEMA IF EXISTS organization")
