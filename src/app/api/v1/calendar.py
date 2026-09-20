import json
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.appointment_views import appointment_view
from ...core.availability import evaluate_availability
from ...core.db.database import async_get_db
from ...core.timezones import DEFAULT_TIMEZONE, timezone, to_timezone, to_utc
from ...models.care import Appointment, AppointmentBookingException, Practitioner
from ...models.masters import Specialty, StaffDesignation, SubSpecialty
from ...models.identity import UserAccount
from ...models.organization import FacilitySchedule, ProtectedPeriod
from .scheduling import _appointment_query, _scope

router = APIRouter(tags=["scheduling"])

@router.get("/practitioners/{practitioner_uuid}")
async def practitioner_detail(practitioner_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    practitioner = await db.get(Practitioner, practitioner_uuid)
    if practitioner is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Practitioner not found.")
    await _scope(db, account, None)

    spec_name = None
    sub_spec_name = None
    desig_name = None
    if practitioner.specialty_id:
        s = await db.get(Specialty, practitioner.specialty_id)
        if s:
            spec_name = s.name
    if practitioner.sub_specialty_id:
        sub = await db.get(SubSpecialty, practitioner.sub_specialty_id)
        if sub:
            sub_spec_name = sub.name
    if practitioner.designation_id:
        d = await db.get(StaffDesignation, practitioner.designation_id)
        if d:
            desig_name = d.name

    return {
        "success": True,
        "data": {
            "uuid": str(practitioner.id),
            "name": practitioner.person_name,
            "specialty": practitioner.specialty or spec_name,
            "specialty_id": str(practitioner.specialty_id) if practitioner.specialty_id else None,
            "specialty_name": spec_name,
            "sub_specialty_id": str(practitioner.sub_specialty_id) if practitioner.sub_specialty_id else None,
            "sub_specialty_name": sub_spec_name,
            "designation_id": str(practitioner.designation_id) if practitioner.designation_id else None,
            "designation_name": desig_name,
            "medical_council_reg_no": practitioner.medical_council_reg_no,
            "has_prescription_authority": practitioner.has_prescription_authority,
            "prescription_authority_status": practitioner.prescription_authority_status,
            "organization_uuid": str(practitioner.organization_id),
            "is_active": getattr(practitioner, "is_active", True),
        },
        "meta": {},
    }


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
    exceptions_by_app_id = {}
    if rows:
        app_ids = [row[0].id for row in rows]
        exceptions = (
            await db.scalars(
                select(AppointmentBookingException).where(
                    AppointmentBookingException.appointment_id.in_(app_ids)
                )
            )
        ).all()
        for exc in exceptions:
            exceptions_by_app_id[exc.appointment_id] = exc
    items = []
    for row in rows:
        item = appointment_view(*row)
        exc = exceptions_by_app_id.get(row[0].id)
        item.update({
            "event_type": "appointment",
            "title": f"{item['patient']['display_name']} - {item['status']}" if item["patient"] else f"Appointment - {item['status']}",
            "start": to_timezone(row[0].scheduled_start, tz).isoformat(),
            "end": to_timezone(row[0].scheduled_end, tz).isoformat(),
            "status": "confirmed" if item["status"] == "booked" else item["status"],
            "is_exception": exc is not None,
            "override_types": exc.override_types if exc else [],
            "exception_reason": exc.reason if exc else None,
            "exception": {
                "uuid": str(exc.id),
                "override_types": exc.override_types,
                "reason": exc.reason,
                "doctor_agreement_recorded": exc.doctor_agreement_recorded,
                "actor_user_uuid": str(exc.actor_user_id),
                "recorded_at": exc.created_at.isoformat(),
            } if exc else None,
        })
        items.append(item)
    practitioner_ids = [UUID(value) for value in practitioner_uuids.split(",") if value] if practitioner_uuids else [None]
    availability = []
    day, final_day = to_timezone(from_utc, tz).date(), (to_timezone(to_utc_value, tz) - timedelta(microseconds=1)).date()
    while day <= final_day:
        for practitioner_id in practitioner_ids:
            value = await evaluate_availability(db, organization_id=organization.id, facility_id=facility.id, practitioner_id=practitioner_id, day=day)
            availability.append({"date": value["date"], "practitioner_uuid": str(practitioner_id) if practitioner_id else None, "status": value["status"], "reason": value["reason"], "usable_slots": value["usable_slots"]})
        day += timedelta(days=1)
    periods = (await db.scalars(select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility.id))).all()
    protected_periods = [{"uuid": str(period.id), "title": period.title, "start_time": period.start_time, "end_time": period.end_time, "period_type": period.period_type, "days_of_week": json.loads(period.days_of_week), "is_recurring": period.is_recurring} for period in periods]
    return {"success": True, "data": {"facility_uuids": [str(facility.id)], "facility_uuid": str(facility.id), "timezone": tz.key, "facility_schedule": schedule_data, "protected_periods": protected_periods, "availability": availability, "from": to_timezone(from_utc, tz).isoformat(), "to": to_timezone(to_utc_value, tz).isoformat(), "items": items}, "meta": {}}
