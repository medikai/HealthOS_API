import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.auth_session import LoginTransaction
from ..models.identity import AuthSession, LoginTransaction as LoginTransactionRecord


class CRUDAuthSessions:
    async def save_transaction(self, db: AsyncSession, transaction: LoginTransaction) -> None:
        db.add(
            LoginTransactionRecord(
                state=transaction.state,
                nonce=transaction.nonce,
                code_verifier=transaction.code_verifier,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await db.commit()

    async def pop_transaction(self, db: AsyncSession, state: str) -> LoginTransaction | None:
        result = await db.execute(
            delete(LoginTransactionRecord)
            .where(LoginTransactionRecord.state == state, LoginTransactionRecord.expires_at > datetime.now(UTC))
            .returning(LoginTransactionRecord.nonce, LoginTransactionRecord.code_verifier)
        )
        await db.commit()
        row = result.one_or_none()
        if row is None:
            return None
        return LoginTransaction(state=state, nonce=row.nonce, code_verifier=row.code_verifier)

    async def create_session(self, db: AsyncSession, values: dict[str, Any]) -> str:
        session_id = secrets.token_urlsafe(48)
        db.add(
            AuthSession(
                id=session_id,
                user_account_id=values["user_account_id"],
                id_token=values["id_token"],
                csrf_token=values["csrf_token"],
                expires_at=datetime.now(UTC) + timedelta(seconds=settings.AUTH_SESSION_TTL_SECONDS),
            )
        )
        await db.commit()
        return session_id

    async def get_session(self, db: AsyncSession, session_id: str | None) -> AuthSession | None:
        if not session_id:
            return None
        return await db.scalar(
            select(AuthSession).where(AuthSession.id == session_id, AuthSession.expires_at > datetime.now(UTC))
        )

    async def delete_session(self, db: AsyncSession, session_id: str | None) -> None:
        if session_id:
            await db.execute(delete(AuthSession).where(AuthSession.id == session_id))
            await db.commit()


crud_auth_sessions = CRUDAuthSessions()
