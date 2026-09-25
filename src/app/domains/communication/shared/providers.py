"""Explicit provider ports for the communication domain.

Domains depend on these protocols, never on a concrete SDK. Adapters own all
network calls; fake adapters implement the same ports for tests.
"""

from typing import Protocol, runtime_checkable

from ....models.communication import DeliveryJob


@runtime_checkable
class DeliveryProvider(Protocol):
    """Port for one durable delivery channel (realtime | email | push)."""

    channel: str

    async def deliver(self, job: DeliveryJob) -> dict:
        """Deliver one claimed job and return redacted provider metadata."""
        ...


@runtime_checkable
class RealtimePublisher(Protocol):
    """Port for publishing minimal invalidation events to realtime transport."""

    async def publish(self, *, channel: str, event_type: str, payload: dict) -> dict:
        ...


@runtime_checkable
class RealtimeTokenProvider(Protocol):
    """Port for minting scoped, short-lived realtime client credentials."""

    configured: bool

    async def create_token_request(
        self, *, client_id: str, capabilities: dict[str, list[str]], ttl_seconds: int
    ) -> dict:
        ...
