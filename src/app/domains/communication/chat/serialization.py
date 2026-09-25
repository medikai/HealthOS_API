"""Pure chat serialization helpers (no DB, no network)."""

from typing import Any
from uuid import UUID

PREVIEW_LENGTH = 120


def _preview(body: str | None) -> str | None:
    if not body:
        return None
    collapsed = " ".join(body.split())
    return collapsed[:PREVIEW_LENGTH]


def conversation_item(
    conversation: Any,
    membership: Any,
    *,
    member_ids: list[UUID],
    counterpart_id: UUID | None = None,
    facility_name: str | None = None,
    last_body: str | None = None,
    last_at: Any = None,
    unread_count: int = 0,
) -> dict[str, Any]:
    return {
        "id": str(conversation.id),
        "kind": conversation.kind,
        "title": conversation.title,
        "facility": (
            {"id": str(conversation.facility_id), "name": facility_name}
            if conversation.facility_id
            else None
        ),
        "memberIds": [str(member_id) for member_id in member_ids],
        "counterpartId": str(counterpart_id) if counterpart_id else None,
        "lastMessageAt": last_at.isoformat() if last_at else None,
        "lastMessagePreview": _preview(last_body),
        "unreadCount": unread_count,
        "readCursor": (
            str(membership.last_read_message_id)
            if membership.last_read_message_id
            else None
        ),
        "handoverCount": 0,
        "createdAt": conversation.created_at.isoformat() if conversation.created_at else None,
        "updatedAt": conversation.updated_at.isoformat() if conversation.updated_at else None,
    }


def message_item(
    message: Any,
    *,
    mine: bool,
    sender_name: str | None,
    receipt: str | None = None,
) -> dict[str, Any]:
    """Persisted message acknowledgement.

    ``delivery`` is ``sent`` because the message is durably stored. It is not a
    recipient-delivery receipt; ``receipt`` is only supplied when known.
    """
    return {
        "id": str(message.id),
        "clientMessageId": message.client_message_id,
        "conversationId": str(message.conversation_id),
        "senderId": str(message.sender_staff_id),
        "senderName": sender_name,
        "body": message.body,
        "createdAt": message.created_at.isoformat() if message.created_at else None,
        "mine": mine,
        "delivery": "sent",
        "receipt": receipt,
        "kind": message.kind,
        "sequence": message.sequence,
    }
