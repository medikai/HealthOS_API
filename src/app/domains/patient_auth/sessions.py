"""Patient portal sessions (opaque cookie, server-side revocation).

Tokens are random 384-bit values; only their SHA-256 digest is stored. Session
creation always rotates away any presented token so a pre-auth cookie cannot be
fixed into an authenticated session. Staff sessions are untouched.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.identity import PatientPortalAccount, PatientPortalSession


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PatientSessionError(RuntimeError):
    code = "PATIENT_SESSION_ERROR"


class PatientSessionService:
    async def create_session(
        self,
        db: AsyncSession,
        *,
        account: PatientPortalAccount,
        ttl_seconds: int,
        rotate_token: str | None = None,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> tuple[str, PatientPortalSession]:
        now = datetime.now(UTC)
        if rotate_token:
            await self.revoke(db, rotate_token, reason="rotated", now=now)
        token = secrets.token_urlsafe(48)
        session = PatientPortalSession(
            patient_account_id=account.id,
            token_hash=hash_session_token(token),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + timedelta(seconds=max(60, ttl_seconds)),
            last_seen_at=now,
        )
        db.add(session)
        account.last_login_at = now
        account.updated_at = now
        await db.flush()
        return token, session

    async def resolve(
        self, db: AsyncSession, token: str | None
    ) -> PatientPortalSession | None:
        if not token:
            return None
        return await db.scalar(
            select(PatientPortalSession).where(
                PatientPortalSession.token_hash == hash_session_token(token),
                PatientPortalSession.revoked_at.is_(None),
                PatientPortalSession.expires_at > datetime.now(UTC),
            )
        )

    async def revoke(
        self,
        db: AsyncSession,
        token: str | None,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        if not token:
            return False
        moment = now or datetime.now(UTC)
        result = await db.execute(
            update(PatientPortalSession)
            .where(
                PatientPortalSession.token_hash == hash_session_token(token),
                PatientPortalSession.revoked_at.is_(None),
            )
            .values(revoked_at=moment, revoke_reason=reason[:64])
        )
        return bool(result.rowcount)

    async def revoke_all_for_account(
        self,
        db: AsyncSession,
        account_id: UUID,
        *,
        reason: str,
        except_session_id: UUID | None = None,
    ) -> int:
        now = datetime.now(UTC)
        statement = (
            update(PatientPortalSession)
            .where(
                PatientPortalSession.patient_account_id == account_id,
                PatientPortalSession.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoke_reason=reason[:64])
        )
        if except_session_id is not None:
            statement = statement.where(PatientPortalSession.id != except_session_id)
        result = await db.execute(statement)
        return int(result.rowcount or 0)
