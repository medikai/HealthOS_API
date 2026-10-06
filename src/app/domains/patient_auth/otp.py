"""Phone OTP challenges for the patient portal (auth-owned).

Reuses the recovery-domain *primitives* (HMAC-peppered verifier, single-use
consumption, aggregate throttling) without reusing its endpoint semantics or
grants: a verified patient OTP creates a patient session directly, and only via
the patient session service. The dev provider is allowlisted, inert in
production and never sends real SMS.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...core.config import EnvironmentOption, Settings
from ...models.identity import (
    PatientOtpChallenge,
    PatientOtpThrottle,
    PatientPortalAccount,
)
from .phone import normalize_phone

LOGIN_PURPOSE = "login"
PHONE_CHANGE_PURPOSE = "phone_change"
ALLOWED_PURPOSES = (LOGIN_PURPOSE, PHONE_CHANGE_PURPOSE)


class PatientOtpError(RuntimeError):
    code = "PATIENT_OTP_ERROR"


class InvalidOtpCode(PatientOtpError):
    code = "OTP_INVALID"

    def __init__(self, message: str, *, attempts_remaining: int | None = None) -> None:
        super().__init__(message)
        self.attempts_remaining = attempts_remaining


class ExpiredOtpCode(PatientOtpError):
    code = "OTP_EXPIRED"


class OtpRateLimited(PatientOtpError):
    code = "OTP_RATE_LIMITED"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many OTP requests.")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class OtpProviderUnavailable(PatientOtpError):
    code = "OTP_PROVIDER_UNAVAILABLE"

    def __init__(self, reason: str) -> None:
        super().__init__("OTP delivery is unavailable.")
        self.reason = reason


class OtpSandboxUnavailable(PatientOtpError):
    """Raised when a sandbox OTP request is not permitted for this phone."""

    code = "OTP_SANDBOX_UNAVAILABLE"

    def __init__(self, reason: str) -> None:
        super().__init__("Sandbox OTP is not available for this request.")
        self.reason = reason


def _digest(pepper: str, value: str) -> str:
    return hmac.new(pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_numeric_code(length: int) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(max(4, length)))


@dataclass(frozen=True)
class OtpPolicy:
    pepper: str
    code_ttl_seconds: int
    resend_cooldown_seconds: int
    max_attempts: int
    phone_limit: int
    ip_limit: int
    throttle_window_seconds: int
    code_length: int

    @classmethod
    def from_settings(cls, settings: Settings) -> OtpPolicy:
        pepper = (
            settings.PATIENT_OTP_PEPPER.get_secret_value()
            if settings.PATIENT_OTP_PEPPER is not None
            else settings.SECRET_KEY.get_secret_value()
        )
        return cls(
            pepper=pepper,
            code_ttl_seconds=settings.PATIENT_OTP_CODE_TTL_SECONDS,
            resend_cooldown_seconds=settings.PATIENT_OTP_RESEND_COOLDOWN_SECONDS,
            max_attempts=settings.PATIENT_OTP_MAX_ATTEMPTS,
            phone_limit=settings.PATIENT_OTP_PHONE_LIMIT,
            ip_limit=settings.PATIENT_OTP_IP_LIMIT,
            throttle_window_seconds=settings.PATIENT_OTP_THROTTLE_WINDOW_SECONDS,
            code_length=settings.PATIENT_OTP_CODE_LENGTH,
        )

    def phone_hash(self, canonical_phone: str) -> str:
        return _digest(self.pepper, f"patient_otp:phone:{canonical_phone}")

    def code_verifier(
        self, *, purpose: str, phone_hash: str, challenge_id: UUID, code: str
    ) -> str:
        return _digest(
            self.pepper,
            f"patient_otp:{purpose}:{phone_hash}:{challenge_id}:{code}",
        )

    def key_hash(self, scope: str, value: str) -> str:
        return _digest(self.pepper, f"patient_otp:{scope}:{value}")


class PatientOtpProvider(Protocol):
    name: str
    configured: bool

    def allows_sandbox(self, phone: str) -> bool: ...

    async def send_code(
        self,
        *,
        phone: str,
        code: str,
        challenge_id: UUID,
        purpose: str,
        expires_at: datetime,
    ) -> None: ...

    def exposed_code(self, challenge_id: UUID) -> str | None: ...


class UnconfiguredPatientOtpProvider:
    """Explicit no-delivery provider. Requests fail closed with 503."""

    name = "unconfigured"
    configured = False

    def allows_sandbox(self, phone: str) -> bool:
        return False

    async def send_code(self, **_: Any) -> None:
        raise OtpProviderUnavailable("otp_provider_unconfigured")

    def exposed_code(self, challenge_id: UUID) -> str | None:
        return None


class DevPatientOtpProvider:
    """Local-only synthetic provider.

    Never sends SMS. Delivery is restricted to allowlisted synthetic phone
    numbers and is unavailable in production regardless of configuration. Codes
    are kept in process memory only, are never logged, and are only echoed to
    the client when the local-only expose flag is explicitly enabled.
    """

    name = "dev"

    def __init__(
        self,
        *,
        environment: str,
        allowlist: list[str],
        expose_code: bool,
    ) -> None:
        normalized: set[str] = set()
        for item in allowlist:
            try:
                normalized.add(normalize_phone(item))
            except ValueError:
                continue
        self._allowlist = normalized
        self._environment = environment
        self._enabled = environment != EnvironmentOption.PRODUCTION.value and bool(
            normalized
        )
        self._expose = bool(expose_code) and environment == EnvironmentOption.LOCAL.value
        self._sent: dict[UUID, str] = {}

    @property
    def configured(self) -> bool:
        return self._enabled

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(self._allowlist)

    def allows(self, phone: str) -> bool:
        return phone in self._allowlist

    def allows_sandbox(self, phone: str) -> bool:
        """Sandbox mode is local-only and restricted to allowlisted numbers."""
        return self._environment == EnvironmentOption.LOCAL.value and self.allows(phone)

    async def send_code(
        self,
        *,
        phone: str,
        code: str,
        challenge_id: UUID,
        purpose: str,
        expires_at: datetime,
    ) -> None:
        if not self._enabled:
            raise OtpProviderUnavailable("dev_provider_disabled")
        if not self.allows(phone):
            return
        if len(self._sent) > 10_000:
            self._sent.clear()
        self._sent[challenge_id] = code

    def exposed_code(self, challenge_id: UUID) -> str | None:
        if not self._expose:
            return None
        return self._sent.get(challenge_id)


def build_patient_otp_provider(settings: Settings) -> PatientOtpProvider:
    provider = (settings.PATIENT_OTP_PROVIDER or "unconfigured").strip().lower()
    if provider == "dev":
        dev = DevPatientOtpProvider(
            environment=settings.ENVIRONMENT.value,
            allowlist=list(settings.PATIENT_OTP_DEV_ALLOWLIST),
            expose_code=settings.PATIENT_OTP_DEV_EXPOSE_CODE,
        )
        if not dev.configured:
            logging.getLogger(__name__).warning(
                "Patient dev OTP provider is disabled (production environment or empty "
                "PATIENT_OTP_DEV_ALLOWLIST); OTP requests will fail closed with 503."
            )
        return dev
    return UnconfiguredPatientOtpProvider()


class PatientOtpRepository:
    async def find_account_by_phone(
        self, db: AsyncSession, phone: str
    ) -> PatientPortalAccount | None:
        return await db.scalar(
            select(PatientPortalAccount).where(PatientPortalAccount.phone == phone)
        )

    async def latest_challenge_for_phone(
        self, db: AsyncSession, phone_hash: str
    ) -> PatientOtpChallenge | None:
        return await db.scalar(
            select(PatientOtpChallenge)
            .where(PatientOtpChallenge.phone_hash == phone_hash)
            .order_by(PatientOtpChallenge.created_at.desc())
            .limit(1)
        )

    async def invalidate_active_challenges(
        self, db: AsyncSession, phone_hash: str, now: datetime
    ) -> None:
        await db.execute(
            update(PatientOtpChallenge)
            .where(
                PatientOtpChallenge.phone_hash == phone_hash,
                PatientOtpChallenge.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )

    async def next_generation(self, db: AsyncSession, phone_hash: str) -> int:
        current = await db.scalar(
            select(func.coalesce(func.max(PatientOtpChallenge.generation), 0)).where(
                PatientOtpChallenge.phone_hash == phone_hash
            )
        )
        return int(current or 0)

    async def create_challenge(
        self, db: AsyncSession, **values: Any
    ) -> PatientOtpChallenge:
        challenge_id = values.pop("id", None)
        challenge = PatientOtpChallenge(**values)
        if challenge_id is not None:
            challenge.id = challenge_id
        db.add(challenge)
        await db.flush()
        return challenge

    async def get_challenge(
        self, db: AsyncSession, challenge_id: UUID
    ) -> PatientOtpChallenge | None:
        return await db.get(PatientOtpChallenge, challenge_id)

    async def consume_challenge_atomic(
        self, db: AsyncSession, challenge_id: UUID, now: datetime
    ) -> bool:
        result = await db.execute(
            update(PatientOtpChallenge)
            .where(
                PatientOtpChallenge.id == challenge_id,
                PatientOtpChallenge.consumed_at.is_(None),
                PatientOtpChallenge.expires_at > now,
            )
            .values(consumed_at=now)
        )
        return bool(result.rowcount)

    async def purge_consumed_challenges(
        self, db: AsyncSession, phone_hash: str
    ) -> None:
        """Retention: keep challenge history bounded per phone identity."""
        await db.execute(
            delete(PatientOtpChallenge).where(
                PatientOtpChallenge.phone_hash == phone_hash,
                PatientOtpChallenge.consumed_at.is_not(None),
            )
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
            select(PatientOtpThrottle).where(
                PatientOtpThrottle.scope == scope,
                PatientOtpThrottle.key_hash == key_hash,
            )
        )
        if row is None:
            row = PatientOtpThrottle(scope=scope, key_hash=key_hash, window_start=now, count=0)
            db.add(row)
        elif (now - row.window_start).total_seconds() >= window_seconds:
            row.window_start = now
            row.count = 0
        row.count += 1
        await db.flush()
        elapsed = int((now - row.window_start).total_seconds())
        return row.count, max(1, window_seconds - elapsed)


class PatientOtpService:
    def __init__(
        self,
        policy: OtpPolicy,
        *,
        provider: PatientOtpProvider,
        repository: PatientOtpRepository | None = None,
    ) -> None:
        self.policy = policy
        self.provider = provider
        self.repository = repository or PatientOtpRepository()

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
            raise OtpRateLimited(retry_after)

    async def request_code(
        self,
        db: AsyncSession,
        *,
        phone: str,
        client_ip: str | None,
        purpose: str = LOGIN_PURPOSE,
        bound_account: PatientPortalAccount | None = None,
        sandbox: bool = False,
    ) -> dict[str, Any]:
        if purpose not in ALLOWED_PURPOSES:
            raise ValueError(f"Unsupported patient OTP purpose: {purpose}")
        canonical = normalize_phone(phone)
        if not self.provider.configured:
            raise OtpProviderUnavailable("otp_provider_unconfigured")
        sandbox_active = False
        if sandbox:
            sandbox_active = self.provider.allows_sandbox(canonical)
            if not sandbox_active:
                raise OtpSandboxUnavailable("sandbox_not_allowed")
        now = datetime.now(UTC)
        phone_hash = self.policy.phone_hash(canonical)
        ip_hash = self.policy.key_hash("ip", client_ip) if client_ip else None
        if not sandbox_active:
            if ip_hash is not None:
                await self._enforce_throttle(
                    db, scope="ip", key_hash=ip_hash, limit=self.policy.ip_limit, now=now
                )
            await self._enforce_throttle(
                db, scope="phone", key_hash=phone_hash, limit=self.policy.phone_limit, now=now
            )

        account = bound_account
        if account is None:
            account = await self.repository.find_account_by_phone(db, canonical)
            if account is not None and not account.is_active:
                account = None

        if not sandbox_active:
            latest = await self.repository.latest_challenge_for_phone(db, phone_hash)
            if latest is not None and latest.purpose == purpose:
                elapsed = (now - latest.created_at).total_seconds()
                if elapsed < self.policy.resend_cooldown_seconds:
                    raise OtpRateLimited(self.policy.resend_cooldown_seconds - int(elapsed))

        await self.repository.invalidate_active_challenges(db, phone_hash, now)
        generation = await self.repository.next_generation(db, phone_hash) + 1
        challenge_id = uuid7()
        code = generate_numeric_code(self.policy.code_length)
        expires_at = now + timedelta(seconds=self.policy.code_ttl_seconds)
        await self.repository.create_challenge(
            db,
            id=challenge_id,
            patient_account_id=account.id if account else None,
            phone_hash=phone_hash,
            code_verifier=self.policy.code_verifier(
                purpose=purpose, phone_hash=phone_hash, challenge_id=challenge_id, code=code
            ),
            purpose=purpose,
            expires_at=expires_at,
            generation=generation,
            attempts=0,
            max_attempts=self.policy.max_attempts,
            ip_hash=ip_hash,
        )
        await db.commit()
        # A first-time phone has no account yet: the code must still be
        # delivered so the owner can complete registration. The response shape
        # is identical whether or not an account exists.
        await self.provider.send_code(
            phone=canonical,
            code=code,
            challenge_id=challenge_id,
            purpose=purpose,
            expires_at=expires_at,
        )
        return self._response(
            challenge_id=challenge_id,
            expires_at=expires_at,
            resend_available_at=now + timedelta(seconds=self.policy.resend_cooldown_seconds),
            sandbox=sandbox_active,
        )

    async def resend_code(
        self,
        db: AsyncSession,
        *,
        challenge_id: UUID,
        client_ip: str | None,
        phone: str | None = None,
        sandbox: bool = False,
    ) -> dict[str, Any]:
        if not self.provider.configured:
            raise OtpProviderUnavailable("otp_provider_unconfigured")
        challenge = await self.repository.get_challenge(db, challenge_id)
        now = datetime.now(UTC)
        if (
            challenge is None
            or challenge.consumed_at is not None
            or challenge.expires_at <= now
            or (challenge.attempts or 0) >= (challenge.max_attempts or 0)
        ):
            raise ExpiredOtpCode("The code is invalid or has expired.")
        canonical: str | None = None
        if phone is not None:
            canonical = normalize_phone(phone)
            if challenge.phone_hash != self.policy.phone_hash(canonical):
                raise ExpiredOtpCode("The code is invalid or has expired.")

        sandbox_active = False
        if sandbox:
            sandbox_active = canonical is not None and self.provider.allows_sandbox(canonical)
            if not sandbox_active:
                raise OtpSandboxUnavailable("sandbox_not_allowed")

        ip_hash = self.policy.key_hash("ip", client_ip) if client_ip else None
        if not sandbox_active:
            elapsed = (now - challenge.created_at).total_seconds()
            if elapsed < self.policy.resend_cooldown_seconds:
                raise OtpRateLimited(self.policy.resend_cooldown_seconds - int(elapsed))

            if ip_hash is not None:
                await self._enforce_throttle(
                    db, scope="ip", key_hash=ip_hash, limit=self.policy.ip_limit, now=now
                )
            await self._enforce_throttle(
                db,
                scope="phone",
                key_hash=challenge.phone_hash,
                limit=self.policy.phone_limit,
                now=now,
            )

        account = (
            await db.get(PatientPortalAccount, challenge.patient_account_id)
            if challenge.patient_account_id
            else None
        )
        if account is not None and not account.is_active:
            account = None
        if account is None and phone is None:
            # The phone is required to re-deliver a code for an account-less
            # (first-time) challenge; without it the request cannot be honoured.
            raise ExpiredOtpCode("The code is invalid or has expired.")

        await self.repository.invalidate_active_challenges(db, challenge.phone_hash, now)
        generation = await self.repository.next_generation(db, challenge.phone_hash) + 1
        new_id = uuid7()
        code = generate_numeric_code(self.policy.code_length)
        expires_at = now + timedelta(seconds=self.policy.code_ttl_seconds)
        await self.repository.create_challenge(
            db,
            id=new_id,
            patient_account_id=account.id if account else None,
            phone_hash=challenge.phone_hash,
            code_verifier=self.policy.code_verifier(
                purpose=challenge.purpose,
                phone_hash=challenge.phone_hash,
                challenge_id=new_id,
                code=code,
            ),
            purpose=challenge.purpose,
            expires_at=expires_at,
            generation=generation,
            attempts=0,
            max_attempts=self.policy.max_attempts,
            ip_hash=ip_hash,
        )
        await db.commit()
        destination = account.phone if account is not None else normalize_phone(phone)
        await self.provider.send_code(
            phone=destination,
            code=code,
            challenge_id=new_id,
            purpose=challenge.purpose,
            expires_at=expires_at,
        )
        return self._response(
            challenge_id=new_id,
            expires_at=expires_at,
            resend_available_at=now + timedelta(seconds=self.policy.resend_cooldown_seconds),
            sandbox=sandbox_active,
        )

    async def verify_code(
        self,
        db: AsyncSession,
        *,
        challenge_id: UUID,
        phone: str,
        code: str,
        purpose: str = LOGIN_PURPOSE,
    ) -> tuple[PatientOtpChallenge, PatientPortalAccount | None]:
        canonical = normalize_phone(phone)
        now = datetime.now(UTC)
        challenge = await self.repository.get_challenge(db, challenge_id)
        if (
            challenge is None
            or challenge.purpose != purpose
            or challenge.consumed_at is not None
            or challenge.expires_at <= now
            or (challenge.attempts or 0) >= (challenge.max_attempts or 0)
        ):
            raise ExpiredOtpCode("The code is invalid or has expired.")
        if challenge.phone_hash != self.policy.phone_hash(canonical):
            challenge.attempts = (challenge.attempts or 0) + 1
            if challenge.attempts >= (challenge.max_attempts or 0):
                challenge.consumed_at = now
            await db.commit()
            raise InvalidOtpCode(
                "The code is invalid or has expired.",
                attempts_remaining=max(
                    0, (challenge.max_attempts or 0) - (challenge.attempts or 0)
                ),
            )

        verifier = self.policy.code_verifier(
            purpose=challenge.purpose,
            phone_hash=challenge.phone_hash,
            challenge_id=challenge.id,
            code=code.strip(),
        )
        if not hmac.compare_digest(verifier, challenge.code_verifier):
            challenge.attempts = (challenge.attempts or 0) + 1
            if challenge.attempts >= (challenge.max_attempts or 0):
                challenge.consumed_at = now
            await db.commit()
            raise InvalidOtpCode(
                "The code is invalid or has expired.",
                attempts_remaining=max(
                    0, (challenge.max_attempts or 0) - (challenge.attempts or 0)
                ),
            )

        consumed = await self.repository.consume_challenge_atomic(db, challenge.id, now)
        if not consumed:
            await db.rollback()
            raise ExpiredOtpCode("The code is invalid or has expired.")
        await db.commit()

        account = (
            await db.get(PatientPortalAccount, challenge.patient_account_id)
            if challenge.patient_account_id
            else None
        )
        if account is not None and not account.is_active:
            account = None
        return challenge, account

    def _response(
        self,
        *,
        challenge_id: UUID,
        expires_at: datetime,
        resend_available_at: datetime,
        sandbox: bool = False,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "challenge_id": str(challenge_id),
            "expires_at": expires_at.isoformat(),
            "resend_available_at": resend_available_at.isoformat(),
            "code_length": self.policy.code_length,
            "numeric": True,
        }
        exposed = self.provider.exposed_code(challenge_id)
        if exposed is not None:
            data["dev_code"] = exposed
        if sandbox:
            data["sandbox"] = True
        return data


def build_patient_otp_service(settings: Settings) -> PatientOtpService:
    return PatientOtpService(
        OtpPolicy.from_settings(settings),
        provider=build_patient_otp_provider(settings),
    )
