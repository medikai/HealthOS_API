"""make care.receipt.issued_by_user_id nullable (patient-triggered receipts)

Revision ID: 20261006_45
Revises: 20261006_44
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261006_45"
down_revision: str | None = "20261006_44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "receipt",
        "issued_by_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
        schema="care",
    )


def downgrade() -> None:
    op.alter_column(
        "receipt",
        "issued_by_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        schema="care",
    )
