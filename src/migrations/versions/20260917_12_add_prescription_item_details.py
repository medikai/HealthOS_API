"""add prescription item details"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_12"
down_revision: Union[str, None] = "20260914_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "prescription_item", sa.Column("strength", sa.String(128)), schema="care"
    )
    op.add_column(
        "prescription_item", sa.Column("brand", sa.String(255)), schema="care"
    )
    op.add_column("prescription_item", sa.Column("route", sa.String(64)), schema="care")
    op.add_column(
        "prescription_item", sa.Column("timing", sa.String(128)), schema="care"
    )
    op.add_column(
        "prescription_item", sa.Column("instructions", sa.Text()), schema="care"
    )


def downgrade() -> None:
    op.drop_column("prescription_item", "instructions", schema="care")
    op.drop_column("prescription_item", "timing", schema="care")
    op.drop_column("prescription_item", "route", schema="care")
    op.drop_column("prescription_item", "brand", schema="care")
    op.drop_column("prescription_item", "strength", schema="care")
