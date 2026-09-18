"""add SOAP custom field values"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260918_14"
down_revision: str | None = "20260917_13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.add_column(
        "soap_note",
        sa.Column(
            "custom_fields",
            json_type,
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        schema="care",
    )


def downgrade() -> None:
    op.drop_column("soap_note", "custom_fields", schema="care")
