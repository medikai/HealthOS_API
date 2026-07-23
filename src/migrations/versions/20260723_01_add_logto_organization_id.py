"""map HealthOS organizations to Logto organizations

Revision ID: 20260723_01
Revises: 20260722_01
Create Date: 2026-07-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260723_01"
down_revision: Union[str, None] = "20260722_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("logto_organization_id", sa.String(length=255), nullable=True), schema="organization")
    op.create_index(
        "ix_organization_organization_logto_organization_id",
        "organization",
        ["logto_organization_id"],
        unique=True,
        schema="organization",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_organization_logto_organization_id",
        table_name="organization",
        schema="organization",
    )
    op.drop_column("organization", "logto_organization_id", schema="organization")
