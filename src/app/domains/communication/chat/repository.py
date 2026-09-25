"""SQL for conversations, memberships, messages and read cursors."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ....models.communication import Conversation, ConversationMember, Message
from ....models.organization import Facility


class ChatRepository:
    async def get_conversation(
        self, db: AsyncSession, conversation_id: UUID
    ) -> Conversation | None:
        return await db.get(Conversation, conversation_id)

    async def lock_conversation(
        self, db: AsyncSession, conversation_id: UUID
    ) -> Conversation | None:
        return await db.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )

    async def get_membership(
        self, db: AsyncSession, *, conversation_id: UUID, staff_member_id: UUID
    ) -> ConversationMember | None:
        return await db.scalar(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.staff_member_id == staff_member_id,
                ConversationMember.left_at.is_(None),
            )
        )

    async def list_members(
        self, db: AsyncSession, conversation_id: UUID
    ) -> list[ConversationMember]:
        rows = await db.scalars(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.left_at.is_(None),
            )
        )
        return list(rows)

    async def members_for_conversations(
        self, db: AsyncSession, conversation_ids: list[UUID]
    ) -> dict[UUID, list[ConversationMember]]:
        if not conversation_ids:
            return {}
        rows = await db.scalars(
            select(ConversationMember).where(
                ConversationMember.conversation_id.in_(conversation_ids),
                ConversationMember.left_at.is_(None),
            )
        )
        grouped: dict[UUID, list[ConversationMember]] = {}
        for member in rows:
            grouped.setdefault(member.conversation_id, []).append(member)
        return grouped

    async def find_direct(
        self, db: AsyncSession, *, organization_id: UUID, direct_key: str
    ) -> Conversation | None:
        return await db.scalar(
            select(Conversation).where(
                Conversation.organization_id == organization_id,
                Conversation.direct_key == direct_key,
            )
        )

    async def create_conversation(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        kind: str,
        direct_key: str | None,
        facility_id: UUID | None,
        title: str | None,
        created_by_staff_id: UUID | None,
    ) -> tuple[Conversation, bool]:
        now = datetime.now(UTC)
        if direct_key is not None:
            stmt = (
                pg_insert(Conversation)
                .values(
                    id=uuid7(),
                    organization_id=organization_id,
                    kind=kind,
                    direct_key=direct_key,
                    facility_id=facility_id,
                    title=title,
                    created_by_staff_id=created_by_staff_id,
                    next_sequence=0,
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["organization_id", "direct_key"])
                .returning(Conversation.id)
            )
            new_id = (await db.execute(stmt)).scalar_one_or_none()
            if new_id is None:
                existing = await self.find_direct(
                    db, organization_id=organization_id, direct_key=direct_key
                )
                assert existing is not None
                return existing, False
            conversation = await db.get(Conversation, new_id)
            assert conversation is not None
            return conversation, True
        conversation = Conversation(
            organization_id=organization_id,
            kind=kind,
            direct_key=None,
            facility_id=facility_id,
            title=title,
            created_by_staff_id=created_by_staff_id,
        )
        db.add(conversation)
        await db.flush()
        return conversation, True

    async def add_member(
        self,
        db: AsyncSession,
        *,
        conversation_id: UUID,
        organization_id: UUID,
        staff_member_id: UUID,
        role: str = "member",
        join_sequence: int = 0,
    ) -> ConversationMember:
        stmt = (
            pg_insert(ConversationMember)
            .values(
                id=uuid7(),
                conversation_id=conversation_id,
                organization_id=organization_id,
                staff_member_id=staff_member_id,
                role=role,
                join_sequence=join_sequence,
                last_read_sequence=join_sequence,
                joined_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["conversation_id", "staff_member_id"])
        )
        await db.execute(stmt)
        membership = await db.scalar(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.staff_member_id == staff_member_id,
            )
        )
        assert membership is not None
        # Re-joining a previously left conversation clears left_at.
        if membership.left_at is not None:
            membership.left_at = None
            await db.flush()
        return membership

    async def remove_member(
        self, db: AsyncSession, membership: ConversationMember
    ) -> None:
        membership.left_at = datetime.now(UTC)
        await db.flush()

    async def find_message_by_client_id(
        self,
        db: AsyncSession,
        *,
        conversation_id: UUID,
        sender_staff_id: UUID,
        client_message_id: str,
    ) -> Message | None:
        return await db.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.sender_staff_id == sender_staff_id,
                Message.client_message_id == client_message_id,
            )
        )

    async def get_message(self, db: AsyncSession, message_id: UUID) -> Message | None:
        return await db.get(Message, message_id)

    async def list_messages(
        self,
        db: AsyncSession,
        *,
        conversation_id: UUID,
        before_sequence: int | None,
        limit: int,
    ) -> list[Message]:
        stmt = select(Message).where(Message.conversation_id == conversation_id)
        if before_sequence is not None:
            stmt = stmt.where(Message.sequence < before_sequence)
        stmt = stmt.order_by(Message.sequence.desc()).limit(limit + 1)
        return list((await db.scalars(stmt)).all())

    async def unread_count(
        self,
        db: AsyncSession,
        *,
        conversation_id: UUID,
        staff_member_id: UUID,
        after_sequence: int,
    ) -> int:
        count = await db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.sequence > after_sequence,
                Message.sender_staff_id != staff_member_id,
            )
        )
        return int(count or 0)

    async def list_conversations_for_staff(
        self, db: AsyncSession, *, organization_id: UUID, staff_member_id: UUID
    ) -> list[tuple[Conversation, ConversationMember, str | None]]:
        stmt = (
            select(Conversation, ConversationMember, Facility.name)
            .join(
                ConversationMember,
                ConversationMember.conversation_id == Conversation.id,
            )
            .outerjoin(Facility, Facility.id == Conversation.facility_id)
            .where(
                Conversation.organization_id == organization_id,
                Conversation.is_active.is_(True),
                ConversationMember.staff_member_id == staff_member_id,
                ConversationMember.left_at.is_(None),
            )
            .order_by(
                func.coalesce(Conversation.last_message_at, Conversation.created_at).desc()
            )
        )
        rows = (await db.execute(stmt)).all()
        return [(row[0], row[1], row[2]) for row in rows]

    async def last_messages(
        self, db: AsyncSession, conversation_ids: list[UUID]
    ) -> dict[UUID, tuple[str, datetime]]:
        if not conversation_ids:
            return {}
        stmt = (
            select(Message.conversation_id, Message.body, Message.created_at)
            .distinct(Message.conversation_id)
            .where(Message.conversation_id.in_(conversation_ids))
            .order_by(Message.conversation_id, Message.sequence.desc())
        )
        return {
            row[0]: (row[1], row[2]) for row in (await db.execute(stmt)).all()
        }
