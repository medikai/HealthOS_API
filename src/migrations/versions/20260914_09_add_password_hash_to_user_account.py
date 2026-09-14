"""add password_hash to user_account"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "20260914_09"
down_revision: Union[str, None] = "20260911_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_account", sa.Column("password_hash", sa.String(255), nullable=True), schema="identity")


def downgrade() -> None:
    op.drop_column("user_account", "password_hash", schema="identity")

