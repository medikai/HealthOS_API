"""Patient portal auth orchestration: OTP → account → session (login/phone change).

Account creation happens on first successful phone verification. No clinical
``Patient``/``Person`` is ever created or mutated here; that is the exclusive
job of the staff-reviewed record-link workflow.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.identity import PatientPortalAccount
from ..communication.realtime.repository import PatientRealtimeChannelRepository
from .otp import (
    LOGIN_PURPOSE,
    PHONE_CHANGE_PURPOSE,
    PatientOtpService,
)
from .phone import normalize_phone
from .sessions import PatientSessionService


class PatientAuthError(RuntimeError):
    code = "PATIENT_AUTH_ERROR"


class PhoneInUse(PatientAuthError):
    code = "PHONE_IN_USE"


class PhoneUnchanged(PatientAuthError):
    code = "PHONE_UNCHANGED"


class PatientAuthService:
    def __init__(
        self,
        *,
        otp_service: PatientOtpService,
        session_service: PatientSessionService,
        session_ttl_seconds: int,
    ) -> None:
        self.otp_service = otp_service
        self.session_service = session_service
        self.session_ttl_seconds = session_ttl_seconds

    async def request_login_otp(
        self,
        db: AsyncSession,
        *,
        phone: str,
        client_ip: str | None,
        sandbox: bool = False,
    ) -> dict:
        return await self.otp_service.request_code(
            db, phone=phone, client_ip=client_ip, purpose=LOGIN_PURPOSE, sandbox=sandbox
        )

    async def resend_login_otp(
        self,
        db: AsyncSession,
        *,
        challenge_id,
        client_ip: str | None,
        phone: str | None = None,
        sandbox: bool = False,
    ) -> dict:
        return await self.otp_service.resend_code(
            db, challenge_id=challenge_id, client_ip=client_ip, phone=phone, sandbox=sandbox
        )

    async def verify_login(
        self,
        db: AsyncSession,
        *,
        phone: str,
        challenge_id,
        code: str,
        rotate_token: str | None,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> tuple[PatientPortalAccount, str]:
        canonical = normalize_phone(phone)
        _, account = await self.otp_service.verify_code(
            db,
            challenge_id=challenge_id,
            phone=canonical,
            code=code,
            purpose=LOGIN_PURPOSE,
        )
        if account is None:
            account = PatientPortalAccount(phone=canonical, is_active=True)
            db.add(account)
            try:
                await db.flush()
            except IntegrityError:
                # Concurrent first logins for the same phone: keep exactly one
                # account and continue with the winner.
                await db.rollback()
                account = await db.scalar(
                    select(PatientPortalAccount).where(
                        PatientPortalAccount.phone == canonical
                    )
                )
                if account is None or not account.is_active:
                    from .otp import InvalidOtpCode

                    raise InvalidOtpCode("The code is invalid or has expired.")
        token, _ = await self.session_service.create_session(
            db,
            account=account,
            ttl_seconds=self.session_ttl_seconds,
            rotate_token=rotate_token,
            user_agent=user_agent,
            client_ip=client_ip,
        )
        await db.commit()
        await db.refresh(account)
        return account, token

    async def request_phone_change(
        self,
        db: AsyncSession,
        *,
        account: PatientPortalAccount,
        new_phone: str,
        client_ip: str | None,
    ) -> dict:
        canonical = normalize_phone(new_phone)
        if canonical == account.phone:
            raise PhoneUnchanged("The new phone number matches the current one.")
        other = await db.scalar(
            select(PatientPortalAccount).where(
                PatientPortalAccount.phone == canonical,
                PatientPortalAccount.is_active.is_(True),
            )
        )
        if other is not None and other.id != account.id:
            raise PhoneInUse("This phone number is already in use.")
        return await self.otp_service.request_code(
            db,
            phone=canonical,
            client_ip=client_ip,
            purpose=PHONE_CHANGE_PURPOSE,
            bound_account=account,
        )

    async def verify_phone_change(
        self,
        db: AsyncSession,
        *,
        account: PatientPortalAccount,
        new_phone: str,
        challenge_id,
        code: str,
        current_token: str | None,
        user_agent: str | None = None,
        client_ip: str | None = None,
    ) -> tuple[PatientPortalAccount, str]:
        canonical = normalize_phone(new_phone)
        _, bound = await self.otp_service.verify_code(
            db,
            challenge_id=challenge_id,
            phone=canonical,
            code=code,
            purpose=PHONE_CHANGE_PURPOSE,
        )
        if bound is None or bound.id != account.id:
            from .otp import InvalidOtpCode

            raise InvalidOtpCode("The code is invalid or has expired.")
        other = await db.scalar(
            select(PatientPortalAccount).where(
                PatientPortalAccount.phone == canonical,
                PatientPortalAccount.id != account.id,
                PatientPortalAccount.is_active.is_(True),
            )
        )
        if other is not None:
            raise PhoneInUse("This phone number is already in use.")
        now = datetime.now(UTC)
        account.phone = canonical
        account.updated_at = now
        await self.session_service.revoke_all_for_account(
            db, account.id, reason="phone_changed"
        )
        # Phone change must also revoke previously issued realtime channels,
        # atomically with the session revocation.
        await PatientRealtimeChannelRepository().bump_patient_generation(
            db, patient_account_id=account.id
        )
        token, _ = await self.session_service.create_session(
            db,
            account=account,
            ttl_seconds=self.session_ttl_seconds,
            rotate_token=None,
            user_agent=user_agent,
            client_ip=client_ip,
        )
        await db.commit()
        await db.refresh(account)
        return account, token

    @staticmethod
    def profile_payload(account: PatientPortalAccount) -> dict:
        from .phone import mask_phone

        complete = bool(
            account.first_name and account.date_of_birth
        )
        return {
            "uuid": str(account.id),
            "phone_masked": mask_phone(account.phone),
            "first_name": account.first_name,
            "last_name": account.last_name,
            "date_of_birth": account.date_of_birth,
            "email": account.email,
            "profile_complete": complete,
            "created_at": account.created_at.isoformat() if account.created_at else None,
        }
