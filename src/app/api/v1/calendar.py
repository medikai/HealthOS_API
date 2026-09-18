import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.appointment_views import appointment_view
from ...core.db.database import async_get_db
from ...core.timezones import DEFAULT_TIMEZONE, timezone, to_timezone, to_utc
from ...models.care import Appointment, Practitioner
from ...models.identity import UserAccount
from ...models.organization import FacilitySchedule
from .scheduling import _appointment_query, _scope

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
async def calendar(facility_uuid: str | None = None, from_datetime: datetime = Query(alias="from"), to_datetime: datetime = Query(alias="to"), account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ..., facility_uuids: str | None = None, practitioner_uuids: str | None = None) -> dict[str, Any]:
    if facility_uuid is None and facility_uuids:
        facility_uuid = facility_uuids.split(",")[0]
    if facility_uuid is None:
        raise ValueError("facility_uuid or facility_uuids is required")
    organization, facility = await _scope(db, account, facility_uuid)
    schedule = await db.scalar(select(FacilitySchedule).where(FacilitySchedule.facility_id == facility.id))
    tz = timezone(schedule.timezone if schedule else DEFAULT_TIMEZONE)
    from_utc, to_utc_value = to_utc(from_datetime, tz), to_utc(to_datetime, tz)
    query = _appointment_query(organization.id).where(Appointment.facility_id == facility.id, Appointment.scheduled_start >= from_utc, Appointment.scheduled_start < to_utc_value)
    rows = (await db.execute(query.order_by(Appointment.scheduled_start))).all()
    schedule_data = {"operating_start": schedule.operating_start if schedule else "08:00", "operating_end": schedule.operating_end if schedule else "20:00", "slot_interval_minutes": schedule.slot_interval_minutes if schedule else 30, "days_of_week": json.loads(schedule.days_of_week) if schedule else ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"], "timezone": tz.key}
    items = []
    for row in rows:
        item = appointment_view(*row)
        item.update({"event_type": "appointment", "title": f"{item['patient']['display_name']} - {item['status']}" if item["patient"] else f"Appointment - {item['status']}", "start": to_timezone(row[0].scheduled_start, tz).isoformat(), "end": to_timezone(row[0].scheduled_end, tz).isoformat(), "status": "confirmed" if item["status"] == "booked" else item["status"]})
        items.append(item)
    return {"success": True, "data": {"facility_uuids": [str(facility.id)], "facility_uuid": str(facility.id), "timezone": tz.key, "facility_schedule": schedule_data, "protected_periods": [], "from": to_timezone(from_utc, tz).isoformat(), "to": to_timezone(to_utc_value, tz).isoformat(), "items": items}, "meta": {}}
