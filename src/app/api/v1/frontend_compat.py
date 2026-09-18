from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import local_datetime
from ...models.care import Appointment, PractitionerAvailabilityRule
from ...models.identity import UserAccount
from .scheduling import _facility_timezone, _scope

router = APIRouter(tags=["frontend-compat"])


@router.get("/scheduling/availability")
@router.get("/appointments/availability")
@router.get("/api/appointments/availability")
async def availability(facility_uuid: UUID, practitioner_uuid: UUID, target_date: date = Query(alias="date"), account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ..., duration_minutes: int = Query(default=30, ge=5, le=240), enforce_future: bool = False) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    tz = await _facility_timezone(db, facility_uuid)
    rules = (await db.scalars(select(PractitionerAvailabilityRule).where(PractitionerAvailabilityRule.organization_id == organization.id, PractitionerAvailabilityRule.facility_id == facility_uuid, PractitionerAvailabilityRule.practitioner_id == practitioner_uuid, PractitionerAvailabilityRule.weekday == target_date.weekday(), PractitionerAvailabilityRule.status == "active"))).all()
    start, end = time(8, 0), time(20, 0)
    if rules:
        start, end = min(r.start_time for r in rules), max(r.end_time for r in rules)
    appointments = (await db.scalars(select(Appointment).where(Appointment.facility_id == facility_uuid, Appointment.practitioner_id == practitioner_uuid, Appointment.status.in_(["booked", "confirmed", "checked_in", "in_consultation"]), Appointment.scheduled_start >= local_datetime(target_date, time.min, tz), Appointment.scheduled_start < local_datetime(target_date + timedelta(days=1), time.min, tz)))).all()
    slots = []
    current = local_datetime(target_date, start, tz)
    closing = local_datetime(target_date, end, tz)
    now = datetime.now(tz)
    while current + timedelta(minutes=duration_minutes) <= closing:
        slot_end = current + timedelta(minutes=duration_minutes)
        booked = any(current < a.scheduled_end and slot_end > a.scheduled_start for a in appointments)
        past = enforce_future and target_date == now.date() and current < now
        status = "PAST" if past else "BOOKED" if booked else "AVAILABLE"
        slots.append({"start": current.strftime("%H:%M"), "end": slot_end.strftime("%H:%M"), "startTime": current.strftime("%H:%M"), "endTime": slot_end.strftime("%H:%M"), "startIso": current.isoformat(), "endIso": slot_end.isoformat(), "status": status, "bookable": status == "AVAILABLE", "reason": "Existing Appointment" if booked else None, "blockType": "APPOINTMENT" if booked else None})
        current = slot_end
    next_slot = next((slot for slot in slots if slot["bookable"]), None)
    return {"success": True, "data": {"facilityId": str(facility_uuid), "facilityUuid": str(facility_uuid), "practitionerId": str(practitioner_uuid), "practitionerUuid": str(practitioner_uuid), "date": target_date.isoformat(), "timezone": tz.key, "durationMinutes": duration_minutes, "operatingHours": {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}, "nextAvailableSlot": next_slot, "slots": slots}, "meta": {}}
