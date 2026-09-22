"""add appointment optimistic version and structured audit details"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260922_27"
down_revision: str | None = "20260922_26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("appointment", sa.Column("version", sa.Integer(), server_default="1", nullable=False), schema="care")
    op.add_column("audit_log", sa.Column("details", postgresql.JSONB(), nullable=True), schema="governance")


def downgrade() -> None:
    op.drop_column("audit_log", "details", schema="governance")
    op.drop_column("appointment", "version", schema="care")
