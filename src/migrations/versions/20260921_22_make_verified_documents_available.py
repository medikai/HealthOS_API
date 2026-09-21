"""make verified patient documents available

Revision ID: 20260921_22
Revises: 20260921_21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_22"
down_revision: str | None = "20260921_21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE care.patient_document "
            "SET status = 'available', updated_at = CURRENT_TIMESTAMP "
            "WHERE status = 'processing' AND deleted_at IS NULL"
        )
    )


def downgrade() -> None:
    # Verified documents cannot safely be distinguished from later available rows.
    pass
