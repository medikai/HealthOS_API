"""SQL for the notification inbox, read state, catch-up and preferences."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ....models.communication import (
    NotificationEvent,
    NotificationPreference,
    NotificationRecipient,
)
from ....models.organization import Facility
from .constants import KIND_TASK


class NotificationRepository:
    async def create_event(
        self, db: AsyncSession, *, values: dict
    ) -> tuple[NotificationEvent, bool]:
        """Insert idempotently by ``dedup_key``. Does not commit."""
        values.setdefault("id", uuid7())
        values.setdefault("created_at", datetime.now(UTC))
        values.setdefault("occurred_at", values["created_at"])
        values.setdefault("updated_at", values["created_at"])
        stmt = (
            pg_insert(NotificationEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(NotificationEvent.id)
        )
        new_id = (await db.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            existing = await db.scalar(
                select(NotificationEvent).where(
                    NotificationEvent.dedup_key == values["dedup_key"]
                )
            )
            assert existing is not None
            return existing, False
        event = await db.get(NotificationEvent, new_id)
        assert event is not None
        return event, True

    async def add_recipients(
        self,
        db: AsyncSession,
        *,
        event: NotificationEvent,
        staff_member_ids: list[UUID],
        facility_id: UUID | None = None,
    ) -> None:
        if not staff_member_ids:
            return
        now = datetime.now(UTC)
        rows = [
            {
                "id": uuid7(),
                "notification_id": event.id,
                "organization_id": event.organization_id,
                "staff_member_id": staff_id,
                "facility_id": facility_id or event.facility_id,
                "created_at": now,
            }
            for staff_id in staff_member_ids
        ]
        await db.execute(
            pg_insert(NotificationRecipient)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["notification_id", "staff_member_id"])
        )

    async def get_event(
        self, db: AsyncSession, notification_id: UUID
    ) -> NotificationEvent | None:
        return await db.get(NotificationEvent, notification_id)

    def _feed_query(
        self,
        *,
        organization_id: UUID,
        staff_member_id: UUID,
        tab: str,
        priority: str | None,
        facility_id: UUID | None,
        query: str | None,
    ):
        stmt = (
            select(NotificationEvent, NotificationRecipient, Facility.name)
            .join(
                NotificationRecipient,
                NotificationRecipient.notification_id == NotificationEvent.id,
            )
            .outerjoin(Facility, Facility.id == NotificationEvent.facility_id)
            .where(
                NotificationEvent.organization_id == organization_id,
                NotificationRecipient.staff_member_id == staff_member_id,
                NotificationEvent.superseded_by_id.is_(None),
            )
        )
        if tab == "unread":
            stmt = stmt.where(NotificationRecipient.read_at.is_(None))
        elif tab == "action":
            stmt = stmt.where(
                NotificationEvent.kind == KIND_TASK,
                NotificationEvent.task_state == "awaiting",
            )
        if priority:
            stmt = stmt.where(NotificationEvent.priority == priority)
        if facility_id:
            stmt = stmt.where(NotificationEvent.facility_id == facility_id)
        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                or_(
                    NotificationEvent.title.ilike(pattern),
                    NotificationEvent.context.ilike(pattern),
                )
            )
        return stmt

    async def list_for_staff(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        staff_member_id: UUID,
        limit: int,
        cursor: tuple[datetime, str] | None,
        tab: str,
        priority: str | None,
        facility_id: UUID | None,
        query: str | None,
    ) -> list[tuple[NotificationEvent, NotificationRecipient, str | None]]:
        stmt = self._feed_query(
            organization_id=organization_id,
            staff_member_id=staff_member_id,
            tab=tab,
            priority=priority,
            facility_id=facility_id,
            query=query,
        )
        if cursor is not None:
            ts, entity_id = cursor
            stmt = stmt.where(
                or_(
                    NotificationEvent.updated_at < ts,
                    and_(
                        NotificationEvent.updated_at == ts,
                        NotificationEvent.id < entity_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            NotificationEvent.updated_at.desc(), NotificationEvent.id.desc()
        ).limit(limit + 1)
        rows = (await db.execute(stmt)).all()
        return [(row[0], row[1], row[2]) for row in rows]

    async def get_for_staff(
        self, db: AsyncSession, *, staff_member_id: UUID, notification_id: UUID
    ) -> tuple[NotificationEvent, NotificationRecipient, str | None] | None:
        stmt = (
            select(NotificationEvent, NotificationRecipient, Facility.name)
            .join(
                NotificationRecipient,
                NotificationRecipient.notification_id == NotificationEvent.id,
            )
            .outerjoin(Facility, Facility.id == NotificationEvent.facility_id)
            .where(
                NotificationEvent.id == notification_id,
                NotificationRecipient.staff_member_id == staff_member_id,
            )
        )
        row = (await db.execute(stmt)).first()
        if row is None:
            return None
        return row[0], row[1], row[2]

    async def mark_read(
        self,
        db: AsyncSession,
        *,
        staff_member_id: UUID,
        notification_id: UUID,
        now: datetime | None = None,
    ) -> bool:
        """Idempotent read. Never touches task_state."""
        now = now or datetime.now(UTC)
        recipient = await db.scalar(
            select(NotificationRecipient).where(
                NotificationRecipient.notification_id == notification_id,
                NotificationRecipient.staff_member_id == staff_member_id,
            )
        )
        if recipient is None:
            return False
        if recipient.read_at is None:
            recipient.read_at = now
            await db.commit()
        return True

    async def mark_all_read(
        self,
        db: AsyncSession,
        *,
        staff_member_id: UUID,
        organization_id: UUID,
        cutoff: datetime,
        tab: str = "unread",
        priority: str | None = None,
        facility_id: UUID | None = None,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        scope = select(NotificationEvent.id).where(
            NotificationEvent.organization_id == organization_id,
            NotificationEvent.created_at <= cutoff,
        )
        if tab == "action":
            scope = scope.where(
                NotificationEvent.kind == KIND_TASK,
                NotificationEvent.task_state == "awaiting",
            )
        if priority:
            scope = scope.where(NotificationEvent.priority == priority)
        if facility_id:
            scope = scope.where(NotificationEvent.facility_id == facility_id)
        result = await db.execute(
            update(NotificationRecipient)
            .where(
                NotificationRecipient.staff_member_id == staff_member_id,
                NotificationRecipient.notification_id.in_(scope),
                NotificationRecipient.read_at.is_(None),
            )
            .values(read_at=now)
        )
        await db.commit()
        return int(result.rowcount or 0)

    async def counts(
        self,
        db: AsyncSession,
        *,
        staff_member_id: UUID,
        organization_id: UUID,
        facility_id: UUID | None = None,
    ) -> dict[str, int]:
        base = (
            select(NotificationRecipient.id)
            .join(
                NotificationEvent,
                NotificationEvent.id == NotificationRecipient.notification_id,
            )
            .where(
                NotificationRecipient.staff_member_id == staff_member_id,
                NotificationEvent.organization_id == organization_id,
                NotificationEvent.superseded_by_id.is_(None),
            )
        )
        if facility_id:
            base = base.where(NotificationEvent.facility_id == facility_id)
        unread = await db.scalar(
            select(func.count()).select_from(
                base.where(NotificationRecipient.read_at.is_(None)).subquery()
            )
        )
        needs_action = await db.scalar(
            select(func.count()).select_from(
                base.where(
                    NotificationEvent.kind == KIND_TASK,
                    NotificationEvent.task_state == "awaiting",
                ).subquery()
            )
        )
        return {"unread": int(unread or 0), "needsAction": int(needs_action or 0)}

    async def changes_since(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        staff_member_id: UUID,
        since: datetime,
        limit: int,
    ) -> list[tuple[NotificationEvent, NotificationRecipient, str | None]]:
        stmt = (
            select(NotificationEvent, NotificationRecipient, Facility.name)
            .join(
                NotificationRecipient,
                NotificationRecipient.notification_id == NotificationEvent.id,
            )
            .outerjoin(Facility, Facility.id == NotificationEvent.facility_id)
            .where(
                NotificationEvent.organization_id == organization_id,
                NotificationRecipient.staff_member_id == staff_member_id,
                NotificationEvent.updated_at >= since,
            )
            .order_by(NotificationEvent.updated_at.asc(), NotificationEvent.id.asc())
            .limit(limit + 1)
        )
        rows = (await db.execute(stmt)).all()
        return [(row[0], row[1], row[2]) for row in rows]

    async def find_coalescible(
        self,
        db: AsyncSession,
        *,
        coalesce_key: str,
        staff_member_id: UUID,
        event_types: tuple[str, ...],
        since: datetime,
    ) -> tuple[NotificationEvent, NotificationRecipient] | None:
        stmt = (
            select(NotificationEvent, NotificationRecipient)
            .join(
                NotificationRecipient,
                NotificationRecipient.notification_id == NotificationEvent.id,
            )
            .where(
                NotificationEvent.coalesce_key == coalesce_key,
                NotificationEvent.event_type.in_(event_types),
                NotificationEvent.superseded_by_id.is_(None),
                NotificationEvent.updated_at >= since,
                NotificationRecipient.staff_member_id == staff_member_id,
                NotificationRecipient.read_at.is_(None),
            )
            .order_by(NotificationEvent.updated_at.desc())
            .limit(1)
        )
        row = (await db.execute(stmt)).first()
        return (row[0], row[1]) if row else None

    async def bump_revision(
        self, db: AsyncSession, event: NotificationEvent, *, values: dict
    ) -> NotificationEvent:
        for key, value in values.items():
            setattr(event, key, value)
        event.revision = (event.revision or 1) + 1
        event.updated_at = datetime.now(UTC)
        await db.flush()
        return event

    async def actionable_by_coalesce(
        self, db: AsyncSession, *, coalesce_key: str
    ) -> list[NotificationEvent]:
        stmt = select(NotificationEvent).where(
            NotificationEvent.coalesce_key == coalesce_key,
            NotificationEvent.task_state == "awaiting",
            NotificationEvent.superseded_by_id.is_(None),
        )
        return list((await db.scalars(stmt)).all())

    async def supersede(
        self, db: AsyncSession, old_event: NotificationEvent, new_event: NotificationEvent
    ) -> None:
        old_event.superseded_by_id = new_event.id
        old_event.task_state = "superseded"
        old_event.updated_at = datetime.now(UTC)
        await db.flush()

    async def get_preferences(
        self, db: AsyncSession, *, organization_id: UUID, staff_member_id: UUID
    ) -> list[NotificationPreference]:
        stmt = select(NotificationPreference).where(
            NotificationPreference.organization_id == organization_id,
            NotificationPreference.staff_member_id == staff_member_id,
        )
        return list((await db.scalars(stmt)).all())

    async def upsert_preference(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        staff_member_id: UUID,
        category: str,
        values: dict,
    ) -> None:
        now = datetime.now(UTC)
        insert_values = {
            "id": uuid7(),
            "organization_id": organization_id,
            "staff_member_id": staff_member_id,
            "category": category,
            "created_at": now,
            "updated_at": now,
            **values,
        }
        update_columns = {**values, "updated_at": now}
        stmt = (
            pg_insert(NotificationPreference)
            .values(**insert_values)
            .on_conflict_do_update(
                index_elements=["organization_id", "staff_member_id", "category"],
                set_=update_columns,
            )
        )
        await db.execute(stmt)
