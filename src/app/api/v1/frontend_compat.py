from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.availability import evaluate_availability
from ...core.db.database import async_get_db
from ...models.identity import UserAccount
from .scheduling import _scope

router = APIRouter(tags=["frontend-compat"])


@router.get("/scheduling/availability")
@router.get("/appointments/availability")
@router.get("/api/appointments/availability")
async def availability(facility_uuid: UUID, practitioner_uuid: UUID, target_date: date = Query(alias="date"), account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ..., duration_minutes: int = Query(default=30, ge=5, le=240), enforce_future: bool = False) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    result = await evaluate_availability(db, organization_id=organization.id, facility_id=facility_uuid, practitioner_id=practitioner_uuid, day=target_date, duration_minutes=duration_minutes, enforce_future=enforce_future)
    slots = []
    for value in result["slots"]:
        start, end = value["start"][11:16], value["end"][11:16]
        slots.append({"start": start, "end": end, "startTime": start, "endTime": end, "startIso": value["start"], "endIso": value["end"], "state": value["state"], "status": value["status"], "bookable": value["bookable"], "reason": value["reason"], "blockType": value["status"] if not value["bookable"] else None, "resourceUuid": value["resource_uuid"]})
    next_slot = next((slot for slot in slots if slot["bookable"]), None)
    return {
        "success": True,
        "data": {
            "facilityId": str(facility_uuid),
            "facilityUuid": str(facility_uuid),
            "facility_uuid": str(facility_uuid),
            "practitionerId": str(practitioner_uuid),
            "practitionerUuid": str(practitioner_uuid),
            "practitioner_uuid": str(practitioner_uuid),
            "date": target_date.isoformat(),
            "timezone": result["timezone"],
            "durationMinutes": duration_minutes,
            "duration_minutes": duration_minutes,
            "operatingHours": result["operating_hours"],
            "operating_hours": result["operating_hours"],
            "status": result["status"],
            "reason": result["reason"],
            "date_state": result["status"],
            "availabilityStatus": result["status"],
            "availabilityReason": result["reason"],
            "isOpen": result["facility_open"] is True,
            "isClosed": result["status"] == "FACILITY_CLOSED",
            "nextAvailableSlot": next_slot,
            "next_available_slot": next_slot,
            "usable_slots": result["usable_slots"],
            "slots": slots,
        },
        "meta": {},
    }
