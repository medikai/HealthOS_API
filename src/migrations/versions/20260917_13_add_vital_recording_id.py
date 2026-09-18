"""add vital recording identifier"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260917_13"
down_revision: Union[str, None] = "20260917_12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "vital", sa.Column("recording_id", postgresql.UUID(as_uuid=True)), schema="care"
    )
    op.create_index(
        "ix_care_vital_recording_id", "vital", ["recording_id"], schema="care"
    )


def downgrade() -> None:
    op.drop_index("ix_care_vital_recording_id", table_name="vital", schema="care")
    op.drop_column("vital", "recording_id", schema="care")
