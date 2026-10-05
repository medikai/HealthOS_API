"""add salutation master and person salutation

Revision ID: 20260929_41
Revises: 20260925_40
"""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_41"
down_revision: str | None = "20260925_40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SALUTATIONS = (
    ("DR", "Doctor", "Dr.", 10),
    ("MR", "Mister", "Mr.", 20),
    ("MS", "Ms", "Ms.", 30),
    ("MRS", "Mrs", "Mrs.", 40),
    ("MX", "Mx", "Mx.", 50),
    ("PROF", "Professor", "Prof.", 60),
)

INSERT_SALUTATION = sa.text(
    "INSERT INTO identity.salutation "
    "(id, code, display_name, abbreviation, sort_order, is_active) "
    "VALUES (:id, :code, :display_name, :abbreviation, :sort_order, TRUE) "
    "ON CONFLICT (code) DO NOTHING"
)


def _salutation_id(code: str):
    return uuid5(NAMESPACE_URL, f"healthos:salutation:{code}")


def _seed_salutations(bind) -> None:
    for code, display_name, abbreviation, sort_order in SALUTATIONS:
        bind.execute(
            INSERT_SALUTATION,
            {
                "id": _salutation_id(code),
                "code": code,
                "display_name": display_name,
                "abbreviation": abbreviation,
                "sort_order": sort_order,
            },
        )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "salutation",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("abbreviation", sa.String(16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_identity_salutation_code"),
        schema="identity",
    )
    op.create_index("ix_identity_salutation_code", "salutation", ["code"], schema="identity")

    op.add_column("person", sa.Column("salutation_id", uuid, nullable=True), schema="identity")
    op.create_foreign_key(
        "fk_identity_person_salutation",
        "person",
        "salutation",
        ["salutation_id"],
        ["id"],
        source_schema="identity",
        referent_schema="identity",
        ondelete="SET NULL",
    )

    op.add_column("user_account", sa.Column("person_id", uuid, nullable=True), schema="identity")
    op.create_foreign_key(
        "fk_identity_user_account_person",
        "user_account",
        "person",
        ["person_id"],
        ["id"],
        source_schema="identity",
        referent_schema="identity",
        ondelete="SET NULL",
    )
    op.create_index("ix_identity_user_account_person_id", "user_account", ["person_id"], schema="identity")

    _seed_salutations(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_identity_user_account_person_id", table_name="user_account", schema="identity")
    op.drop_constraint("fk_identity_user_account_person", "user_account", schema="identity", type_="foreignkey")
    op.drop_column("user_account", "person_id", schema="identity")

    op.drop_constraint("fk_identity_person_salutation", "person", schema="identity", type_="foreignkey")
    op.drop_column("person", "salutation_id", schema="identity")

    op.drop_index("ix_identity_salutation_code", table_name="salutation", schema="identity")
    op.drop_table("salutation", schema="identity")
