"""Realtime authorization service.

Derives exact channels, clientId and capabilities from trusted membership and
the stored channel generation. Never accepts a tenant/staff/channel from the
client. Missing provider credentials raise ``ProviderUnavailable`` so callers
fail closed instead of returning a dummy successful token.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies import CommunicationStaffContext
from ..shared.errors import ProviderUnavailable
from ..shared.providers import RealtimeTokenProvider
from ..utils.channels import (
    build_capabilities,
    build_client_id,
    facility_presence_channel,
    own_user_channel,
    validate_namespace,
)
from .repository import RealtimeChannelRepository


class RealtimeService:
    def __init__(
        self,
        *,
        provider: RealtimeTokenProvider,
        repository: RealtimeChannelRepository,
        namespace: str,
        ttl_seconds: int,
        renew_after_seconds: int,
        presence_enabled: bool,
        max_presence_channels: int,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.namespace = validate_namespace(namespace)
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.renew_after_seconds = max(30, min(int(renew_after_seconds), self.ttl_seconds - 1))
        self.presence_enabled = bool(presence_enabled)
        self.max_presence_channels = max(0, int(max_presence_channels))

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
            "presence_enabled": self.presence_enabled,
            "browser_capabilities": ["subscribe"] + (["presence"] if self.presence_enabled else []),
            "browser_publish": False,
        }

    async def issue_token(
        self, db: AsyncSession, context: CommunicationStaffContext
    ) -> dict:
        if not getattr(self.provider, "configured", False):
            raise ProviderUnavailable(
                "ably_not_configured", "Realtime transport is not configured."
            )

        state = await self.repository.get_or_create(
            db,
            organization_id=context.organization_id,
            staff_member_id=context.staff_member_id,
        )
        own_channel = own_user_channel(
            self.namespace,
            context.organization_id,
            context.staff_member_id,
            state.generation,
        )
        presence_channels: list[str] = []
        if self.presence_enabled:
            presence_channels = [
                facility_presence_channel(self.namespace, context.organization_id, facility_id)
                for facility_id in context.facility_ids[: self.max_presence_channels]
            ]
        capabilities = build_capabilities(own_channel, presence_channels)
        client_id = build_client_id(context.organization_id, context.staff_member_id)

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
            "channels": {"own": own_channel, "presence": presence_channels},
            "capabilities": capabilities,
            "token_request": token_request,
            "token_type": "token_request",
            "expires_at": (now + timedelta(seconds=self.ttl_seconds)).isoformat(),
            "renew_after_seconds": self.renew_after_seconds,
            "issued_at": now.isoformat(),
        }

    async def revoke_staff_channels(
        self, db: AsyncSession, *, organization_id: UUID, staff_member_id: UUID
    ) -> int:
        """Server-internal revocation hook for membership/session changes.

        Returns the new generation. Callers stop publishing to old generations.
        """
        state = await self.repository.bump_generation(
            db, organization_id=organization_id, staff_member_id=staff_member_id
        )
        return state.generation
