"""Cross-worker invalidation events for the SSE stream."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from .config import settings

logger = logging.getLogger(__name__)
CHANNEL = "healthos:invalidation"
_redis: Any | None = None


def configure() -> None:
    global _redis
    from redis.asyncio import Redis

    _redis = Redis(
        host=settings.REDIS_QUEUE_HOST,
        port=settings.REDIS_QUEUE_PORT,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=30,
    )


async def close() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def ping() -> None:
    if _redis is None:
        configure()
    assert _redis is not None
    await _redis.ping()


def _json_value(value: Any) -> Any:
    if isinstance(value, (UUID, date, datetime)):
        return value.isoformat()
    return value


def make_event(
    event_type: str,
    *,
    entity_id: UUID | str,
    entity_version: int | None,
    organization_id: UUID | str,
    facility_id: UUID | str,
    practitioner_id: UUID | str | None = None,
    old: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "entity_id": str(entity_id),
        "entity_version": entity_version,
        "organization_id": str(organization_id),
        "facility_id": str(facility_id),
        "practitioner_id": str(practitioner_id) if practitioner_id else None,
        "old": {k: _json_value(v) for k, v in (old or {}).items()},
        "new": {k: _json_value(v) for k, v in (new or {}).items()},
    }


async def publish(event: dict[str, Any]) -> None:
    """Publish after commit; a missed publish is repaired by client refetch."""
    try:
        if _redis is None:
            configure()
        assert _redis is not None
        await _redis.publish(CHANNEL, json.dumps(event, separators=(",", ":")))
    except Exception:
        # ponytail: no outbox added; the bounded commit-to-publish gap is repaired by reset/refetch.
        logger.exception("invalidation_event_publish_failed", extra={"event_type": event.get("event_type")})


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    if _redis is None:
        configure()
    assert _redis is not None
    pubsub = _redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=25)
            if message and message.get("type") == "message":
                try:
                    yield json.loads(message["data"])
                except (TypeError, json.JSONDecodeError):
                    continue
            else:
                yield {"_heartbeat": True}
            await asyncio.sleep(0)
    finally:
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
