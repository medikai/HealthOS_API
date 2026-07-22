import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...crud.crud_auth_session import crud_auth_sessions
from ...crud.crud_identity import crud_user_accounts
from ...domains.auth.logto import logto_oidc_client
from ...models.identity import UserAccount

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", include_in_schema=False)
async def login(db: AsyncSession = Depends(async_get_db)) -> RedirectResponse:
    sign_in_url, transaction = await logto_oidc_client.create_login_transaction()
    await crud_auth_sessions.save_transaction(db, transaction)
    return RedirectResponse(sign_in_url, status_code=status.HTTP_302_FOUND)


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
    response = RedirectResponse(_required_post_login_redirect_uri(), status_code=status.HTTP_303_SEE_OTHER)
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


@router.get("/me")
async def me(request: Request, db: AsyncSession = Depends(async_get_db)) -> dict[str, Any]:
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
    response.delete_cookie(key=settings.AUTH_SESSION_COOKIE_NAME, path="/")
    return {
        "success": True,
        "data": {"logout_url": await logto_oidc_client.get_logout_url(session.id_token if session else None)},
        "meta": {},
    }


def _required_post_login_redirect_uri() -> str:
    if not settings.AUTH_POST_LOGIN_REDIRECT_URI:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AUTH_POST_LOGIN_REDIRECT_URI is not configured.")
    return settings.AUTH_POST_LOGIN_REDIRECT_URI
