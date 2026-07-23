import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwt

from ...core.auth_session import LoginTransaction
from ...core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OidcConfiguration:
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    jwks_uri: str
    issuer: str
    id_token_signing_algorithms: list[str]
    end_session_endpoint: str | None


class LogtoOidcClient:
    _configuration: OidcConfiguration | None = None
    _management_token: str | None = None
    _management_token_expires_at: float = 0

    async def create_management_organization(self, name: str) -> str:
        """Create an organization in Logto through a configured M2M application."""
        base_url = self._required("LOGTO_MANAGEMENT_API_BASE_URL", settings.LOGTO_MANAGEMENT_API_BASE_URL).rstrip("/")
        token = await self._get_management_token()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/organizations",
                json={"name": name},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if response.is_error:
            logger.warning("Logto organization creation failed with status %s", response.status_code)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to create Logto organization.")
        organization_id = response.json().get("id")
        if not isinstance(organization_id, str) or not organization_id:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Logto returned an invalid organization.")
        return organization_id

    async def add_management_organization_member(self, organization_id: str, user_id: str) -> None:
        token = await self._get_management_token()
        base_url = self._required("LOGTO_MANAGEMENT_API_BASE_URL", settings.LOGTO_MANAGEMENT_API_BASE_URL).rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/organizations/{organization_id}/users",
                json={"userIds": [user_id]},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if response.is_error:
            logger.warning("Logto organization member creation failed with status %s", response.status_code)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to add user to Logto organization.")

    async def assign_management_organization_role(self, organization_id: str, user_id: str, role_id: str) -> None:
        token = await self._get_management_token()
        base_url = self._required("LOGTO_MANAGEMENT_API_BASE_URL", settings.LOGTO_MANAGEMENT_API_BASE_URL).rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{base_url}/organizations/{organization_id}/users/roles",
                json={"userIds": [user_id], "organizationRoleIds": [role_id]},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if response.is_error:
            logger.warning("Logto organization role assignment failed with status %s", response.status_code)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to assign Logto organization role.")

    async def _get_management_token(self) -> str:
        if self._management_token and time.time() < self._management_token_expires_at:
            return self._management_token
        token_endpoint = self._required("LOGTO_MANAGEMENT_TOKEN_ENDPOINT", settings.LOGTO_MANAGEMENT_TOKEN_ENDPOINT)
        app_id = self._required("LOGTO_MANAGEMENT_APP_ID", settings.LOGTO_MANAGEMENT_APP_ID)
        app_secret = self._required_secret_value("LOGTO_MANAGEMENT_APP_SECRET", settings.LOGTO_MANAGEMENT_APP_SECRET)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                token_endpoint,
                data={"grant_type": "client_credentials", "resource": settings.LOGTO_MANAGEMENT_API_BASE_URL, "scope": settings.LOGTO_MANAGEMENT_SCOPE},
                auth=(app_id, app_secret),
                headers={"Accept": "application/json"},
            )
        if response.is_error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to authorize with Logto Management API.")
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Logto did not return a management token.")
        self._management_token = token
        self._management_token_expires_at = time.time() + max(int(payload.get("expires_in", 3600)) - 60, 60)
        return token

    async def create_login_transaction(self) -> tuple[str, LoginTransaction]:
        configuration = await self._get_configuration()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        transaction = LoginTransaction(state=state, nonce=nonce, code_verifier=code_verifier)
        parameters = {
            "client_id": self._required("LOGTO_APP_ID", settings.LOGTO_APP_ID),
            "redirect_uri": self._required("LOGTO_REDIRECT_URI", settings.LOGTO_REDIRECT_URI),
            "response_type": "code",
            "scope": " ".join(settings.LOGTO_SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{configuration.authorization_endpoint}?{urlencode(parameters)}", transaction

    async def complete_login(self, code: str, transaction: LoginTransaction) -> tuple[dict[str, Any], dict[str, Any]]:
        configuration = await self._get_configuration()
        token_response = await self._exchange_code(configuration, code, transaction.code_verifier)
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str):
            raise self._unauthorized("Logto did not return an ID token.")

        access_token = token_response.get("access_token")
        if not isinstance(access_token, str):
            raise self._unauthorized("Logto did not return an access token.")

        claims = await self._validate_id_token(
            configuration, id_token, transaction.nonce, access_token
        )
        userinfo = await self._get_userinfo(configuration, access_token)
        if userinfo.get("sub") != claims.get("sub"):
            raise self._unauthorized("Logto user information did not match the ID token.")
        return claims | userinfo, token_response

    async def get_logout_url(self, id_token: str | None) -> str:
        configuration = await self._get_configuration()
        if not configuration.end_session_endpoint:
            return self._required("LOGTO_POST_LOGOUT_REDIRECT_URI", settings.LOGTO_POST_LOGOUT_REDIRECT_URI)
        parameters: dict[str, str] = {
            "post_logout_redirect_uri": self._required(
                "LOGTO_POST_LOGOUT_REDIRECT_URI", settings.LOGTO_POST_LOGOUT_REDIRECT_URI
            )
        }
        if id_token:
            parameters["id_token_hint"] = id_token
        return f"{configuration.end_session_endpoint}?{urlencode(parameters)}"

    async def _get_configuration(self) -> OidcConfiguration:
        self._ensure_enabled()
        if self._configuration is not None:
            return self._configuration
        endpoint = self._required("LOGTO_ENDPOINT", settings.LOGTO_ENDPOINT).rstrip("/")
        discovery_url = (
            f"{endpoint}/.well-known/openid-configuration"
            if endpoint.endswith("/oidc")
            else f"{endpoint}/oidc/.well-known/openid-configuration"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(discovery_url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to retrieve Logto's OpenID Connect configuration.",
            ) from exc
        payload = response.json()
        try:
            self._configuration = OidcConfiguration(
                authorization_endpoint=payload["authorization_endpoint"],
                token_endpoint=payload["token_endpoint"],
                userinfo_endpoint=payload["userinfo_endpoint"],
                jwks_uri=payload["jwks_uri"],
                issuer=payload["issuer"],
                id_token_signing_algorithms=payload["id_token_signing_alg_values_supported"],
                end_session_endpoint=payload.get("end_session_endpoint"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=503, detail="Logto discovery document is incomplete.") from exc
        return self._configuration

    async def _exchange_code(self, configuration: OidcConfiguration, code: str, code_verifier: str) -> dict[str, Any]:
        app_id = self._required("LOGTO_APP_ID", settings.LOGTO_APP_ID)
        app_secret = self._required_secret("LOGTO_APP_SECRET")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._required("LOGTO_REDIRECT_URI", settings.LOGTO_REDIRECT_URI),
            "code_verifier": code_verifier,
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                configuration.token_endpoint,
                data=form,
                auth=(app_id, app_secret),
                headers={"Accept": "application/json"},
            )
        if response.is_error:
            raise self._unauthorized("Unable to complete Logto sign-in.")
        return response.json()

    async def _validate_id_token(
        self,
        configuration: OidcConfiguration,
        id_token: str,
        expected_nonce: str,
        access_token: str,
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
            algorithm = header.get("alg")
            if not isinstance(algorithm, str) or algorithm not in configuration.id_token_signing_algorithms:
                raise JWTError("Unsupported signing algorithm")
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(configuration.jwks_uri)
                response.raise_for_status()
            keys = response.json().get("keys", [])
            key = next((candidate for candidate in keys if candidate.get("kid") == header.get("kid")), None)
            if key is None:
                raise JWTError("Signing key not found")
            claims = jwt.decode(
                id_token,
                key,
                algorithms=[algorithm],
                audience=self._required("LOGTO_APP_ID", settings.LOGTO_APP_ID),
                issuer=configuration.issuer,
                # Verify at_hash when Logto includes it in a hybrid-compatible ID token.
                access_token=access_token,
            )
        except (httpx.HTTPError, JWTError, ValueError) as exc:
            # Deliberately log only the validation reason; never log the token or claims.
            logger.warning("Logto ID token validation failed: %s", exc)
            raise self._unauthorized("Invalid Logto ID token.") from None
        if claims.get("nonce") != expected_nonce:
            raise self._unauthorized("Invalid Logto sign-in nonce.")
        return claims

    async def _get_userinfo(self, configuration: OidcConfiguration, access_token: Any) -> dict[str, Any]:
        if not isinstance(access_token, str):
            raise self._unauthorized("Logto did not return an access token.")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                configuration.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.is_error:
            raise self._unauthorized("Unable to obtain Logto user information.")
        return response.json()

    @staticmethod
    def _unauthorized(message: str) -> HTTPException:
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message)

    @staticmethod
    def _required(name: str, value: str | None) -> str:
        if not value:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{name} is not configured.")
        return value

    @staticmethod
    def _required_secret(name: str) -> str:
        if settings.LOGTO_APP_SECRET is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{name} is not configured.")
        return settings.LOGTO_APP_SECRET.get_secret_value()

    @staticmethod
    def _required_secret_value(name: str, value: Any) -> str:
        if value is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{name} is not configured.")
        return value.get_secret_value()

    @staticmethod
    def _ensure_enabled() -> None:
        if not settings.LOGTO_ENABLED:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Logto authentication is disabled.")


logto_oidc_client = LogtoOidcClient()
