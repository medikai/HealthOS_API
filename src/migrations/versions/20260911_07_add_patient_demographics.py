"""add frontend-compatible patient demographics"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "20260911_07"
down_revision: Union[str, None] = "20260910_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column("person", sa.Column("date_of_birth", sa.String(10)), schema="identity")
    op.add_column("person", sa.Column("gender", sa.String(32)), schema="identity")

def downgrade() -> None:
    op.drop_column("person", "gender", schema="identity")
    op.drop_column("person", "date_of_birth", schema="identity")
