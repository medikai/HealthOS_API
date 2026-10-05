"""Salutation reference data and person-identity validation.

Salutation is a person-identity concept. It is stored on ``identity.salutation``
and referenced by ``identity.person.salutation_id``. It must never be inferred
from profession, designation, RBAC role, specialty, gender or name.
"""

import uuid as uuid_pkg
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.identity import Salutation

SALUTATIONS: tuple[dict[str, object], ...] = (
    {"code": "DR", "display_name": "Doctor", "abbreviation": "Dr.", "sort_order": 10},
    {"code": "MR", "display_name": "Mister", "abbreviation": "Mr.", "sort_order": 20},
    {"code": "MS", "display_name": "Ms", "abbreviation": "Ms.", "sort_order": 30},
    {"code": "MRS", "display_name": "Mrs", "abbreviation": "Mrs.", "sort_order": 40},
    {"code": "MX", "display_name": "Mx", "abbreviation": "Mx.", "sort_order": 50},
    {"code": "PROF", "display_name": "Professor", "abbreviation": "Prof.", "sort_order": 60},
)


def salutation_uuid(code: str) -> uuid_pkg.UUID:
    """Deterministic reference UUID for a salutation code."""

    return uuid5(NAMESPACE_URL, f"healthos:salutation:{code}")


def _salutation_table(schema: str | None) -> sa.Table:
    return sa.Table(
        "salutation",
        sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("abbreviation", sa.String(16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        schema=schema,
    )


def seed_salutations(connection: sa.Connection, schema: str | None = "identity") -> int:
    """Idempotently upsert the initial salutation master rows by stable code.

    Running repeatedly never creates duplicates because rows are matched on the
    stable business identifier ``code``.
    """

    table = _salutation_table(schema)
    existing = set(connection.execute(sa.select(table.c.code)).scalars().all())
    inserted = 0
    for row in SALUTATIONS:
        if row["code"] in existing:
            continue
        connection.execute(table.insert().values(id=salutation_uuid(str(row["code"])), is_active=True, **row))
        inserted += 1
    return inserted


async def resolve_active_salutation(db: AsyncSession, salutation_id: uuid_pkg.UUID | None) -> Salutation | None:
    """Validate an optional salutation reference against active master data."""

    if salutation_id is None:
        return None
    record = await db.get(Salutation, salutation_id)
    if record is None or not record.is_active:
        raise HTTPException(status_code=422, detail="Salutation does not exist or is inactive.")
    return record
