"""SQL for per-facility staff work status."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....models.communication import WorkStatus


class WorkStatusRepository:
    async def get(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        facility_id: UUID,
        staff_member_id: UUID,
    ) -> WorkStatus | None:
        return await db.scalar(
            select(WorkStatus).where(
                WorkStatus.organization_id == organization_id,
                WorkStatus.facility_id == facility_id,
                WorkStatus.staff_member_id == staff_member_id,
            )
        )

    async def upsert(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        facility_id: UUID,
        staff_member_id: UUID,
        work_state: str,
        duty: str | None,
        source: str,
        return_at: datetime | None,
    ) -> WorkStatus:
        now = datetime.now(UTC)
        status_row = await self.get(
            db,
            organization_id=organization_id,
            facility_id=facility_id,
            staff_member_id=staff_member_id,
        )
        if status_row is None:
            status_row = WorkStatus(
                organization_id=organization_id,
                facility_id=facility_id,
                staff_member_id=staff_member_id,
                work_state=work_state,
                duty=duty,
                source=source,
                return_at=return_at,
                created_at=now,
                updated_at=now,
            )
            db.add(status_row)
        else:
            status_row.work_state = work_state
            status_row.duty = duty
            status_row.source = source
            status_row.return_at = return_at
            status_row.updated_at = now
        await db.flush()
        return status_row
