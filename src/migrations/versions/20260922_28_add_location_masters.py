"""add country, state, city masters and facility address

Revision ID: 20260922_28
Revises: 20260922_27
"""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260922_28"
down_revision: str | None = "20260922_27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDIA_STATES = (
    ("AN", "Andaman and Nicobar Islands"), ("AP", "Andhra Pradesh"),
    ("AR", "Arunachal Pradesh"), ("AS", "Assam"), ("BR", "Bihar"),
    ("CH", "Chandigarh"), ("CG", "Chhattisgarh"),
    ("DN", "Dadra and Nagar Haveli and Daman and Diu"), ("DL", "Delhi"),
    ("GA", "Goa"), ("GJ", "Gujarat"), ("HR", "Haryana"),
    ("HP", "Himachal Pradesh"), ("JK", "Jammu and Kashmir"),
    ("JH", "Jharkhand"), ("KA", "Karnataka"), ("KL", "Kerala"),
    ("LA", "Ladakh"), ("LD", "Lakshadweep"), ("MP", "Madhya Pradesh"),
    ("MH", "Maharashtra"), ("MN", "Manipur"), ("ML", "Meghalaya"),
    ("MZ", "Mizoram"), ("NL", "Nagaland"), ("OD", "Odisha"),
    ("PY", "Puducherry"), ("PB", "Punjab"), ("RJ", "Rajasthan"),
    ("SK", "Sikkim"), ("TN", "Tamil Nadu"), ("TS", "Telangana"),
    ("TR", "Tripura"), ("UP", "Uttar Pradesh"), ("UK", "Uttarakhand"),
    ("WB", "West Bengal"),
)


def _id(kind: str, *values: str):
    return uuid5(NAMESPACE_URL, ":".join(("healthos", kind, *values)))


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "country",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("code", sa.String(2), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("iso3_code", sa.String(3), nullable=True, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema="platform",
    )
    op.create_index("ix_platform_country_code", "country", ["code"], schema="platform")
    op.create_table(
        "state",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("country_id", uuid, sa.ForeignKey("platform.country.id"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("external_code", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("country_id", "code", name="uq_platform_state_country_code"),
        schema="platform",
    )
    op.create_index("ix_platform_state_country_id", "state", ["country_id"], schema="platform")
    op.create_table(
        "city",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("state_id", uuid, sa.ForeignKey("platform.state.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("normalized_name", sa.String(255), nullable=False),
        sa.Column("external_code", sa.String(64), nullable=True),
        sa.Column("is_user_added", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("state_id", "normalized_name", name="uq_platform_city_state_name"),
        schema="platform",
    )
    op.create_index("ix_platform_city_state_id", "city", ["state_id"], schema="platform")

    country_id = _id("country", "IN")
    bind = op.get_bind()
    bind.execute(
        sa.text("INSERT INTO platform.country (id, code, iso3_code, name) VALUES (:id, 'IN', 'IND', 'India')"),
        {"id": country_id},
    )
    bind.execute(
        sa.text("""
            INSERT INTO platform.state (id, country_id, code, external_code, name)
            VALUES (:id, :country_id, :code, :external_code, :name)
        """),
        [
            {"id": _id("state", "IN", code), "country_id": country_id, "code": code, "external_code": f"IN-{code}", "name": name}
            for code, name in INDIA_STATES
        ],
    )

    for name, column_type in (
        ("classification", sa.String(128)), ("street_address", sa.Text()),
        ("postal_code", sa.String(32)), ("phone", sa.String(32)),
    ):
        op.add_column("facility", sa.Column(name, column_type, nullable=True), schema="organization")
    for name, target in (
        ("country_id", "platform.country.id"),
        ("state_id", "platform.state.id"),
        ("city_id", "platform.city.id"),
    ):
        op.add_column("facility", sa.Column(name, uuid, sa.ForeignKey(target), nullable=True), schema="organization")
        op.create_index(f"ix_organization_facility_{name}", "facility", [name], schema="organization")

    # Existing HealthOS facilities are Indian; finer address data never existed to backfill safely.
    bind.execute(sa.text("UPDATE organization.facility SET country_id = :country_id WHERE country_id IS NULL"), {"country_id": country_id})


def downgrade() -> None:
    for name in ("city_id", "state_id", "country_id"):
        op.drop_index(f"ix_organization_facility_{name}", table_name="facility", schema="organization")
        op.drop_column("facility", name, schema="organization")
    for name in ("phone", "postal_code", "street_address", "classification"):
        op.drop_column("facility", name, schema="organization")
    op.drop_table("city", schema="platform")
    op.drop_table("state", schema="platform")
    op.drop_table("country", schema="platform")
