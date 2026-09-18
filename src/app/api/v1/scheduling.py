from datetime import date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.appointment_views import appointment_view
from ...core.db.database import async_get_db
from ...core.timezones import (
    DEFAULT_TIMEZONE,
    local_datetime,
    normalize_range,
    timezone,
)
from ...models.care import (
    Appointment,
    Practitioner,
    PractitionerAvailabilityException,
    PractitionerAvailabilityRule,
)
from ...models.identity import Patient, Person, UserAccount
from ...models.organization import (
    Facility,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffMember,
)
from ...schemas.scheduling import (
    AppointmentCreate,
    AppointmentReschedule,
    AvailabilityExceptionInput,
    AvailabilityRuleInput,
)
from .bootstrap import ADMIN_ROLES, _staff_context

router = APIRouter(tags=["scheduling"])


async def _facility_timezone(db: AsyncSession, facility_uuid: UUID):
    name = await db.scalar(
        select(FacilitySchedule.timezone).where(
            FacilitySchedule.facility_id == facility_uuid
        )
    )
    return timezone(name or DEFAULT_TIMEZONE)


def _appointment_query(
    organization_id: UUID | None = None,
    staff_member_id: UUID | None = None,
    account_id: UUID | None = None,
):
    query = (
        select(Appointment, Patient, Person, Practitioner)
        .outerjoin(
            Patient,
            and_(
                Patient.id == Appointment.patient_id,
                Patient.organization_id == Appointment.organization_id,
            ),
        )
        .outerjoin(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Appointment.organization_id,
            ),
        )
        .outerjoin(
            Practitioner,
            and_(
                Practitioner.id == Appointment.practitioner_id,
                Practitioner.organization_id == Appointment.organization_id,
            ),
        )
    )
    if organization_id is not None:
        query = query.where(Appointment.organization_id == organization_id)
    if account_id is not None:
        query = query.where(
            exists(
                select(StaffAssignment.id)
                .join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id)
                .where(
                    StaffMember.user_account_id == account_id,
                    StaffMember.organization_id == Appointment.organization_id,
                    StaffMember.is_active.is_(True),
                    StaffAssignment.is_active.is_(True),
                    or_(
                        StaffAssignment.role_code.in_(ADMIN_ROLES),
                        StaffAssignment.facility_id == Appointment.facility_id,
                    ),
                )
            )
        )
    elif staff_member_id is not None:
        query = query.where(
            exists(
                select(StaffAssignment.id).where(
                    StaffAssignment.staff_member_id == staff_member_id,
                    StaffAssignment.is_active.is_(True),
                    or_(
                        StaffAssignment.role_code.in_(ADMIN_ROLES),
                        StaffAssignment.facility_id == Appointment.facility_id,
                    ),
                )
            )
        )
    return query


def _scope_query(account_id: UUID, facility_uuid: UUID | str | None = None):
    requested = facility_uuid is not None and str(facility_uuid).lower() != "all"
    try:
        target_id = UUID(str(facility_uuid)) if requested else None
    except (ValueError, TypeError):
        target_id = None

    query = (
        select(Organization, Facility)
        .select_from(StaffMember)
        .join(Organization, Organization.id == StaffMember.organization_id)
        .join(
            StaffAssignment,
            and_(
                StaffAssignment.staff_member_id == StaffMember.id,
                StaffAssignment.is_active.is_(True),
            ),
        )
        .join(
            Facility,
            and_(
                Facility.organization_id == Organization.id,
                Facility.is_active.is_(True),
                or_(
                    StaffAssignment.facility_id == Facility.id,
                    StaffAssignment.role_code.in_(ADMIN_ROLES),
                ),
            ),
        )
        .where(
            StaffMember.user_account_id == account_id,
            StaffMember.is_active.is_(True),
            Organization.is_active.is_(True),
        )
    )
    if not requested:
        return query
    if target_id is None:
        return query.where(Facility.id.is_(None))
    return query.where(Facility.id == target_id).order_by(Facility.created_at)


async def _scope(
    db: AsyncSession, account: UserAccount, facility_uuid: UUID | str | None = None
):
    row = (await db.execute(_scope_query(account.id, facility_uuid))).first()
    if row is not None:
        return row

    if facility_uuid is not None and str(facility_uuid).lower() != "all":
        raise HTTPException(status_code=403, detail="Facility access is not permitted.")

    # Preserve the existing error and no-facility behavior on exceptional paths.
    _, organization = await _staff_context(db, account)
    return organization, None


@router.get("/practitioners")
async def practitioners(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: str | None = None,
) -> dict[str, Any]:
    requested = facility_uuid is not None and str(facility_uuid).lower() != "all"
    facility = None
    query = select(Practitioner).where(Practitioner.is_active.is_(True))
    if requested:
        organization, facility = await _scope(db, account, facility_uuid)
        query = query.where(Practitioner.organization_id == organization.id)
    else:
        query = query.where(
            exists(
                select(StaffAssignment.id)
                .join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id)
                .where(
                    StaffMember.user_account_id == account.id,
                    StaffMember.organization_id == Practitioner.organization_id,
                    StaffMember.is_active.is_(True),
                    StaffAssignment.is_active.is_(True),
                )
            )
        )
    items = [
        {"uuid": str(p.id), "name": p.person_name, "specialty": p.specialty}
        for p in (await db.scalars(query)).all()
    ]
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"facility_uuid": str(facility.id) if facility else None},
    }


@router.get("/scheduling/availability-rules")
async def availability_rules(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: UUID,
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    rules = (
        await db.scalars(
            select(PractitionerAvailabilityRule).where(
                PractitionerAvailabilityRule.organization_id == organization.id,
                PractitionerAvailabilityRule.facility_id == facility_uuid,
                PractitionerAvailabilityRule.status == "active",
            )
        )
    ).all()
    items = [
        {
            "uuid": str(r.id),
            "practitioner_uuid": str(r.practitioner_id),
            "weekday": r.weekday,
            "start_time": r.start_time.isoformat(),
            "end_time": r.end_time.isoformat(),
            "slot_duration_minutes": r.slot_duration_minutes,
            "valid_from": r.valid_from.isoformat(),
            "valid_until": r.valid_until.isoformat() if r.valid_until else None,
        }
        for r in rules
    ]
    return {"success": True, "data": {"items": items}, "meta": {}}


@router.get("/scheduling/availability-exceptions")
async def availability_exceptions(
    facility_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    practitioner_uuid: UUID | None = None,
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    query = select(PractitionerAvailabilityException).where(
        PractitionerAvailabilityException.organization_id == organization.id,
        PractitionerAvailabilityException.facility_id == facility_uuid,
    )
    if practitioner_uuid:
        query = query.where(
            PractitionerAvailabilityException.practitioner_id == practitioner_uuid
        )
    values = (
        await db.scalars(
            query.order_by(PractitionerAvailabilityException.exception_date)
        )
    ).all()
    return {
        "success": True,
        "data": {
            "items": [
                {
                    "uuid": str(v.id),
                    "practitioner_uuid": str(v.practitioner_id),
                    "exception_date": v.exception_date.isoformat(),
                    "start_time": v.start_time.isoformat() if v.start_time else None,
                    "end_time": v.end_time.isoformat() if v.end_time else None,
                    "exception_type": v.exception_type,
                    "reason": v.reason,
                }
                for v in values
            ]
        },
        "meta": {},
    }


@router.post("/scheduling/availability-exceptions", status_code=status.HTTP_201_CREATED)
async def create_availability_exception(
    payload: AvailabilityExceptionInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    value = PractitionerAvailabilityException(
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        exception_date=payload.exception_date,
        start_time=payload.start_time,
        end_time=payload.end_time,
        exception_type=payload.exception_type,
        reason=payload.reason,
    )
    db.add(value)
    await db.commit()
    return {"success": True, "data": {"uuid": str(value.id)}, "meta": {}}


@router.delete("/scheduling/availability-exceptions/{exception_uuid}")
async def delete_availability_exception(
    exception_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    value = await db.get(PractitionerAvailabilityException, exception_uuid)
    if value is None:
        raise HTTPException(status_code=404, detail="Availability exception not found.")
    await _scope(db, account, value.facility_id)
    await db.delete(value)
    await db.commit()
    return {"success": True, "data": {}, "meta": {}}


@router.put("/scheduling/availability-rules")
async def replace_availability_rule(
    payload: AvailabilityRuleInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    practitioner = await db.scalar(
        select(Practitioner).where(
            Practitioner.id == payload.practitioner_uuid,
            Practitioner.organization_id == organization.id,
            Practitioner.is_active.is_(True),
        )
    )
    if practitioner is None:
        raise HTTPException(status_code=404, detail="Practitioner not found.")
    rule = PractitionerAvailabilityRule(
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        weekday=payload.weekday,
        start_time=payload.start_time,
        end_time=payload.end_time,
        slot_duration_minutes=payload.slot_duration_minutes,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
    )
    db.add(rule)
    await db.commit()
    return {"success": True, "data": {"uuid": str(rule.id)}, "meta": {}}


@router.get("/scheduling/next-slots")
async def next_slots(
    facility_uuid: UUID,
    practitioner_uuid: UUID,
    from_date: date | None = Query(default=None),
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    tz = await _facility_timezone(db, facility_uuid)
    from_date = from_date or datetime.now(tz).date()
    rules = (
        await db.scalars(
            select(PractitionerAvailabilityRule).where(
                PractitionerAvailabilityRule.organization_id == organization.id,
                PractitionerAvailabilityRule.facility_id == facility_uuid,
                PractitionerAvailabilityRule.practitioner_id == practitioner_uuid,
                PractitionerAvailabilityRule.status == "active",
            )
        )
    ).all()
    appointments = (
        await db.scalars(
            select(Appointment).where(
                Appointment.organization_id == organization.id,
                Appointment.facility_id == facility_uuid,
                Appointment.practitioner_id == practitioner_uuid,
                Appointment.status.in_(["booked", "checked_in", "in_consultation"]),
                Appointment.scheduled_start
                >= local_datetime(from_date, datetime.min.time(), tz),
            )
        )
    ).all()
    occupied = {(a.scheduled_start, a.scheduled_end) for a in appointments}
    slots: list[str] = []
    for offset in range(14):
        day = from_date + timedelta(days=offset)
        for rule in rules:
            if (
                day.weekday() != rule.weekday
                or day < rule.valid_from
                or (rule.valid_until and day > rule.valid_until)
            ):
                continue
            current = local_datetime(day, rule.start_time, tz)
            end = local_datetime(day, rule.end_time, tz)
            while (
                current + timedelta(minutes=rule.slot_duration_minutes) <= end
                and len(slots) < 20
            ):
                slot_end = current + timedelta(minutes=rule.slot_duration_minutes)
                if not any(
                    current < busy_end and slot_end > busy_start
                    for busy_start, busy_end in occupied
                ):
                    slots.append(current.isoformat())
                current = slot_end
    return {"success": True, "data": {"slots": slots}, "meta": {}}


@router.post("/appointments", status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: AppointmentCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    tz = await _facility_timezone(db, payload.facility_uuid)
    practitioner = await db.scalar(
        select(Practitioner).where(
            Practitioner.id == payload.practitioner_uuid,
            Practitioner.organization_id == organization.id,
            Practitioner.is_active.is_(True),
        )
    )
    patient = await db.scalar(
        select(Patient).where(
            Patient.id == payload.patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    if practitioner is None or patient is None:
        raise HTTPException(
            status_code=404, detail="Practitioner or patient not found."
        )
    try:
        scheduled_start, scheduled_end = normalize_range(
            payload.scheduled_start, payload.scheduled_end, tz
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    appointment = Appointment(
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        patient_id=payload.patient_uuid,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
        idempotency_key=payload.idempotency_key,
    )
    db.add(appointment)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="The appointment slot is no longer available."
        ) from None
    return {
        "success": True,
        "data": {
            "uuid": str(appointment.id),
            "status": appointment.status,
            "scheduled_start": appointment.scheduled_start.isoformat(),
            "scheduled_end": appointment.scheduled_end.isoformat(),
        },
        "meta": {},
    }


@router.get("/appointments")
async def list_appointments(
    facility_uuid: str | None = None,
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
    status_filter: str | None = Query(default=None, alias="status"),
    patient_uuid: UUID | None = None,
) -> dict[str, Any]:
    if facility_uuid and str(facility_uuid).lower() != "all":
        await _scope(db, account, facility_uuid)
    query = _appointment_query(account_id=account.id)
    if facility_uuid and str(facility_uuid).lower() != "all":
        query = query.where(Appointment.facility_id == UUID(str(facility_uuid)))
    if status_filter:
        query = query.where(Appointment.status == status_filter)
    if patient_uuid:
        query = query.where(Appointment.patient_id == patient_uuid)
    rows = (await db.execute(query.order_by(Appointment.scheduled_start))).all()
    return {
        "success": True,
        "data": {"items": [appointment_view(*row) for row in rows]},
        "meta": {"count": len(rows)},
    }


@router.post("/appointments/{appointment_uuid}/reschedule")
async def reschedule_appointment(
    appointment_uuid: UUID,
    payload: AppointmentReschedule,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    appointment = await db.scalar(
        select(Appointment).where(Appointment.id == appointment_uuid).with_for_update()
    )
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status not in {"booked", "checked_in"}:
        raise HTTPException(
            status_code=409, detail="Appointment cannot be rescheduled."
        )
    try:
        appointment.scheduled_start, appointment.scheduled_end = normalize_range(
            payload.scheduled_start,
            payload.scheduled_end,
            await _facility_timezone(db, appointment.facility_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="The new appointment slot is no longer available."
        ) from None
    return {"success": True, "data": appointment_view(appointment), "meta": {}}


@router.get("/appointments/{appointment_uuid}")
async def get_appointment(
    appointment_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    row = (
        await db.execute(
            _appointment_query(organization.id, staff.id).where(
                Appointment.id == appointment_uuid
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    return {"success": True, "data": appointment_view(*row), "meta": {}}


@router.post("/appointments/{appointment_uuid}/cancel")
async def cancel_appointment(
    appointment_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status in {"completed", "cancelled", "no_show"}:
        raise HTTPException(status_code=409, detail="Appointment cannot be cancelled.")
    appointment.status = "cancelled"
    await db.commit()
    return {"success": True, "data": appointment_view(appointment), "meta": {}}


@router.post("/appointments/{appointment_uuid}/no-show")
async def no_show_appointment(
    appointment_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status not in {"booked", "checked_in"}:
        raise HTTPException(
            status_code=409, detail="Appointment cannot be marked no-show."
        )
    appointment.status = "no_show"
    await db.commit()
    return {"success": True, "data": appointment_view(appointment), "meta": {}}
