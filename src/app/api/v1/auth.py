from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.schemas import Token
from ...core.security import oauth2_scheme
from ...domains.auth import issue_tokens, refresh_access_token, revoke_tokens

router = APIRouter(tags=["auth"])


@router.post("/login", response_model=Token)
async def login_for_access_token(
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    tokens = await issue_tokens(username_or_email=form_data.username, password=form_data.password, db=db)
    max_age = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

    response.set_cookie(
        key="refresh_token",
        value=tokens.refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=max_age,
    )

    return {"access_token": tokens.access_token, "token_type": tokens.token_type}


@router.post("/refresh", response_model=Token)
async def refresh_token(request: Request, db: AsyncSession = Depends(async_get_db)) -> dict[str, str]:
    tokens = await refresh_access_token(request.cookies.get("refresh_token"), db)
    return {"access_token": tokens.access_token, "token_type": tokens.token_type}


@router.post("/logout")
async def logout(
    response: Response,
    access_token: str = Depends(oauth2_scheme),
    refresh_token: str | None = Cookie(None, alias="refresh_token"),
    db: AsyncSession = Depends(async_get_db),
) -> dict[str, str]:
    await revoke_tokens(access_token=access_token, refresh_token=refresh_token, db=db)
    response.delete_cookie(key="refresh_token")
    return {"message": "Logged out successfully"}
