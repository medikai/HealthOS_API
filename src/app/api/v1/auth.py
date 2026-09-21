import secrets
import uuid as uuid_pkg
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.security import (
    ALGORITHM,
    SECRET_KEY,
    create_access_token,
    get_password_hash,
    verify_password,
)
from ...crud.crud_auth_session import crud_auth_sessions
from ...crud.crud_identity import crud_user_accounts
from ...domains.auth.logto import logto_oidc_client
from ...models.identity import UserAccount
from ...models.masters import MedicalCouncil, Specialty
from ...models.organization import StaffMember
from ...schemas.local_auth import LocalLoginPayload, LocalRegisterPayload

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", include_in_schema=False)
async def login(db: AsyncSession = Depends(async_get_db)) -> RedirectResponse:
    sign_in_url, transaction = await logto_oidc_client.create_login_transaction()
    await crud_auth_sessions.save_transaction(db, transaction)
    return RedirectResponse(sign_in_url, status_code=status.HTTP_302_FOUND)


@router.get("/register", include_in_schema=False)
async def register(db: AsyncSession = Depends(async_get_db)) -> RedirectResponse:
    """Start Logto-hosted registration; credentials never enter HealthOS."""
    sign_up_url, transaction = await logto_oidc_client.create_login_transaction(first_screen="identifier:register")
    await crud_auth_sessions.save_transaction(db, transaction)
    return RedirectResponse(sign_up_url, status_code=status.HTTP_302_FOUND)


@router.get("/callback", include_in_schema=False)
async def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: AsyncSession = Depends(async_get_db),
) -> RedirectResponse:
    if error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=error_description or error)
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Logto callback parameters.")

    transaction = await crud_auth_sessions.pop_transaction(db, state)
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired Logto sign-in state.")

    claims, tokens = await logto_oidc_client.complete_login(code, transaction)
    account = await crud_user_accounts.upsert_from_logto(db, claims)
    csrf_token = secrets.token_urlsafe(32)
    session_id = await crud_auth_sessions.create_session(
        db,
        {
            "user_account_id": account.id,
            "id_token": tokens["id_token"],
            "csrf_token": csrf_token,
        }
    )
    has_membership = await db.scalar(select(StaffMember.id).where(StaffMember.user_account_id == account.id, StaffMember.is_active.is_(True)))
    redirect_uri = _required_post_registration_redirect_uri() if not has_membership and settings.AUTH_POST_REGISTRATION_REDIRECT_URI else _required_post_login_redirect_uri()
    response = RedirectResponse(redirect_uri, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.AUTH_SESSION_COOKIE_NAME,
        value=session_id,
        max_age=settings.AUTH_SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        path="/",
    )
    return response


@router.post("/local/register")
async def local_register(
    payload: LocalRegisterPayload,
    db: AsyncSession = Depends(async_get_db),
) -> dict[str, Any]:
    if settings.LOGTO_ENABLED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Local auth is disabled when Logto is enabled.")

    existing = await db.scalar(select(UserAccount).where(UserAccount.email == payload.email))
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already registered.")

    specialty = None
    if payload.specialty_id:
        specialty = await db.get(Specialty, payload.specialty_id)
        if specialty is None or not specialty.is_active:
            raise HTTPException(status_code=422, detail="Clinical specialty does not exist or is inactive.")
    elif payload.specialty:
        clean_code = payload.specialty.strip().lower().replace("&", "and").replace("/", " ").replace(" ", "_")
        specialty = await db.scalar(select(Specialty).where(or_(
            Specialty.code == clean_code,
            func.lower(Specialty.name) == payload.specialty.strip().lower(),
        )))

    if payload.medical_council_id:
        council = await db.get(MedicalCouncil, payload.medical_council_id)
        if council is None or not council.is_active:
            raise HTTPException(status_code=422, detail="Medical council does not exist or is inactive.")

    account = UserAccount(
        logto_user_id=f"local:{payload.email}",
        email=payload.email,
        display_name=payload.name,
        password_hash=get_password_hash(payload.password),
        registration_specialty=payload.specialty.strip() if payload.specialty else (specialty.name if specialty else None),
        registration_specialty_id=specialty.id if specialty else None,
        registration_medical_council_id=payload.medical_council_id,
        registration_medical_council_reg_no=payload.medical_council_reg_no.strip() if payload.medical_council_reg_no else None,
        is_active=True,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    access_token = await create_access_token({"sub": str(account.id), "email": account.email, "name": account.display_name})
    return {
        "success": True,
        "data": {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "uuid": str(account.id),
                "email": account.email,
                "display_name": account.display_name,
            },
        },
        "meta": {},
    }


@router.post("/local/login")
async def local_login(
    payload: LocalLoginPayload,
    db: AsyncSession = Depends(async_get_db),
) -> dict[str, Any]:
    if settings.LOGTO_ENABLED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Local auth is disabled when Logto is enabled.")

    account = await db.scalar(select(UserAccount).where(UserAccount.email == payload.email))
    if not account or not account.password_hash or not await verify_password(payload.password, account.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password.")

    if not account.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated.")

    access_token = await create_access_token({"sub": str(account.id), "email": account.email, "name": account.display_name})
    return {
        "success": True,
        "data": {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "uuid": str(account.id),
                "email": account.email,
                "display_name": account.display_name,
            },
        },
        "meta": {},
    }


@router.get("/me")
async def me(request: Request, db: AsyncSession = Depends(async_get_db)) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        try:
            payload = jwt.decode(token, SECRET_KEY.get_secret_value(), algorithms=[ALGORITHM])
            sub = payload.get("sub")
            if not sub:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
            account = await db.get(UserAccount, uuid_pkg.UUID(sub))
            if account is None or not account.is_active:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
            return {
                "success": True,
                "data": {"user_account_id": str(account.id), "logto_user_id": account.logto_user_id, "email": account.email, "display_name": account.display_name},
                "meta": {"csrf_token": "bearer-local-token"},
            }
        except (JWTError, ValueError):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token.")

    session = await crud_auth_sessions.get_session(db, request.cookies.get(settings.AUTH_SESSION_COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    account = await db.get(UserAccount, session.user_account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return {
        "success": True,
        "data": {"user_account_id": str(session.user_account_id), "logto_user_id": account.logto_user_id},
        "meta": {"csrf_token": session.csrf_token},
    }


@router.post("/logout")
async def logout(
    response: Response,
    session_cookie: Annotated[str | None, Cookie(alias=settings.AUTH_SESSION_COOKIE_NAME)] = None,
    db: AsyncSession = Depends(async_get_db),
) -> dict[str, Any]:
    session = await crud_auth_sessions.get_session(db, session_cookie)
    await crud_auth_sessions.delete_session(db, session_cookie)
    response.delete_cookie(
        key=settings.AUTH_SESSION_COOKIE_NAME,
        path="/",
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
    )
    return {
        "success": True,
        "data": {"logout_url": await logto_oidc_client.get_logout_url(session.id_token if session else None)},
        "meta": {},
    }


def _required_post_login_redirect_uri() -> str:
    if not settings.AUTH_POST_LOGIN_REDIRECT_URI:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AUTH_POST_LOGIN_REDIRECT_URI is not configured.")
    return settings.AUTH_POST_LOGIN_REDIRECT_URI


def _required_post_registration_redirect_uri() -> str:
    if not settings.AUTH_POST_REGISTRATION_REDIRECT_URI:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AUTH_POST_REGISTRATION_REDIRECT_URI is not configured.")
    return settings.AUTH_POST_REGISTRATION_REDIRECT_URI
