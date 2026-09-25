"""Event envelope and payload-safety policy for durable delivery."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

EVENT_SCHEMA_VERSION = 1

# Substrings that mark credential/secret material which must never enter the
# generic outbox payload or a broker invalidation event.
FORBIDDEN_PAYLOAD_KEY_PARTS = (
    "otp",
    "password",
    "secret",
    "api_key",
    "authorization",
    "reset_url",
    "recovery_token",
    "challenge",
)


class UnsafePayloadError(ValueError):
    code = "UNSAFE_PAYLOAD"


def _json_value(value: Any) -> Any:
    if isinstance(value, (UUID, date, datetime)):
        return value.isoformat()
    return value


def assert_safe_payload(payload: dict[str, Any]) -> None:
    """Reject secret-bearing keys before a job is persisted or published."""
    for key in payload:
        lowered = str(key).lower()
        if any(part in lowered for part in FORBIDDEN_PAYLOAD_KEY_PARTS):
            raise UnsafePayloadError(
                f"Payload key '{key}' is not allowed in durable delivery payloads."
            )


def build_event_envelope(
    event_type: str,
    *,
    entity_id: UUID | str,
    entity_version: int | None,
    organization_id: UUID | str | None,
    facility_id: UUID | str | None = None,
    recipient_staff_id: UUID | str | None = None,
    occurred_at: datetime | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Minimal invalidation envelope; broker events never carry message bodies.

    This intentionally differs from ``core.events.make_event`` (SSE) because
    broker events carry only opaque IDs and safe replay metadata. The stable
    delivery ``event_id`` is separate from provider message IDs and cursors.
    """
    safe_data = dict(data or {})
    assert_safe_payload(safe_data)
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "schema_version": EVENT_SCHEMA_VERSION,
        "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
        "entity_id": str(entity_id) if entity_id is not None else None,
        "entity_version": entity_version,
        "organization_id": str(organization_id) if organization_id else None,
        "facility_id": str(facility_id) if facility_id else None,
        "recipient_staff_id": str(recipient_staff_id) if recipient_staff_id else None,
        "data": {k: _json_value(v) for k, v in safe_data.items()},
    }
