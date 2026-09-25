"""Local password recovery (auth-owned).

The local auth domain owns credentials; ``communication.email`` is used only
for delivery. OTPs are stored as keyed HMAC verifiers (never plain text), are
purpose/account/challenge bound, single-use and generation-bound. A verified
OTP yields only a short-lived single-use reset grant, never a login token.
Resetting bumps ``user_account.credentials_version`` so already-issued local
access tokens are rejected, and deletes BFF sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...core.config import Settings
from ...core.security import get_password_hash
from ...models.identity import (
    AuthSession,
    PasswordRecoveryChallenge,
    PasswordRecoveryGrant,
    PasswordRecoveryThrottle,
    UserAccount,
)
from ..communication.email.service import build_email_service
from ..communication.shared.errors import ProviderUnavailable


class RecoveryError(RuntimeError):
    code = "RECOVERY_ERROR"


class InvalidRecoveryCode(RecoveryError):
    code = "INCORRECT_CODE"

    def __init__(self, message: str, *, attempts_remaining: int | None = None) -> None:
        super().__init__(message)
        self.attempts_remaining = attempts_remaining


class ExpiredRecoveryCode(RecoveryError):
    code = "CODE_EXPIRED"


class RecoveryRateLimited(RecoveryError):
    code = "RATE_LIMITED"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many recovery requests.")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class InvalidRecoveryGrant(RecoveryError):
    code = "RESET_GRANT_INVALID"


def _digest(pepper: str, value: str) -> str:
    return hmac.new(pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_numeric_code(length: int) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(max(4, length)))


def generate_grant_token() -> str:
    return secrets.token_urlsafe(32)


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    visible = local[:1]
    return f"{visible}***@{domain}"


@dataclass(frozen=True)
class RecoveryPolicy:
    pepper: str
    code_ttl_seconds: int
    resend_cooldown_seconds: int
    max_attempts: int
    account_limit: int
    ip_limit: int
    throttle_window_seconds: int
    grant_ttl_seconds: int
    code_length: int

    @classmethod
    def from_settings(cls, settings: Settings) -> RecoveryPolicy:
        pepper = (
            settings.AUTH_RECOVERY_PEPPER.get_secret_value()
            if settings.AUTH_RECOVERY_PEPPER is not None
            else settings.SECRET_KEY.get_secret_value()
        )
        return cls(
            pepper=pepper,
            code_ttl_seconds=settings.AUTH_RECOVERY_CODE_TTL_SECONDS,
            resend_cooldown_seconds=settings.AUTH_RECOVERY_RESEND_COOLDOWN_SECONDS,
            max_attempts=settings.AUTH_RECOVERY_MAX_ATTEMPTS,
            account_limit=settings.AUTH_RECOVERY_ACCOUNT_LIMIT,
            ip_limit=settings.AUTH_RECOVERY_IP_LIMIT,
            throttle_window_seconds=settings.AUTH_RECOVERY_THROTTLE_WINDOW_SECONDS,
            grant_ttl_seconds=settings.AUTH_RECOVERY_GRANT_TTL_SECONDS,
            code_length=settings.AUTH_RECOVERY_CODE_LENGTH,
        )

    def code_verifier(self, challenge_id: UUID, user_account_id: UUID, code: str) -> str:
        return _digest(
            self.pepper,
            f"password_reset:{user_account_id}:{challenge_id}:{code}",
        )

    def key_hash(self, scope: str, value: str) -> str:
        return _digest(self.pepper, f"{scope}:{value}")


class RecoveryRepository:
    async def find_account_by_email(
        self, db: AsyncSession, email: str
    ) -> UserAccount | None:
        # Deterministic oldest-account resolution, matching login-by-email.
        return await db.scalar(
            select(UserAccount)
            .where(func.lower(UserAccount.email) == email.lower())
            .order_by(UserAccount.created_at)
            .limit(1)
        )

    async def latest_challenge(
        self, db: AsyncSession, user_account_id: UUID
    ) -> PasswordRecoveryChallenge | None:
        return await db.scalar(
            select(PasswordRecoveryChallenge)
            .where(PasswordRecoveryChallenge.user_account_id == user_account_id)
            .order_by(PasswordRecoveryChallenge.created_at.desc())
            .limit(1)
        )

    async def invalidate_active_challenges(
        self, db: AsyncSession, user_account_id: UUID, now: datetime
    ) -> None:
        await db.execute(
            update(PasswordRecoveryChallenge)
            .where(
                PasswordRecoveryChallenge.user_account_id == user_account_id,
                PasswordRecoveryChallenge.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )

    async def next_generation(self, db: AsyncSession, user_account_id: UUID) -> int:
        current = await db.scalar(
            select(func.coalesce(func.max(PasswordRecoveryChallenge.generation), 0)).where(
                PasswordRecoveryChallenge.user_account_id == user_account_id
            )
        )
        return int(current or 0)

    async def create_challenge(self, db: AsyncSession, **values) -> PasswordRecoveryChallenge:
        # ``id`` is init=False on the dataclass; assign an explicit id when the
        # caller pre-generated one (the code verifier is bound to it).
        challenge_id = values.pop("id", None)
        challenge = PasswordRecoveryChallenge(**values)
        if challenge_id is not None:
            challenge.id = challenge_id
        db.add(challenge)
        await db.flush()
        return challenge

    async def get_challenge(
        self, db: AsyncSession, challenge_id: UUID
    ) -> PasswordRecoveryChallenge | None:
        return await db.get(PasswordRecoveryChallenge, challenge_id)

    async def consume_challenge_atomic(
        self, db: AsyncSession, challenge_id: UUID, now: datetime
    ) -> bool:
        result = await db.execute(
            update(PasswordRecoveryChallenge)
            .where(
                PasswordRecoveryChallenge.id == challenge_id,
                PasswordRecoveryChallenge.consumed_at.is_(None),
                PasswordRecoveryChallenge.expires_at > now,
            )
            .values(consumed_at=now)
        )
        return bool(result.rowcount)

    async def create_grant(self, db: AsyncSession, **values) -> PasswordRecoveryGrant:
        grant = PasswordRecoveryGrant(**values)
        db.add(grant)
        await db.flush()
        return grant

    async def consume_grant_atomic(
        self, db: AsyncSession, token_hash: str, now: datetime
    ) -> PasswordRecoveryGrant | None:
        result = await db.execute(
            update(PasswordRecoveryGrant)
            .where(
                PasswordRecoveryGrant.token_hash == token_hash,
                PasswordRecoveryGrant.consumed_at.is_(None),
                PasswordRecoveryGrant.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(PasswordRecoveryGrant.id)
        )
        row = result.first()
        if row is None:
            return None
        return await db.get(PasswordRecoveryGrant, row[0])

    async def delete_account_sessions(self, db: AsyncSession, user_account_id: UUID) -> None:
        await db.execute(
            delete(AuthSession).where(AuthSession.user_account_id == user_account_id)
        )

    async def hit_throttle(
        self,
        db: AsyncSession,
        *,
        scope: str,
        key_hash: str,
        now: datetime,
        window_seconds: int,
    ) -> tuple[int, int]:
        row = await db.scalar(
            select(PasswordRecoveryThrottle).where(
                PasswordRecoveryThrottle.scope == scope,
                PasswordRecoveryThrottle.key_hash == key_hash,
            )
        )
        if row is None:
            row = PasswordRecoveryThrottle(
                scope=scope, key_hash=key_hash, window_start=now, count=0
            )
            db.add(row)
        elif (now - row.window_start).total_seconds() >= window_seconds:
            row.window_start = now
            row.count = 0
        row.count += 1
        await db.flush()
        elapsed = int((now - row.window_start).total_seconds())
        return row.count, max(1, window_seconds - elapsed)


class RecoveryService:
    def __init__(
        self,
        policy: RecoveryPolicy,
        *,
        repository: RecoveryRepository | None = None,
        email_service=None,
    ) -> None:
        self.policy = policy
        self.repository = repository or RecoveryRepository()
        self.email_service = email_service

    async def _enforce_throttle(
        self, db: AsyncSession, *, scope: str, key_hash: str, limit: int, now: datetime
    ) -> None:
        count, retry_after = await self.repository.hit_throttle(
            db,
            scope=scope,
            key_hash=key_hash,
            now=now,
            window_seconds=self.policy.throttle_window_seconds,
        )
        if count > limit:
            raise RecoveryRateLimited(retry_after)

    async def request_reset(
        self, db: AsyncSession, *, email: str, client_ip: str | None
    ) -> dict:
        if self.email_service is None or not self.email_service.configured:
            raise ProviderUnavailable(
                "zeptomail_not_configured", "Email transport is not configured."
            )
        now = datetime.now(UTC)
        email_norm = email.strip().lower()
        email_hash = self.policy.key_hash("account", email_norm)
        ip_hash = (
            self.policy.key_hash("ip", client_ip) if client_ip else None
        )
        if ip_hash is not None:
            await self._enforce_throttle(
                db, scope="ip", key_hash=ip_hash, limit=self.policy.ip_limit, now=now
            )
        await self._enforce_throttle(
            db, scope="account", key_hash=email_hash, limit=self.policy.account_limit, now=now
        )

        account = await self.repository.find_account_by_email(db, email_norm)
        # Secret material is generated regardless of account existence so the
        # response shape/timing stay neutral for known and unknown emails.
        code = generate_numeric_code(self.policy.code_length)
        challenge_id = uuid7()
        expires_at = now + timedelta(seconds=self.policy.code_ttl_seconds)
        resend_at = now + timedelta(seconds=self.policy.resend_cooldown_seconds)

        if account is not None and account.is_active:
            latest = await self.repository.latest_challenge(db, account.id)
            if latest is not None and (now - latest.created_at).total_seconds() < self.policy.resend_cooldown_seconds:
                raise RecoveryRateLimited(self.policy.resend_cooldown_seconds)
            await self.repository.invalidate_active_challenges(db, account.id, now)
            generation = await self.repository.next_generation(db, account.id) + 1
            verifier = self.policy.code_verifier(challenge_id, account.id, code)
            await self.repository.create_challenge(
                db,
                id=challenge_id,
                user_account_id=account.id,
                email_hash=email_hash,
                code_verifier=verifier,
                expires_at=expires_at,
                purpose="password_reset",
                generation=generation,
                attempts=0,
                max_attempts=self.policy.max_attempts,
                ip_hash=ip_hash,
            )
            await db.commit()
            await self._send_code(db, account, code, challenge_id, expires_at)
        else:
            await db.commit()

        return {
            "challenge_id": str(challenge_id),
            "masked_email": mask_email(email_norm),
            "code_length": self.policy.code_length,
            "numeric": True,
            "segmented": True,
            "expires_at": expires_at.isoformat(),
            "resend_available_at": resend_at.isoformat(),
        }

    async def _send_code(
        self,
        db: AsyncSession,
        account: UserAccount,
        code: str,
        challenge_id: UUID,
        expires_at: datetime,
    ) -> None:
        await self.email_service.enqueue_template(
            db,
            template_code="password_reset_code",
            to_email=account.email,
            to_name=account.display_name,
            dedup_key=f"password-reset:{account.id}:{challenge_id}",
            context={
                "recipient_name": account.display_name or "there",
                "code": code,
                "expires_minutes": str(self.policy.code_ttl_seconds // 60),
            },
            expires_at=expires_at,
        )
        await db.commit()

    async def verify_code(
        self, db: AsyncSession, *, challenge_id: UUID, code: str
    ) -> dict:
        now = datetime.now(UTC)
        challenge = await self.repository.get_challenge(db, challenge_id)
        if (
            challenge is None
            or challenge.consumed_at is not None
            or challenge.expires_at <= now
            or (challenge.attempts or 0) >= (challenge.max_attempts or 0)
        ):
            raise ExpiredRecoveryCode("The code is invalid or has expired.")
        verifier = self.policy.code_verifier(
            challenge.id, challenge.user_account_id, code.strip()
        )
        if not hmac.compare_digest(verifier, challenge.code_verifier):
            challenge.attempts = (challenge.attempts or 0) + 1
            if challenge.attempts >= (challenge.max_attempts or 0):
                challenge.consumed_at = now  # fail closed
            await db.commit()
            raise InvalidRecoveryCode(
                "The code is invalid or has expired.",
                attempts_remaining=max(
                    0, (challenge.max_attempts or 0) - (challenge.attempts or 0)
                ),
            )

        consumed = await self.repository.consume_challenge_atomic(db, challenge.id, now)
        if not consumed:
            raise ExpiredRecoveryCode("The code is invalid or has expired.")
        token = generate_grant_token()
        grant_expires = now + timedelta(seconds=self.policy.grant_ttl_seconds)
        await self.repository.create_grant(
            db,
            challenge_id=challenge.id,
            user_account_id=challenge.user_account_id,
            token_hash=self.policy.key_hash("grant", token),
            expires_at=grant_expires,
        )
        await db.commit()
        return {"grant": token, "expires_at": grant_expires.isoformat()}

    async def reset_password(self, db: AsyncSession, *, grant: str, password: str) -> dict:
        now = datetime.now(UTC)
        token_hash = self.policy.key_hash("grant", grant)
        grant_row = await self.repository.consume_grant_atomic(db, token_hash, now)
        if grant_row is None:
            raise InvalidRecoveryGrant("The reset link is invalid or has expired.")
        account = await db.get(UserAccount, grant_row.user_account_id)
        if account is None or not account.is_active:
            raise InvalidRecoveryGrant("The reset link is invalid or has expired.")
        account.password_hash = get_password_hash(password)
        account.credentials_version = (account.credentials_version or 1) + 1
        account.updated_at = now
        await self.repository.delete_account_sessions(db, account.id)
        await db.commit()
        try:
            await self.email_service.enqueue_template(
                db,
                template_code="password_changed",
                to_email=account.email,
                to_name=account.display_name,
                dedup_key=f"password-changed:{account.id}:{account.credentials_version}",
                context={"recipient_name": account.display_name or "there"},
            )
            await db.commit()
        except Exception:  # noqa: BLE001 - notification is best-effort, reset already committed
            await db.rollback()
        return {
            "message": "Password has been reset. Sign in with your new password.",
            "sessions_revoked": True,
        }


def build_recovery_service(settings: Settings) -> RecoveryService:
    return RecoveryService(
        RecoveryPolicy.from_settings(settings),
        email_service=build_email_service(settings),
    )
