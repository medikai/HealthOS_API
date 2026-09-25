"""Work status service.

Connection (online/reconnecting/offline) is owned by the client/Ably presence
and is not stored here as authority. Work availability and duty are explicit,
per facility context, and a staff override is never overwritten by a derived
consultation update while it is still in effect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ....models.communication import (
    DUTY_STATES,
    WORK_STATES,
    WORK_STATUS_SOURCE_CONSULTATION,
    WORK_STATUS_SOURCE_STAFF,
)
from ....models.organization import Facility
from .repository import WorkStatusRepository

if TYPE_CHECKING:
    from ..dependencies import CommunicationStaffContext


class WorkStatusService:
    def __init__(self, repository: WorkStatusRepository | None = None) -> None:
        self.repository = repository or WorkStatusRepository()

    def _facility(
        self, context: CommunicationStaffContext, facility_uuid: UUID | None
    ) -> UUID:
        if facility_uuid is None:
            if not context.facility_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No facility context is available.",
                )
            return context.facility_ids[0]
        if facility_uuid not in context.facility_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Facility access is not permitted.",
            )
        return facility_uuid

    async def _facilities(
        self, db: AsyncSession, context: CommunicationStaffContext
    ) -> list[dict]:
        if not context.facility_ids:
            return []
        rows = await db.execute(
            select(Facility.id, Facility.name).where(Facility.id.in_(context.facility_ids))
        )
        return [{"uuid": str(row[0]), "name": row[1]} for row in rows.all()]

    def _serialize(self, facility_id: UUID, facility_name, row) -> dict:
        return {
            "facility": {"uuid": str(facility_id), "name": facility_name},
            "status": {
                "workState": row.work_state if row else "available",
                "duty": row.duty if row else None,
                "returnAt": (
                    row.return_at.isoformat() if row is not None and row.return_at else None
                ),
                "source": row.source if row else None,
                "updatedAt": (
                    row.updated_at.isoformat() if row is not None and row.updated_at else None
                ),
            },
            # connection is client/Ably presence, never server-authoritative here.
            "connection": None,
            "permitted": {"workStates": list(WORK_STATES), "duties": list(DUTY_STATES)},
        }

    async def get_status(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        *,
        facility_uuid: UUID | None = None,
    ) -> dict:
        facility_id = self._facility(context, facility_uuid)
        row = await self.repository.get(
            db,
            organization_id=context.organization_id,
            facility_id=facility_id,
            staff_member_id=context.staff_member_id,
        )
        facility_name = await db.scalar(
            select(Facility.name).where(Facility.id == facility_id)
        )
        result = self._serialize(facility_id, facility_name, row)
        result["facilities"] = await self._facilities(db, context)
        return result

    async def patch_status(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        payload: dict,
    ) -> dict:
        facility_id = self._facility(context, payload.get("facility_uuid"))
        work_state = payload.get("work_state")
        duty = payload.get("duty")
        if work_state not in WORK_STATES:
            raise HTTPException(status_code=422, detail="Unsupported work state.")
        if duty is not None and duty not in DUTY_STATES:
            raise HTTPException(status_code=422, detail="Unsupported duty state.")
        return_at = payload.get("return_at")
        if return_at is not None and return_at.tzinfo is None:
            return_at = return_at.replace(tzinfo=UTC)
        await self.repository.upsert(
            db,
            organization_id=context.organization_id,
            facility_id=facility_id,
            staff_member_id=context.staff_member_id,
            work_state=work_state,
            duty=duty,
            source=WORK_STATUS_SOURCE_STAFF,
            return_at=return_at,
        )
        await db.commit()
        return await self.get_status(db, context, facility_uuid=facility_id)

    async def apply_derived_status(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        facility_id: UUID,
        staff_member_id: UUID,
        work_state: str,
        return_at: datetime | None = None,
        now: datetime | None = None,
    ):
        """Consultation-derived update; preserves a live staff override.

        Does not commit: the owning workflow commits the unit of work.
        """
        now = now or datetime.now(UTC)
        existing = await self.repository.get(
            db,
            organization_id=organization_id,
            facility_id=facility_id,
            staff_member_id=staff_member_id,
        )
        if (
            existing is not None
            and existing.source == WORK_STATUS_SOURCE_STAFF
            and (existing.return_at is None or existing.return_at > now)
        ):
            return existing
        return await self.repository.upsert(
            db,
            organization_id=organization_id,
            facility_id=facility_id,
            staff_member_id=staff_member_id,
            work_state=work_state,
            duty=existing.duty if existing else None,
            source=WORK_STATUS_SOURCE_CONSULTATION,
            return_at=return_at,
        )


work_status_service = WorkStatusService()
