import json
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import HTTPException, status

from .config import settings
from .utils import cache


@dataclass(frozen=True)
class LoginTransaction:
    state: str
    nonce: str
    code_verifier: str


class AuthSessionStore:
    """Stores opaque BFF sessions and one-time OIDC transactions in Redis."""

    _transaction_prefix = "healthos:auth:transaction:"
    _session_prefix = "healthos:auth:session:"

    async def save_transaction(self, transaction: LoginTransaction) -> None:
        await self._set_json(
            f"{self._transaction_prefix}{transaction.state}", asdict(transaction), expires_in=600
        )

    async def pop_transaction(self, state: str) -> LoginTransaction | None:
        value = await self._pop_json(f"{self._transaction_prefix}{state}")
        if value is None:
            return None
        return LoginTransaction(**value)

    async def create_session(self, values: dict[str, Any]) -> str:
        session_id = secrets.token_urlsafe(48)
        await self._set_json(
            f"{self._session_prefix}{session_id}", values, expires_in=settings.AUTH_SESSION_TTL_SECONDS
        )
        return session_id

    async def get_session(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        return await self._get_json(f"{self._session_prefix}{session_id}")

    async def delete_session(self, session_id: str | None) -> None:
        if session_id:
            await self._client().delete(f"{self._session_prefix}{session_id}")

    async def _set_json(self, key: str, value: dict[str, Any], expires_in: int) -> None:
        await self._client().set(key, json.dumps(value), ex=expires_in)

    async def _get_json(self, key: str) -> dict[str, Any] | None:
        raw_value = await self._client().get(key)
        if raw_value is None:
            return None
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8")
        return json.loads(raw_value)

    async def _pop_json(self, key: str) -> dict[str, Any] | None:
        raw_value = await self._client().getdel(key)
        if raw_value is None:
            return None
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8")
        return json.loads(raw_value)

    @staticmethod
    def _client():
        if cache.client is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication session storage is unavailable.",
            )
        return cache.client


auth_session_store = AuthSessionStore()
