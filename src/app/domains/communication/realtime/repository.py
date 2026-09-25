"""SQL for server-controlled realtime channel generation."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ....models.communication import RealtimeChannelState


class RealtimeChannelRepository:
    async def get(
        self, db: AsyncSession, *, organization_id: UUID, staff_member_id: UUID
    ) -> RealtimeChannelState | None:
        return await db.scalar(
            select(RealtimeChannelState).where(
                RealtimeChannelState.organization_id == organization_id,
                RealtimeChannelState.staff_member_id == staff_member_id,
            )
        )

    async def get_or_create(
        self, db: AsyncSession, *, organization_id: UUID, staff_member_id: UUID
    ) -> RealtimeChannelState:
        state = await self.get(
            db, organization_id=organization_id, staff_member_id=staff_member_id
        )
        if state is not None:
            return state
        now = datetime.now(UTC)
        stmt = (
            pg_insert(RealtimeChannelState)
            .values(
                id=uuid7(),
                organization_id=organization_id,
                staff_member_id=staff_member_id,
                generation=1,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["organization_id", "staff_member_id"]
            )
        )
        await db.execute(stmt)
        await db.commit()
        state = await self.get(
            db, organization_id=organization_id, staff_member_id=staff_member_id
        )
        assert state is not None
        return state

    async def bump_generation(
        self, db: AsyncSession, *, organization_id: UUID, staff_member_id: UUID
    ) -> RealtimeChannelState:
        """Retire all previously issued channels/tokens for this principal."""
        state = await self.get(
            db, organization_id=organization_id, staff_member_id=staff_member_id
        )
        if state is None:
            state = await self.get_or_create(
                db, organization_id=organization_id, staff_member_id=staff_member_id
            )
        state.generation += 1
        state.revoked_at = datetime.now(UTC)
        state.updated_at = state.revoked_at
        await db.commit()
        return state
