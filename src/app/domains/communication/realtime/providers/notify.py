"""Realtime delivery provider: publish minimal notification invalidation events.

Rechecks cancellation/reassignment/eligibility before publishing and never
carries message bodies or patient data on the broker; the browser fetches
authorized detail over HTTP.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from .....core.db.database import local_session
from .....models.communication import (
    Conversation,
    ConversationMember,
    NotificationRecipient,
    RealtimeChannelState,
)
from ...notifications.repository import NotificationRepository
from ...notifications.service import NotificationService
from ...shared.envelope import build_event_envelope
from ...shared.errors import PermanentDeliveryError
from ...utils.channels import own_user_channel
from .ably import AblyRealtimeProvider, build_ably_token_provider

REALTIME_KIND_CONVERSATION = "conversation"


class RealtimeNotificationProvider:
    channel = "realtime"

    def __init__(
        self,
        *,
        publisher: AblyRealtimeProvider,
        repository: NotificationRepository | None = None,
        service: NotificationService | None = None,
        session_factory: Any = None,
    ) -> None:
        self.publisher = publisher
        self.repository = repository or NotificationRepository()
        self.service = service or NotificationService()
        self._session_factory = session_factory or local_session

    @property
    def configured(self) -> bool:
        return bool(getattr(self.publisher, "configured", False))

    async def deliver(self, job: Any) -> dict:
        payload = job.payload or {}
        if payload.get("kind") == REALTIME_KIND_CONVERSATION:
            return await self._deliver_conversation(payload)
        return await self._deliver_notification(payload)

    async def _deliver_conversation(self, payload: dict) -> dict:
        conversation_id = _uuid(payload.get("conversation_id"))
        staff_member_id = _uuid(payload.get("recipient_staff_id"))
        if conversation_id is None or staff_member_id is None:
            raise PermanentDeliveryError(
                "conversation_payload_invalid", "Job is missing conversation identity."
            )
        async with self._session_factory() as db:
            conversation = await db.get(Conversation, conversation_id)
            membership = await db.scalar(
                select(ConversationMember.id).where(
                    ConversationMember.conversation_id == conversation_id,
                    ConversationMember.staff_member_id == staff_member_id,
                    ConversationMember.left_at.is_(None),
                )
            )
            if conversation is None or membership is None:
                # removed/inactive membership: stop delivery, never leak future state
                return {"provider": "ably", "published": False, "decision": "ineligible"}
            generation = await db.scalar(
                select(RealtimeChannelState.generation).where(
                    RealtimeChannelState.organization_id == conversation.organization_id,
                    RealtimeChannelState.staff_member_id == staff_member_id,
                )
            )
            channel = own_user_channel(
                self.publisher.namespace,
                conversation.organization_id,
                staff_member_id,
                generation or 1,
            )
            event_type = payload.get("event_type") or "message.created"
            envelope = build_event_envelope(
                event_type,
                entity_id=conversation_id,
                entity_version=None,
                organization_id=conversation.organization_id,
                facility_id=conversation.facility_id,
                recipient_staff_id=staff_member_id,
                data={
                    "conversation_id": str(conversation_id),
                    "message_id": payload.get("message_id"),
                },
            )
        await self.publisher.publish(
            channel=channel, event_type=event_type, payload=envelope
        )
        return {"provider": "ably", "published": True}

    async def _deliver_notification(self, payload: dict) -> dict:
        notification_id = _uuid(payload.get("notification_id"))
        staff_member_id = _uuid(payload.get("staff_member_id"))
        if notification_id is None or staff_member_id is None:
            raise PermanentDeliveryError(
                "notification_payload_invalid", "Job is missing notification identity."
            )

        async with self._session_factory() as db:
            event = await self.repository.get_event(db, notification_id)
            recipient = await db.scalar(
                select(NotificationRecipient).where(
                    NotificationRecipient.notification_id == notification_id,
                    NotificationRecipient.staff_member_id == staff_member_id,
                )
            )
            if event is None or recipient is None:
                raise PermanentDeliveryError(
                    "notification_missing", "Notification or recipient was not found."
                )
            decision = await self.service.evaluate_dispatch(db, event, recipient)
            if decision != "publish":
                await db.commit()
                return {"provider": "ably", "published": False, "decision": decision}
            generation = await db.scalar(
                select(RealtimeChannelState.generation).where(
                    RealtimeChannelState.organization_id == event.organization_id,
                    RealtimeChannelState.staff_member_id == staff_member_id,
                )
            )
            channel = own_user_channel(
                self.publisher.namespace,
                event.organization_id,
                staff_member_id,
                generation or 1,
            )
            envelope = build_event_envelope(
                event.event_type,
                entity_id=event.id,
                entity_version=event.revision,
                organization_id=event.organization_id,
                facility_id=event.facility_id,
                recipient_staff_id=staff_member_id,
                data={
                    "notification_id": str(event.id),
                    "priority": event.priority,
                    "category": event.category,
                    "kind": event.kind,
                    "task_state": event.task_state,
                    "correlation_type": event.resource_type,
                    "correlation_id": str(event.resource_id) if event.resource_id else None,
                },
            )

        # network call outside any SQL transaction
        await self.publisher.publish(
            channel=channel, event_type=event.event_type, payload=envelope
        )

        async with self._session_factory() as db:
            recipient = await db.scalar(
                select(NotificationRecipient).where(
                    NotificationRecipient.notification_id == notification_id,
                    NotificationRecipient.staff_member_id == staff_member_id,
                )
            )
            if recipient is not None and recipient.delivered_at is None:
                recipient.delivered_at = datetime.now(UTC)
                await db.commit()
        return {"provider": "ably", "published": True}


def _uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def build_realtime_notification_provider(settings: Any) -> RealtimeNotificationProvider | None:
    publisher = build_ably_token_provider(settings)
    if not publisher.configured:
        return None
    return RealtimeNotificationProvider(publisher=publisher)
