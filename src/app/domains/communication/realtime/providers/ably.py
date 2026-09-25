"""Backend-only Ably adapter.

The official ``ably`` Python SDK signs scoped token requests locally from the
server API key, so issuing browser credentials needs no outbound network call
and the API key never reaches the client. The installed SDK (ably 2.x) exposes
no provider-side token revocation API, so server-controlled revocation is
implemented by bumping the stored channel generation (see ``RealtimeChannelState``).
"""

from datetime import timedelta
from typing import Any

from ...shared.errors import ProviderUnavailable

# Rest clients are cheap and safe to reuse across requests keyed by API key.
_CLIENTS: dict[str, Any] = {}


class AblyRealtimeProvider:
    def __init__(self, *, api_key: str | None, namespace: str, presence_enabled: bool) -> None:
        self._api_key = api_key or None
        self.namespace = namespace
        self.presence_enabled = presence_enabled

    def __repr__(self) -> str:  # never leak the key in logs/tracebacks
        return (
            f"AblyRealtimeProvider(configured={self.configured}, "
            f"namespace={self.namespace!r})"
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _client(self):
        if not self._api_key:
            raise ProviderUnavailable("ably_not_configured", "Ably is not configured.")
        client = _CLIENTS.get(self._api_key)
        if client is None:
            try:
                from ably import AblyRest
            except ImportError as exc:  # pragma: no cover - dependency declared
                raise ProviderUnavailable("ably_sdk_missing", "Ably SDK is not installed.") from exc
            try:
                client = AblyRest(self._api_key)
            except Exception as exc:  # invalid key material -> unavailable, never dummy
                raise ProviderUnavailable("ably_key_invalid", "Ably credentials are invalid.") from exc
            _CLIENTS[self._api_key] = client
        return client

    async def create_token_request(
        self, *, client_id: str, capabilities: dict[str, list[str]], ttl_seconds: int
    ) -> dict:
        client = self._client()
        try:
            request = await client.auth.create_token_request(
                {
                    "client_id": client_id,
                    "capability": capabilities,
                    "ttl": timedelta(seconds=int(ttl_seconds)),
                }
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable("ably_token_failed", "Ably token request failed.") from exc
        # A signed TokenRequest: key_name/timestamp/nonce/mac/capability/client_id/ttl.
        return request.to_dict()

    async def publish(self, *, channel: str, event_type: str, payload: dict) -> dict:
        client = self._client()
        try:
            await client.channels.get(channel).publish(event_type, payload)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable("ably_publish_failed", "Ably publish failed.") from exc
        return {"provider": "ably", "channel": channel, "event_type": event_type}


def build_ably_token_provider(settings: Any) -> AblyRealtimeProvider:
    api_key = settings.ABLY_API_KEY.get_secret_value() if settings.ABLY_API_KEY else None
    return AblyRealtimeProvider(
        api_key=api_key,
        namespace=settings.ABLY_CHANNEL_NAMESPACE,
        presence_enabled=settings.ABLY_PRESENCE_ENABLED,
    )
