"""Explicit fake providers for isolated delivery tests.

These record calls and can inject bounded failures. They never activate as a
production fallback; callers wire them only in tests.
"""

from .....models.communication import (
    JOB_CHANNEL_EMAIL,
    JOB_CHANNEL_PUSH,
    JOB_CHANNEL_REALTIME,
    DeliveryJob,
)
from ...shared.errors import ProviderUnavailable


class FakeDeliveryProvider:
    def __init__(self, channel: str, *, fail_first: int = 0) -> None:
        self.channel = channel
        self.fail_remaining = max(0, fail_first)
        self.delivered: list[dict] = []
        self.attempts: int = 0

    async def deliver(self, job: DeliveryJob) -> dict:
        self.attempts += 1
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise ProviderUnavailable("fake_failure", "Injected fake provider failure.")
        self.delivered.append(
            {"dedup_key": job.dedup_key, "event_type": job.event_type, "payload": job.payload}
        )
        return {"provider": "fake", "channel": self.channel, "accepted": True}


class FakeRealtimePublisher(FakeDeliveryProvider):
    def __init__(self, *, fail_first: int = 0) -> None:
        super().__init__(JOB_CHANNEL_REALTIME, fail_first=fail_first)
        self.published: list[dict] = []

    async def publish(self, *, channel: str, event_type: str, payload: dict) -> dict:
        self.published.append(
            {"channel": channel, "event_type": event_type, "payload": payload}
        )
        return {"provider": "fake-realtime", "channel": channel}


class FakeEmailProvider(FakeDeliveryProvider):
    def __init__(self, *, fail_first: int = 0) -> None:
        super().__init__(JOB_CHANNEL_EMAIL, fail_first=fail_first)


class FakePushProvider(FakeDeliveryProvider):
    def __init__(self, *, fail_first: int = 0) -> None:
        super().__init__(JOB_CHANNEL_PUSH, fail_first=fail_first)
