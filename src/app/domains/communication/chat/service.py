"""Chat service: authorized conversations, durable messages, read cursors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....models.communication import (
    Conversation,
    ConversationMember,
    Message,
)
from ....models.identity import UserAccount
from ....models.organization import Facility, StaffMember
from ..delivery.repository import DeliveryJobRepository
from ..notifications.constants import (
    CHAT_EVENT_TYPES,
    EVENT_CHAT_DIRECT,
    EVENT_CHAT_TEAM,
)
from ..notifications.repository import NotificationRepository
from ..notifications.workflow import record_notification
from .constants import (
    CONVERSATION_DIRECT,
    CONVERSATION_TEAM,
    EVENT_CONVERSATION_READ,
    EVENT_MESSAGE_CREATED,
    REALTIME_KIND_CONVERSATION,
)
from .repository import ChatRepository
from .serialization import conversation_item, message_item

if TYPE_CHECKING:
    from ..dependencies import CommunicationStaffContext

CHAT_MAX_GROUP_MEMBERS = 20
CHAT_MAX_MESSAGE_LENGTH = 4000
CHAT_HISTORY_PAGE_SIZE = 50
CHAT_NOTIFY_DEBOUNCE_SECONDS = 300


def _direct_key(first: UUID, second: UUID) -> str:
    return ":".join(sorted([str(first), str(second)]))


async def staff_display_names(
    db: AsyncSession, staff_ids: set[UUID] | list[UUID]
) -> dict[UUID, str | None]:
    if not staff_ids:
        return {}
    rows = await db.execute(
        select(StaffMember.id, UserAccount.display_name)
        .join(UserAccount, UserAccount.id == StaffMember.user_account_id)
        .where(StaffMember.id.in_(list(staff_ids)))
    )
    return {row[0]: row[1] for row in rows.all()}


class ChatService:
    def __init__(
        self,
        repository: ChatRepository | None = None,
        notifications: NotificationRepository | None = None,
        delivery: DeliveryJobRepository | None = None,
        *,
        max_group_members: int = CHAT_MAX_GROUP_MEMBERS,
        max_message_length: int = CHAT_MAX_MESSAGE_LENGTH,
        history_page_size: int = CHAT_HISTORY_PAGE_SIZE,
        notify_debounce_seconds: int = CHAT_NOTIFY_DEBOUNCE_SECONDS,
    ) -> None:
        self.repository = repository or ChatRepository()
        self.notifications = notifications or NotificationRepository()
        self.delivery = delivery or DeliveryJobRepository()
        self.max_group_members = max(2, int(max_group_members))
        self.max_message_length = max(1, int(max_message_length))
        self.history_page_size = max(1, int(history_page_size))
        self.notify_debounce_seconds = max(0, int(notify_debounce_seconds))

    # ---------------------------------------------------------- authorization

    async def _authorize_conversation(
        self, db: AsyncSession, context: CommunicationStaffContext, conversation_id: UUID
    ) -> tuple[Conversation, ConversationMember]:
        conversation = await self.repository.get_conversation(db, conversation_id)
        if conversation is None or conversation.organization_id != context.organization_id:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        membership = await self.repository.get_membership(
            db, conversation_id=conversation_id, staff_member_id=context.staff_member_id
        )
        if membership is None:
            # Membership is current-only: removed/foreign callers are denied.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation access is not permitted.",
            )
        return conversation, membership

    async def _valid_staff_ids(
        self, db: AsyncSession, *, organization_id: UUID, staff_ids: set[UUID]
    ) -> set[UUID]:
        if not staff_ids:
            return set()
        rows = await db.scalars(
            select(StaffMember.id).where(
                StaffMember.id.in_(list(staff_ids)),
                StaffMember.organization_id == organization_id,
                StaffMember.is_active.is_(True),
            )
        )
        return set(rows)

    def _authorize_facility(
        self, context: CommunicationStaffContext, facility_uuid: UUID | None
    ) -> UUID | None:
        if facility_uuid is None:
            return None
        if facility_uuid not in context.facility_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Facility access is not permitted.",
            )
        return facility_uuid

    # ---------------------------------------------------------- conversations

    async def list_conversations(
        self, db: AsyncSession, context: CommunicationStaffContext
    ) -> dict:
        rows = await self.repository.list_conversations_for_staff(
            db,
            organization_id=context.organization_id,
            staff_member_id=context.staff_member_id,
        )
        ids = [row[0].id for row in rows]
        members_map = await self.repository.members_for_conversations(db, ids)
        last_map = await self.repository.last_messages(db, ids)
        items = []
        for conversation, membership, facility_name in rows:
            member_ids = [m.staff_member_id for m in members_map.get(conversation.id, [])]
            counterpart = None
            if conversation.kind == CONVERSATION_DIRECT:
                counterpart = next(
                    (m for m in member_ids if m != context.staff_member_id), None
                )
            unread = await self.repository.unread_count(
                db,
                conversation_id=conversation.id,
                staff_member_id=context.staff_member_id,
                after_sequence=membership.last_read_sequence,
            )
            body, last_at = last_map.get(conversation.id, (None, None))
            items.append(
                conversation_item(
                    conversation,
                    membership,
                    member_ids=member_ids,
                    counterpart_id=counterpart,
                    facility_name=facility_name,
                    last_body=body,
                    last_at=last_at,
                    unread_count=unread,
                )
            )
        return {"items": items}

    async def create_conversation(
        self, db: AsyncSession, context: CommunicationStaffContext, payload: dict
    ) -> dict:
        kind = payload["kind"]
        requested = set(payload["member_uuids"])
        targets = requested | {context.staff_member_id}
        valid = await self._valid_staff_ids(
            db, organization_id=context.organization_id, staff_ids=targets
        )
        if valid != targets:
            raise HTTPException(
                status_code=422, detail="One or more members are not active staff."
            )
        facility_id = self._authorize_facility(context, payload.get("facility_uuid"))
        if kind == CONVERSATION_DIRECT:
            if len(targets) != 2:
                raise HTTPException(
                    status_code=422, detail="Direct conversations require exactly two members."
                )
            other = next(iter(targets - {context.staff_member_id}), None)
            assert other is not None
            conversation, created = await self.repository.create_conversation(
                db,
                organization_id=context.organization_id,
                kind=CONVERSATION_DIRECT,
                direct_key=_direct_key(context.staff_member_id, other),
                facility_id=facility_id,
                title=None,
                created_by_staff_id=context.staff_member_id,
            )
            if created:
                for staff_id in (context.staff_member_id, other):
                    await self.repository.add_member(
                        db,
                        conversation_id=conversation.id,
                        organization_id=context.organization_id,
                        staff_member_id=staff_id,
                    )
        elif kind == CONVERSATION_TEAM:
            if len(targets) > self.max_group_members:
                raise HTTPException(
                    status_code=422,
                    detail=f"Team conversations are limited to {self.max_group_members} members.",
                )
            conversation, created = await self.repository.create_conversation(
                db,
                organization_id=context.organization_id,
                kind=CONVERSATION_TEAM,
                direct_key=None,
                facility_id=facility_id,
                title=payload.get("title"),
                created_by_staff_id=context.staff_member_id,
            )
            if created:
                for staff_id in targets:
                    await self.repository.add_member(
                        db,
                        conversation_id=conversation.id,
                        organization_id=context.organization_id,
                        staff_member_id=staff_id,
                        role="owner" if staff_id == context.staff_member_id else "member",
                    )
        else:
            raise HTTPException(status_code=422, detail="Unsupported conversation kind.")
        await db.commit()
        return await self.get_conversation(db, context, conversation.id)

    async def get_conversation(
        self, db: AsyncSession, context: CommunicationStaffContext, conversation_id: UUID
    ) -> dict:
        conversation, membership = await self._authorize_conversation(
            db, context, conversation_id
        )
        members = await self.repository.list_members(db, conversation_id)
        member_ids = [m.staff_member_id for m in members]
        counterpart = None
        if conversation.kind == CONVERSATION_DIRECT:
            counterpart = next(
                (m for m in member_ids if m != context.staff_member_id), None
            )
        names = await staff_display_names(db, set(member_ids))
        facility_name = None
        if conversation.facility_id:
            facility_name = await db.scalar(
                select(Facility.name).where(Facility.id == conversation.facility_id)
            )
        unread = await self.repository.unread_count(
            db,
            conversation_id=conversation_id,
            staff_member_id=context.staff_member_id,
            after_sequence=membership.last_read_sequence,
        )
        last_map = await self.repository.last_messages(db, [conversation_id])
        body, last_at = last_map.get(conversation_id, (None, None))
        item = conversation_item(
            conversation,
            membership,
            member_ids=member_ids,
            counterpart_id=counterpart,
            facility_name=facility_name,
            last_body=body,
            last_at=last_at,
            unread_count=unread,
        )
        item["members"] = [
            {
                "id": str(m.staff_member_id),
                "role": m.role,
                "name": names.get(m.staff_member_id),
            }
            for m in members
        ]
        return item

    async def add_member(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        conversation_id: UUID,
        staff_uuid: UUID,
    ) -> dict:
        conversation, _membership = await self._authorize_conversation(
            db, context, conversation_id
        )
        if conversation.kind != CONVERSATION_TEAM:
            raise HTTPException(status_code=409, detail="Members are managed only for team conversations.")
        if await self._valid_staff_ids(
            db, organization_id=context.organization_id, staff_ids={staff_uuid}
        ) != {staff_uuid}:
            raise HTTPException(status_code=422, detail="Member is not active staff.")
        members = await self.repository.list_members(db, conversation_id)
        existing = {m.staff_member_id for m in members}
        if staff_uuid not in existing and len(existing) + 1 > self.max_group_members:
            raise HTTPException(
                status_code=422,
                detail=f"Team conversations are limited to {self.max_group_members} members.",
            )
        await self.repository.add_member(
            db,
            conversation_id=conversation_id,
            organization_id=context.organization_id,
            staff_member_id=staff_uuid,
            join_sequence=conversation.next_sequence,
        )
        await db.commit()
        item = await self.get_conversation(db, context, conversation_id)
        await self._enqueue_conversation_event(
            db, conversation, "conversation.updated", [m.staff_member_id for m in members]
        )
        await db.commit()
        return item

    async def remove_member(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        conversation_id: UUID,
        staff_uuid: UUID,
    ) -> dict:
        conversation, _membership = await self._authorize_conversation(
            db, context, conversation_id
        )
        if conversation.kind != CONVERSATION_TEAM:
            raise HTTPException(status_code=409, detail="Members are managed only for team conversations.")
        target = next(
            (m for m in await self.repository.list_members(db, conversation_id)
             if m.staff_member_id == staff_uuid),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="Member not found in conversation.")
        await self.repository.remove_member(db, target)
        await db.commit()
        return await self.get_conversation(db, context, conversation_id)

    # ---------------------------------------------------------- messages

    async def _enqueue_message_jobs(
        self,
        db: AsyncSession,
        *,
        conversation: Conversation,
        message: Message,
        recipients: list[UUID],
    ) -> None:
        for staff_id in recipients:
            await self.delivery.enqueue(
                db,
                channel="realtime",
                event_type=EVENT_MESSAGE_CREATED,
                dedup_key=f"message:{message.id}:{staff_id}",
                payload={
                    "kind": REALTIME_KIND_CONVERSATION,
                    "event_type": EVENT_MESSAGE_CREATED,
                    "conversation_id": str(conversation.id),
                    "message_id": str(message.id),
                    "recipient_staff_id": str(staff_id),
                },
                organization_id=conversation.organization_id,
                facility_id=conversation.facility_id,
                recipient_staff_id=staff_id,
            )

    async def _enqueue_conversation_event(
        self,
        db: AsyncSession,
        conversation: Conversation,
        event_type: str,
        recipients: list[UUID],
    ) -> None:
        for staff_id in recipients:
            await self.delivery.enqueue(
                db,
                channel="realtime",
                event_type=event_type,
                dedup_key=f"{event_type}:{conversation.id}:{staff_id}:{datetime.now(UTC).timestamp()}",
                payload={
                    "kind": REALTIME_KIND_CONVERSATION,
                    "event_type": event_type,
                    "conversation_id": str(conversation.id),
                    "recipient_staff_id": str(staff_id),
                },
                organization_id=conversation.organization_id,
                facility_id=conversation.facility_id,
                recipient_staff_id=staff_id,
                max_attempts=1,
            )

    async def _notify_recipients(
        self,
        db: AsyncSession,
        *,
        context: CommunicationStaffContext,
        conversation: Conversation,
        message: Message,
        recipients: list[UUID],
        sender_name: str | None,
    ) -> None:
        if not recipients:
            return
        since = datetime.now(UTC) - timedelta(seconds=self.notify_debounce_seconds)
        pending: list[UUID] = []
        for staff_id in recipients:
            recent = await self.notifications.find_coalescible(
                db,
                coalesce_key=f"conversation:{conversation.id}",
                staff_member_id=staff_id,
                event_types=CHAT_EVENT_TYPES,
                since=since,
            )
            if recent is None:  # debounce: skip while an unread thread alert exists
                pending.append(staff_id)
        if not pending:
            return
        event_type = (
            EVENT_CHAT_DIRECT if conversation.kind == CONVERSATION_DIRECT else EVENT_CHAT_TEAM
        )
        await record_notification(
            db,
            event_type=event_type,
            organization_id=conversation.organization_id,
            facility_id=conversation.facility_id,
            actor_user_id=context.account_id,
            recipient_staff_ids=pending,
            title="New message",
            context=sender_name,
            task_type=None,
            task_state="none",
            resource_type="conversation",
            resource_id=str(conversation.id),
            coalesce_key=f"conversation:{conversation.id}",
            dedup_key=f"chat.notify:{message.id}",
        )

    async def send_message(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        conversation_id: UUID,
        *,
        body: str,
        client_message_id: str | None,
    ) -> dict:
        conversation, membership = await self._authorize_conversation(
            db, context, conversation_id
        )
        if not conversation.is_active:
            raise HTTPException(status_code=409, detail="Conversation is closed.")
        if len(body) > self.max_message_length:
            raise HTTPException(
                status_code=422,
                detail=f"Message exceeds {self.max_message_length} characters.",
            )
        if not body.strip():
            raise HTTPException(status_code=422, detail="Message body is empty.")

        names = await staff_display_names(db, {membership.staff_member_id})
        sender_name = names.get(membership.staff_member_id)

        locked = await self.repository.lock_conversation(db, conversation_id)
        assert locked is not None
        if client_message_id:
            existing = await self.repository.find_message_by_client_id(
                db,
                conversation_id=conversation_id,
                sender_staff_id=membership.staff_member_id,
                client_message_id=client_message_id,
            )
            if existing is not None:
                await db.commit()  # release the conversation lock
                if existing.body != body:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "code": "IDEMPOTENCY_KEY_REUSED",
                            "message": "client_message_id was reused with different content.",
                        },
                    )
                return message_item(existing, mine=True, sender_name=sender_name)

        now = datetime.now(UTC)
        locked.next_sequence += 1
        message = Message(
            conversation_id=conversation_id,
            organization_id=conversation.organization_id,
            sender_staff_id=membership.staff_member_id,
            body=body,
            sequence=locked.next_sequence,
            sender_user_id=context.account_id,
            facility_id=conversation.facility_id,
            client_message_id=client_message_id,
            kind="text",
            created_at=now,
        )
        db.add(message)
        locked.last_message_at = now
        locked.updated_at = now
        await db.flush()

        members = await self.repository.list_members(db, conversation_id)
        # Realtime fan-out reaches every currently authorized member, including
        # the sender, so the sender's other sessions/tabs reconcile too. The
        # in-app notification still suppresses the sender.
        fanout = list(dict.fromkeys(m.staff_member_id for m in members))
        recipients = [
            staff_id
            for staff_id in fanout
            if staff_id != membership.staff_member_id
        ]
        await self._enqueue_message_jobs(
            db, conversation=conversation, message=message, recipients=fanout
        )
        await self._notify_recipients(
            db,
            context=context,
            conversation=conversation,
            message=message,
            recipients=recipients,
            sender_name=sender_name,
        )
        await db.commit()
        return message_item(message, mine=True, sender_name=sender_name)

    async def list_messages(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        conversation_id: UUID,
        *,
        before: int | None,
        limit: int | None,
    ) -> dict:
        await self._authorize_conversation(db, context, conversation_id)
        page = min(limit or self.history_page_size, self.history_page_size)
        rows = await self.repository.list_messages(
            db, conversation_id=conversation_id, before_sequence=before, limit=page
        )
        has_more = len(rows) > page
        rows = rows[:page]
        rows.reverse()  # ascending sequence for display
        names = await staff_display_names(db, {m.sender_staff_id for m in rows})
        items = [
            message_item(
                message,
                mine=message.sender_staff_id == context.staff_member_id,
                sender_name=names.get(message.sender_staff_id),
            )
            for message in rows
        ]
        next_cursor = str(rows[0].sequence) if has_more and rows else None
        return {"items": items, "has_more": has_more, "next_cursor": next_cursor}

    async def mark_read(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        conversation_id: UUID,
        message_id: UUID,
    ) -> dict:
        _conversation, membership = await self._authorize_conversation(
            db, context, conversation_id
        )
        message = await self.repository.get_message(db, message_id)
        if message is None or message.conversation_id != conversation_id:
            raise HTTPException(status_code=404, detail="Message not found in conversation.")
        # Monotonic only-forward cursor: never regress.
        if message.sequence > membership.last_read_sequence:
            membership.last_read_sequence = message.sequence
            membership.last_read_message_id = message.id
            await db.flush()
            await self._enqueue_conversation_event(
                db, _conversation, EVENT_CONVERSATION_READ, [context.staff_member_id]
            )
            await db.commit()
        unread = await self.repository.unread_count(
            db,
            conversation_id=conversation_id,
            staff_member_id=context.staff_member_id,
            after_sequence=membership.last_read_sequence,
        )
        return {
            "conversationId": str(conversation_id),
            "readCursor": (
                str(membership.last_read_message_id)
                if membership.last_read_message_id
                else None
            ),
            "unreadCount": unread,
        }


def build_chat_service(settings) -> ChatService:
    return ChatService(
        max_group_members=settings.COMMUNICATION_CHAT_MAX_GROUP_MEMBERS,
        max_message_length=settings.COMMUNICATION_CHAT_MAX_MESSAGE_LENGTH,
        history_page_size=settings.COMMUNICATION_CHAT_HISTORY_PAGE_SIZE,
        notify_debounce_seconds=settings.COMMUNICATION_CHAT_NOTIFY_DEBOUNCE_SECONDS,
    )
