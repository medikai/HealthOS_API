from dataclasses import dataclass
from datetime import timedelta

from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.exceptions.http_exceptions import UnauthorizedException
from ...core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    TokenType,
    authenticate_user,
    blacklist_tokens,
    create_access_token,
    create_refresh_token,
    verify_token,
)


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


async def issue_tokens(username_or_email: str, password: str, db: AsyncSession) -> AuthTokens:
    user = await authenticate_user(username_or_email=username_or_email, password=password, db=db)
    if not user:
        raise UnauthorizedException("Wrong username, email or password.")

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = await create_access_token(data={"sub": user["username"]}, expires_delta=access_token_expires)
    refresh_token = await create_refresh_token(data={"sub": user["username"]})

    return AuthTokens(access_token=access_token, refresh_token=refresh_token)


async def refresh_access_token(refresh_token: str | None, db: AsyncSession) -> AuthTokens:
    if not refresh_token:
        raise UnauthorizedException("Refresh token missing.")

    user_data = await verify_token(refresh_token, TokenType.REFRESH, db)
    if not user_data:
        raise UnauthorizedException("Invalid refresh token.")

    access_token = await create_access_token(data={"sub": user_data.username_or_email})
    return AuthTokens(access_token=access_token, refresh_token=refresh_token)


async def revoke_tokens(access_token: str, refresh_token: str | None, db: AsyncSession) -> None:
    if not refresh_token:
        raise UnauthorizedException("Refresh token not found")

    try:
        await blacklist_tokens(access_token=access_token, refresh_token=refresh_token, db=db)
    except JWTError:
        raise UnauthorizedException("Invalid token.") from None
