"""Idempotent seed for the identity.salutation person-identity master.

Run with: python -m scripts.seed_salutations
"""

import asyncio

from sqlalchemy import select

from src.app.core.db.database import local_session
from src.app.domains.identity.salutations import SALUTATIONS
from src.app.models.identity import Salutation


async def _seed() -> None:
    async with local_session() as db:
        existing = set((await db.scalars(select(Salutation.code))).all())
        inserted = 0
        for row in SALUTATIONS:
            if row["code"] in existing:
                continue
            db.add(Salutation(**row))
            inserted += 1
        await db.commit()
        print(f"Salutations inserted: {inserted}; expected total: {len(SALUTATIONS)}.")


if __name__ == "__main__":
    asyncio.run(_seed())
