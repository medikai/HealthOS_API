"""Patient realtime authorization service.

Derives the single account-level channel, clientId and capabilities from the
authenticated patient principal only. The patient may link to several
organizations, so organization identity travels in each event envelope rather
than in the channel name. Missing provider credentials raise
``ProviderUnavailable`` so callers fail closed instead of returning a dummy
successful token.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ....models.identity import PatientPortalAccount
from ..shared.errors import ProviderUnavailable
from ..shared.providers import RealtimeTokenProvider
from ..utils.channels import (
    build_patient_capabilities,
    build_patient_client_id,
    patient_user_channel,
    validate_namespace,
)
from .repository import PatientRealtimeChannelRepository


class PatientRealtimeService:
    def __init__(
        self,
        *,
        provider: RealtimeTokenProvider,
        repository: PatientRealtimeChannelRepository,
        namespace: str,
        ttl_seconds: int,
        renew_after_seconds: int,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.namespace = validate_namespace(namespace)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.renew_after_seconds = max(30, min(int(renew_after_seconds), self.ttl_seconds - 1))

    def feature_config(self) -> dict:
        available = bool(getattr(self.provider, "configured", False))
        return {
            "available": available,
            "provider": "ably",
            "reason": None if available else "ably_not_configured",
            "namespace": self.namespace,
            "token_type": "token_request",
            "token_ttl_seconds": self.ttl_seconds,
            "renew_after_seconds": self.renew_after_seconds,
            "browser_capabilities": ["subscribe"],
            "browser_publish": False,
        }

    async def issue_token(
        self, db: AsyncSession, account: PatientPortalAccount
    ) -> dict:
        if not getattr(self.provider, "configured", False):
            raise ProviderUnavailable(
                "ably_not_configured", "Realtime transport is not configured."
            )

        state = await self.repository.get_or_create_patient(
            db, patient_account_id=account.id
        )
        channel = patient_user_channel(self.namespace, account.id, state.generation)
        capabilities = build_patient_capabilities(channel)
        client_id = build_patient_client_id(account.id)

        token_request = await self.provider.create_token_request(
            client_id=client_id,
            capabilities=capabilities,
            ttl_seconds=self.ttl_seconds,
        )
        now = datetime.now(UTC)
        return {
            "available": True,
            "provider": "ably",
            "namespace": self.namespace,
            "client_id": client_id,
            "generation": state.generation,
            "channel": channel,
            "capabilities": capabilities,
            "token_request": token_request,
            "token_type": "token_request",
            "expires_at": (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            "renew_after_seconds": self.renew_after_seconds,
            "issued_at": now.isoformat(),
        }
