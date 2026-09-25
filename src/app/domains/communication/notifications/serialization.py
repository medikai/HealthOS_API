"""Pure serialization/cursor helpers for notifications (no DB, no network)."""

import base64
import binascii
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .constants import CATEGORY_CATALOG, KIND_TASK

TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def encode_cursor(updated_at: datetime, entity_id: UUID | str) -> str:
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    raw = f"{updated_at.isoformat()}|{entity_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        timestamp, _, entity_id = raw.partition("|")
        parsed = datetime.fromisoformat(timestamp)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if not entity_id:
        return None
    return parsed, entity_id


def is_needs_action(event: Any) -> bool:
    return (
        event.kind == KIND_TASK
        and event.task_state == "awaiting"
        and event.superseded_by_id is None
    )


def notification_item(
    event: Any,
    recipient: Any,
    *,
    facility_name: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "category": event.category,
        "priority": event.priority,
        "kind": event.kind,
        "title": event.title,
        "context": event.context,
        "actor": (
            {"name": event.actor_name, "role": event.actor_role}
            if (event.actor_name or event.actor_role)
            else None
        ),
        "facility": (
            {"id": str(event.facility_id), "name": facility_name}
            if event.facility_id
            else None
        ),
        "location": event.location,
        "createdAt": event.occurred_at.isoformat() if event.occurred_at else None,
        "updatedAt": event.updated_at.isoformat() if event.updated_at else None,
        "read": recipient.read_at is not None,
        "taskState": event.task_state,
        "taskRef": (
            {"type": event.task_type, "id": event.task_id}
            if event.task_id
            else None
        ),
        "action": (
            {"kind": event.action_kind, "label": event.action_label}
            if event.action_kind
            else None
        ),
        "dueAt": event.expires_at.isoformat() if event.expires_at else None,
        "supersededById": (
            str(event.superseded_by_id) if event.superseded_by_id else None
        ),
    }


def preference_defaults(category: str) -> dict[str, Any]:
    meta = CATEGORY_CATALOG.get(category, {})
    return {
        "category": category,
        "label": meta.get("label", category.replace("_", " ").title()),
        "inApp": True,
        "browser": bool(meta.get("browser_default", True)),
        "sound": False,
        "locked": bool(meta.get("locked", False)),
    }
