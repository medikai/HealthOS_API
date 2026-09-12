from datetime import date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import Appointment, Practitioner, PractitionerAvailabilityRule, PractitionerAvailabilityException
from ...models.identity import Patient, UserAccount
from ...models.organization import Facility, StaffAssignment
from ...schemas.scheduling import AppointmentCreate, AvailabilityRuleInput, AvailabilityExceptionInput, AppointmentReschedule
from .bootstrap import _staff_context

router = APIRouter(tags=["scheduling"])


async def _scope(db: AsyncSession, account: UserAccount, facility_uuid: UUID | None = None):
    staff, organization = await _staff_context(db, account)
    query = select(Facility).join(StaffAssignment, StaffAssignment.facility_id == Facility.id).where(StaffAssignment.staff_member_id == staff.id, StaffAssignment.is_active.is_(True), Facility.is_active.is_(True))
    if facility_uuid:
        query = query.where(Facility.id == facility_uuid)
    facility = (await db.scalars(query)).first()
    if facility_uuid and facility is None:
        raise HTTPException(status_code=403, detail="Facility access is not permitted.")
    return organization, facility


@router.get("/practitioners")
async def practitioners(account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], facility_uuid: UUID | None = None) -> dict[str, Any]:
    organization, facility = await _scope(db, account, facility_uuid)
    query = select(Practitioner).where(Practitioner.organization_id == organization.id, Practitioner.is_active.is_(True))
    items = [{"uuid": str(p.id), "name": p.person_name, "specialty": p.specialty} for p in (await db.scalars(query)).all()]
    return {"success": True, "data": {"items": items}, "meta": {"facility_uuid": str(facility.id) if facility else None}}


@router.get("/scheduling/availability-rules")
async def availability_rules(account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], facility_uuid: UUID) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    rules = (await db.scalars(select(PractitionerAvailabilityRule).where(PractitionerAvailabilityRule.organization_id == organization.id, PractitionerAvailabilityRule.facility_id == facility_uuid, PractitionerAvailabilityRule.status == "active"))).all()
    items = [{"uuid": str(r.id), "practitioner_uuid": str(r.practitioner_id), "weekday": r.weekday, "start_time": r.start_time.isoformat(), "end_time": r.end_time.isoformat(), "slot_duration_minutes": r.slot_duration_minutes, "valid_from": r.valid_from.isoformat(), "valid_until": r.valid_until.isoformat() if r.valid_until else None} for r in rules]
    return {"success": True, "data": {"items": items}, "meta": {}}


@router.get("/scheduling/availability-exceptions")
async def availability_exceptions(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], practitioner_uuid: UUID | None = None) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    query = select(PractitionerAvailabilityException).where(PractitionerAvailabilityException.organization_id == organization.id, PractitionerAvailabilityException.facility_id == facility_uuid)
    if practitioner_uuid:
        query = query.where(PractitionerAvailabilityException.practitioner_id == practitioner_uuid)
    values = (await db.scalars(query.order_by(PractitionerAvailabilityException.exception_date))).all()
    return {"success": True, "data": {"items": [{"uuid": str(v.id), "practitioner_uuid": str(v.practitioner_id), "exception_date": v.exception_date.isoformat(), "start_time": v.start_time.isoformat() if v.start_time else None, "end_time": v.end_time.isoformat() if v.end_time else None, "exception_type": v.exception_type, "reason": v.reason} for v in values]}, "meta": {}}


@router.post("/scheduling/availability-exceptions", status_code=status.HTTP_201_CREATED)
async def create_availability_exception(payload: AvailabilityExceptionInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    value = PractitionerAvailabilityException(organization_id=organization.id, facility_id=payload.facility_uuid, practitioner_id=payload.practitioner_uuid, exception_date=payload.exception_date, start_time=payload.start_time, end_time=payload.end_time, exception_type=payload.exception_type, reason=payload.reason)
    db.add(value)
    await db.commit()
    return {"success": True, "data": {"uuid": str(value.id)}, "meta": {}}


@router.delete("/scheduling/availability-exceptions/{exception_uuid}")
async def delete_availability_exception(exception_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    value = await db.get(PractitionerAvailabilityException, exception_uuid)
    if value is None:
        raise HTTPException(status_code=404, detail="Availability exception not found.")
    await _scope(db, account, value.facility_id)
    await db.delete(value)
    await db.commit()
    return {"success": True, "data": {}, "meta": {}}


@router.put("/scheduling/availability-rules")
async def replace_availability_rule(payload: AvailabilityRuleInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    practitioner = await db.scalar(select(Practitioner).where(Practitioner.id == payload.practitioner_uuid, Practitioner.organization_id == organization.id, Practitioner.is_active.is_(True)))
    if practitioner is None:
        raise HTTPException(status_code=404, detail="Practitioner not found.")
    rule = PractitionerAvailabilityRule(organization_id=organization.id, facility_id=payload.facility_uuid, practitioner_id=payload.practitioner_uuid, weekday=payload.weekday, start_time=payload.start_time, end_time=payload.end_time, slot_duration_minutes=payload.slot_duration_minutes, valid_from=payload.valid_from, valid_until=payload.valid_until)
    db.add(rule)
    await db.commit()
    return {"success": True, "data": {"uuid": str(rule.id)}, "meta": {}}


@router.get("/scheduling/next-slots")
async def next_slots(facility_uuid: UUID, practitioner_uuid: UUID, from_date: date = Query(default_factory=date.today), account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ...) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    rules = (await db.scalars(select(PractitionerAvailabilityRule).where(PractitionerAvailabilityRule.organization_id == organization.id, PractitionerAvailabilityRule.facility_id == facility_uuid, PractitionerAvailabilityRule.practitioner_id == practitioner_uuid, PractitionerAvailabilityRule.status == "active"))).all()
    appointments = (await db.scalars(select(Appointment).where(Appointment.organization_id == organization.id, Appointment.facility_id == facility_uuid, Appointment.practitioner_id == practitioner_uuid, Appointment.status.in_(["booked", "checked_in", "in_consultation"]), Appointment.scheduled_start >= datetime.combine(from_date, datetime.min.time())))).all()
    occupied = {(a.scheduled_start, a.scheduled_end) for a in appointments}
    slots: list[str] = []
    for offset in range(14):
        day = from_date + timedelta(days=offset)
        for rule in rules:
            if day.weekday() != rule.weekday or day < rule.valid_from or (rule.valid_until and day > rule.valid_until):
                continue
            current = datetime.combine(day, rule.start_time)
            end = datetime.combine(day, rule.end_time)
            while current + timedelta(minutes=rule.slot_duration_minutes) <= end and len(slots) < 20:
                slot_end = current + timedelta(minutes=rule.slot_duration_minutes)
                if not any(current < busy_end and slot_end > busy_start for busy_start, busy_end in occupied):
                    slots.append(current.isoformat())
                current = slot_end
    return {"success": True, "data": {"slots": slots}, "meta": {}}


@router.post("/appointments", status_code=status.HTTP_201_CREATED)
async def create_appointment(payload: AppointmentCreate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    practitioner = await db.scalar(select(Practitioner).where(Practitioner.id == payload.practitioner_uuid, Practitioner.organization_id == organization.id, Practitioner.is_active.is_(True)))
    patient = await db.scalar(select(Patient).where(Patient.id == payload.patient_uuid, Patient.organization_id == organization.id, Patient.is_active.is_(True)))
    if practitioner is None or patient is None:
        raise HTTPException(status_code=404, detail="Practitioner or patient not found.")
    appointment = Appointment(organization_id=organization.id, facility_id=payload.facility_uuid, practitioner_id=payload.practitioner_uuid, patient_id=payload.patient_uuid, scheduled_start=payload.scheduled_start, scheduled_end=payload.scheduled_end, reason_code=payload.reason_code, reason_text=payload.reason_text, idempotency_key=payload.idempotency_key)
    db.add(appointment)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="The appointment slot is no longer available.") from None
    return {"success": True, "data": {"uuid": str(appointment.id), "status": appointment.status, "scheduled_start": appointment.scheduled_start.isoformat(), "scheduled_end": appointment.scheduled_end.isoformat()}, "meta": {}}


def _appointment_item(a: Appointment) -> dict[str, Any]:
    return {"uuid": str(a.id), "facility_uuid": str(a.facility_id), "patient_uuid": str(a.patient_id), "practitioner_uuid": str(a.practitioner_id), "scheduled_start": a.scheduled_start.isoformat(), "scheduled_end": a.scheduled_end.isoformat(), "status": a.status, "reason_code": a.reason_code, "reason_text": a.reason_text}


@router.get("/appointments")
async def list_appointments(facility_uuid: UUID | None = None, account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ..., status_filter: str | None = Query(default=None, alias="status")) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    query = select(Appointment).where(Appointment.organization_id == organization.id)
    if facility_uuid:
        query = query.where(Appointment.facility_id == facility_uuid)
    if status_filter:
        query = query.where(Appointment.status == status_filter)
    values = (await db.scalars(query.order_by(Appointment.scheduled_start))).all()
    return {"success": True, "data": {"items": [_appointment_item(a) for a in values]}, "meta": {"count": len(values)}}


@router.post("/appointments/{appointment_uuid}/reschedule")
async def reschedule_appointment(appointment_uuid: UUID, payload: AppointmentReschedule, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    appointment = await db.scalar(select(Appointment).where(Appointment.id == appointment_uuid).with_for_update())
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status not in {"booked", "checked_in"}:
        raise HTTPException(status_code=409, detail="Appointment cannot be rescheduled.")
    appointment.scheduled_start, appointment.scheduled_end = payload.scheduled_start, payload.scheduled_end
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="The new appointment slot is no longer available.") from None
    return {"success": True, "data": _appointment_item(appointment), "meta": {}}


@router.get("/appointments/{appointment_uuid}")
async def get_appointment(appointment_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    return {"success": True, "data": _appointment_item(appointment), "meta": {}}


@router.post("/appointments/{appointment_uuid}/cancel")
async def cancel_appointment(appointment_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status in {"completed", "cancelled", "no_show"}:
        raise HTTPException(status_code=409, detail="Appointment cannot be cancelled.")
    appointment.status = "cancelled"
    await db.commit()
    return {"success": True, "data": _appointment_item(appointment), "meta": {}}


@router.post("/appointments/{appointment_uuid}/no-show")
async def no_show_appointment(appointment_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status not in {"booked", "checked_in"}:
        raise HTTPException(status_code=409, detail="Appointment cannot be marked no-show.")
    appointment.status = "no_show"
    await db.commit()
    return {"success": True, "data": _appointment_item(appointment), "meta": {}}
