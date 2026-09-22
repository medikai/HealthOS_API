"""add patient search and keyset browsing indexes

Revision ID: 20260922_24
Revises: 20260921_23
Create Date: 2026-09-22 10:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260922_24"
down_revision: str | None = "20260921_23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Retain extension prerequisite for any environment where pg_trgm is not pre-installed
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))

    # 2. Add pg_trgm GIN index matching the exact normalized immutable full-name expression
    # Note: Expression uses immutable string concatenation (first_name || ' ' || coalesce(last_name, ''))
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_identity_person_trgm_name
            ON identity.person
            USING gin (lower(first_name || ' ' || coalesce(last_name, '')) gin_trgm_ops);
            """
        )
    )

    # 3. Add tenant-leading ordering index for deterministic keyset browsing and pagination
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_identity_person_org_lower_names
            ON identity.person (organization_id, lower(first_name), lower(coalesce(last_name, '')), id);
            """
        )
    )


def downgrade() -> None:
    # Drop only newly created indexes
    op.drop_index(
        "ix_identity_person_org_lower_names",
        table_name="person",
        schema="identity",
        if_exists=True,
    )
    op.drop_index(
        "ix_identity_person_trgm_name",
        table_name="person",
        schema="identity",
        if_exists=True,
    )
