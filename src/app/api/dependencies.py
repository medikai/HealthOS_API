import asyncio
from time import perf_counter
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db.database import async_get_db
from ..core.exceptions.http_exceptions import ForbiddenException, UnauthorizedException
from ..core.logger import logging
from ..core.request_metrics import current_request_metrics
from ..core.security import TokenType, oauth2_scheme, verify_token
from ..crud.crud_auth_session import crud_auth_sessions
from ..crud.crud_users import crud_users
from ..models.identity import UserAccount

logger = logging.getLogger(__name__)
_local_demo_lock = asyncio.Lock()


async def _get_or_create_local_demo_account(db: AsyncSession) -> UserAccount:
    """Provision the local bypass principal once, including its admin scope."""
    from sqlalchemy import select

    from ..models.organization import (
        Facility,
        Organization,
        StaffAssignment,
        StaffMember,
    )

    async with _local_demo_lock:
        demo_account = await db.scalar(
            select(UserAccount).where(UserAccount.logto_user_id == "local-demo-user")
        )
        if demo_account is None:
            demo_account = UserAccount(
                logto_user_id="local-demo-user",
                email="demo@healthos.local",
                display_name="Local Demo User",
            )
            db.add(demo_account)
            await db.flush()

        organization = await db.scalar(
            select(Organization).where(Organization.code == "LOCAL-DEMO")
        )
        if organization is None:
            organization = Organization(
                name="HealthOS Local Demo", code="LOCAL-DEMO", is_active=True
            )
            db.add(organization)
            await db.flush()

        facility = await db.scalar(
            select(Facility)
            .where(Facility.organization_id == organization.id)
            .order_by(Facility.created_at)
        )
        if facility is None:
            facility = Facility(
                organization_id=organization.id,
                name="Main Hospital",
                code="MAIN",
                is_active=True,
            )
            db.add(facility)
            await db.flush()

        staff = await db.scalar(
            select(StaffMember).where(
                StaffMember.organization_id == organization.id,
                StaffMember.user_account_id == demo_account.id,
            )
        )
        if staff is None:
            staff = StaffMember(
                organization_id=organization.id,
                user_account_id=demo_account.id,
                is_active=True,
            )
            db.add(staff)
            await db.flush()

        assignment = await db.scalar(
            select(StaffAssignment).where(
                StaffAssignment.staff_member_id == staff.id,
                StaffAssignment.role_code == "organization_admin",
            )
        )
        if assignment is None:
            db.add(
                StaffAssignment(
                    staff_member_id=staff.id,
                    facility_id=facility.id,
                    role_code="organization_admin",
                    is_active=True,
                )
            )

        await db.commit()
        await db.refresh(demo_account)
        return demo_account


async def get_current_identity_account(
    request: Request, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> UserAccount:
    """Resolve the BFF session or local JWT to its HealthOS-owned user account mapping."""
    metrics = current_request_metrics()
    auth_started_at = perf_counter()
    try:
        if metrics is not None:
            acquire_started_at = perf_counter()
            await db.connection()
            metrics.db_acquire_ms = round(
                (perf_counter() - acquire_started_at) * 1000, 2
            )

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            try:
                import uuid as uuid_pkg

                from jose import jwt

                from ..core.security import ALGORITHM, SECRET_KEY

                payload = jwt.decode(
                    token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM]
                )
                sub = payload.get("sub")
                if sub:
                    account = await db.get(UserAccount, uuid_pkg.UUID(sub))
                    if account and account.is_active:
                        return account
            except Exception:
                raise UnauthorizedException("Invalid or expired token.")

        # Explicit emergency/demo bypass. Keep this false on any public deployment.
        if not settings.LOGTO_ENABLED and settings.AUTH_LOCAL_DEV_BYPASS:
            return await _get_or_create_local_demo_account(db)
        session = await crud_auth_sessions.get_session(
            db, request.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
        )
        if session is None:
            raise UnauthorizedException("Authentication required.")
        from sqlalchemy import select

        account = await db.scalar(
            select(UserAccount).where(UserAccount.id == session.user_account_id)
        )
        if account is None or not account.is_active:
            raise UnauthorizedException("Authenticated account is unavailable.")
        return account
    finally:
        if metrics is not None:
            metrics.auth_ms = round((perf_counter() - auth_started_at) * 1000, 2)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    token_data = await verify_token(token, TokenType.ACCESS, db)
    if token_data is None:
        raise UnauthorizedException("User not authenticated.")

    if "@" in token_data.username_or_email:
        user = await crud_users.get(
            db=db, email=token_data.username_or_email, is_deleted=False
        )
    else:
        user = await crud_users.get(
            db=db, username=token_data.username_or_email, is_deleted=False
        )

    if user:
        return user

    raise UnauthorizedException("User not authenticated.")


async def get_optional_user(
    request: Request, db: AsyncSession = Depends(async_get_db)
) -> dict | None:
    token = request.headers.get("Authorization")
    if not token:
        return None

    try:
        token_type, _, token_value = token.partition(" ")
        if token_type.lower() != "bearer" or not token_value:
            return None

        token_data = await verify_token(token_value, TokenType.ACCESS, db)
        if token_data is None:
            return None

        return await get_current_user(token_value, db=db)

    except HTTPException as http_exc:
        if http_exc.status_code != 401:
            logger.error(
                f"Unexpected HTTPException in get_optional_user: {http_exc.detail}"
            )
        return None

    except Exception as exc:
        logger.error(f"Unexpected error in get_optional_user: {exc}")
        return None


async def get_current_superuser(
    current_user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    if not current_user["is_superuser"]:
        raise ForbiddenException("You do not have enough privileges.")

    return current_user
