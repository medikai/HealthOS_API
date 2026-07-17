from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.identity import UserAccount


class CRUDUserAccounts:
    async def get_by_logto_user_id(self, db: AsyncSession, logto_user_id: str) -> UserAccount | None:
        result = await db.execute(select(UserAccount).where(UserAccount.logto_user_id == logto_user_id))
        return result.scalar_one_or_none()

    async def upsert_from_logto(self, db: AsyncSession, claims: dict[str, Any]) -> UserAccount:
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("Logto user information did not include a subject.")

        account = await self.get_by_logto_user_id(db, subject)
        values = {
            "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
            "display_name": _display_name(claims),
            "avatar_url": claims.get("picture") if isinstance(claims.get("picture"), str) else None,
        }
        if account is None:
            account = UserAccount(logto_user_id=subject, **values)
            db.add(account)
        else:
            for field, value in values.items():
                setattr(account, field, value)
            account.updated_at = datetime.now(UTC)

        await db.commit()
        await db.refresh(account)
        return account


def _display_name(claims: dict[str, Any]) -> str | None:
    for key in ("name", "username", "preferred_username", "email"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


crud_user_accounts = CRUDUserAccounts()
