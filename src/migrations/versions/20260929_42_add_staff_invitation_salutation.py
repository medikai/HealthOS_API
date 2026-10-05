"""add salutation_id to organization.staff_invitation

Revision ID: 20260929_42
Revises: 20260929_41
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_42"
down_revision: str | None = "20260929_41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.add_column(
        "staff_invitation",
        sa.Column("salutation_id", uuid, nullable=True),
        schema="organization",
    )
    op.create_foreign_key(
        "fk_organization_staff_invitation_salutation",
        "staff_invitation",
        "salutation",
        ["salutation_id"],
        ["id"],
        source_schema="organization",
        referent_schema="identity",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_organization_staff_invitation_salutation_id",
        "staff_invitation",
        ["salutation_id"],
        schema="organization",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_staff_invitation_salutation_id",
        table_name="staff_invitation",
        schema="organization",
    )
    op.drop_constraint(
        "fk_organization_staff_invitation_salutation",
        "staff_invitation",
        schema="organization",
        type_="foreignkey",
    )
    op.drop_column("staff_invitation", "salutation_id", schema="organization")
