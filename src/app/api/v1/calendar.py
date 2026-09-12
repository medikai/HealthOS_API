from datetime import datetime
from typing import Annotated, Any
from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import Appointment, Practitioner
from ...models.identity import UserAccount
from .scheduling import _scope

router = APIRouter(tags=["scheduling"])

@router.get("/practitioners/{practitioner_uuid}")
async def practitioner_detail(practitioner_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    practitioner = await db.get(Practitioner, practitioner_uuid)
    if practitioner is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Practitioner not found.")
    await _scope(db, account, None)
    return {"success": True, "data": {"uuid": str(practitioner.id), "name": practitioner.person_name, "specialty": practitioner.specialty, "organization_uuid": str(practitioner.organization_id)}, "meta": {}}

@router.get("/scheduling/calendar")
async def calendar(facility_uuid: UUID | None = None, from_datetime: datetime = Query(alias="from"), to_datetime: datetime = Query(alias="to"), account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ..., facility_uuids: str | None = None, practitioner_uuids: str | None = None) -> dict[str, Any]:
    if facility_uuid is None and facility_uuids:
        facility_uuid = UUID(facility_uuids.split(",")[0])
    if facility_uuid is None:
        raise ValueError("facility_uuid or facility_uuids is required")
    await _scope(db, account, facility_uuid)
    values = (await db.scalars(select(Appointment).where(Appointment.facility_id == facility_uuid, Appointment.scheduled_start >= from_datetime, Appointment.scheduled_start < to_datetime).order_by(Appointment.scheduled_start))).all()
    return {"success": True, "data": {"facility_uuids": [str(facility_uuid)], "facility_uuid": str(facility_uuid), "timezone": "Asia/Kolkata", "facility_schedule": {"operating_start": "08:00", "operating_end": "20:00", "slot_interval_minutes": 30, "days_of_week": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]}, "protected_periods": [], "from": from_datetime.isoformat(), "to": to_datetime.isoformat(), "items": [{"event_type": "appointment", "uuid": str(a.id), "facility_uuid": str(a.facility_id), "patient_uuid": str(a.patient_id), "practitioner_uuid": str(a.practitioner_id), "title": f"Appointment - {a.status}", "start": a.scheduled_start.isoformat(), "end": a.scheduled_end.isoformat(), "status": "confirmed" if a.status == "booked" else a.status} for a in values]}, "meta": {}}
