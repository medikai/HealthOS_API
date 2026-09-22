from datetime import date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.appointment_views import appointment_view
from ...core.availability import (
    evaluate_availability,
    find_legacy_rule_conflicts,
    rules_from_facility_schedule,
    validate_interval,
)
from ...core.db.database import async_get_db
from ...core.events import make_event, publish
from ...core.timezones import (
    DEFAULT_TIMEZONE,
    normalize_range,
    timezone,
    to_timezone,
)
from ...domains.governance.audit import record_audit
from ...models.care import (
    Appointment,
    AppointmentBookingException,
    Encounter,
    Practitioner,
    PractitionerAvailabilityException,
    PractitionerAvailabilityRule,
    PractitionerSchedule,
)
from ...models.identity import Patient, Person, UserAccount
from ...models.masters import MedicalCouncil, Specialty, StaffDesignation, SubSpecialty
from ...models.organization import (
    Facility,
    FacilityResource,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffMember,
)
from ...schemas.masters import PractitionerCreate, PractitionerUpdate
from ...schemas.practitioner_schedule import (
    WEEKDAYS,
    DaySchedule,
    PractitionerScheduleInput,
    PractitionerScheduleResetInput,
    ScheduleDateException,
)
from ...schemas.scheduling import (
    AppointmentCreate,
    AppointmentReschedule,
    AvailabilityExceptionInput,
    AvailabilityRuleInput,
    AvailabilityRulesReplaceInput,
    AvailabilityRulesResetInput,
    ExceptionBookingCreate,
)
from .bootstrap import ADMIN_ROLES, _staff_context
from .practitioner_schedules import (
    reset_practitioner_schedule,
    update_practitioner_schedule,
)

router = APIRouter(tags=["scheduling"])
EXCEPTION_BOOKING_ROLES = {"administrator", "organization_admin", "owner"}


def _availability_http_error(exc: ValueError, *, prefix: str = "") -> HTTPException:
    code, separator, message = str(exc).partition("|")
    if not separator:
        code, message = "INVALID_INTERVAL", str(exc)
    return HTTPException(status_code=409 if code != "INVALID_INTERVAL" else 422, detail={"code": code, "message": f"{prefix}{message}", "details": [{"availability_status": code}]})


async def _require_exception_booking_permission(db: AsyncSession, account: UserAccount, organization_id: UUID) -> None:
    permitted = await db.scalar(select(StaffAssignment.id).join(StaffMember).where(
        StaffMember.user_account_id == account.id,
        StaffMember.organization_id == organization_id,
        StaffMember.is_active.is_(True),
        StaffAssignment.is_active.is_(True),
        StaffAssignment.role_code.in_(EXCEPTION_BOOKING_ROLES),
    ))
    if permitted is None:
        raise HTTPException(status_code=403, detail={"code": "EXCEPTION_BOOKING_PERMISSION_REQUIRED", "message": "Exception booking permission is required."})


async def _facility_timezone(db: AsyncSession, facility_uuid: UUID):
    name = await db.scalar(
        select(FacilitySchedule.timezone).where(
            FacilitySchedule.facility_id == facility_uuid
        )
    )
    return timezone(name or DEFAULT_TIMEZONE)


async def _canonical_practitioner_id(
    db: AsyncSession, organization_id: UUID, value: UUID
) -> UUID | None:
    return await db.scalar(
        select(Practitioner.id)
        .outerjoin(
            StaffMember,
            and_(
                StaffMember.user_account_id == Practitioner.user_account_id,
                StaffMember.organization_id == Practitioner.organization_id,
                StaffMember.is_active.is_(True),
            ),
        )
        .where(
            Practitioner.organization_id == organization_id,
            Practitioner.is_active.is_(True),
            or_(
                Practitioner.id == value,
                Practitioner.user_account_id == value,
                StaffMember.id == value,
            ),
        )
    )


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


async def _practitioner_payload(db: AsyncSession, p: Practitioner) -> dict[str, Any]:
    spec_name = None
    sub_spec_name = None
    desig_name = None
    council_name = None
    if p.specialty_id:
        s = await db.get(Specialty, p.specialty_id)
        if s:
            spec_name = s.name
    if p.sub_specialty_id:
        sub = await db.get(SubSpecialty, p.sub_specialty_id)
        if sub:
            sub_spec_name = sub.name
    if p.designation_id:
        d = await db.get(StaffDesignation, p.designation_id)
        if d:
            desig_name = d.name
    medical_council_id = getattr(p, "medical_council_id", None)
    if medical_council_id:
        council = await db.get(MedicalCouncil, medical_council_id)
        if council:
            council_name = council.name

    return {
        "uuid": str(p.id),
        "name": p.person_name,
        "specialty": p.specialty or spec_name,
        "specialty_id": str(p.specialty_id) if p.specialty_id else None,
        "specialty_name": spec_name,
        "sub_specialty_id": str(p.sub_specialty_id) if p.sub_specialty_id else None,
        "sub_specialty_name": sub_spec_name,
        "designation_id": str(p.designation_id) if p.designation_id else None,
        "designation_name": desig_name,
        "medical_council_id": str(medical_council_id) if medical_council_id else None,
        "medical_council_name": council_name,
        "medical_council_reg_no": p.medical_council_reg_no,
        "has_prescription_authority": p.has_prescription_authority,
        "prescription_authority_status": p.prescription_authority_status,
        "organization_uuid": str(p.organization_id),
        "is_active": getattr(p, "is_active", True),
    }


@router.get("/practitioners")
async def practitioners(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: str | None = None,
) -> dict[str, Any]:
    requested = facility_uuid is not None and str(facility_uuid).lower() != "all"
    facility = None
    query = (
        select(Practitioner, Specialty.name, SubSpecialty.name, StaffDesignation.name)
        .outerjoin(Specialty, Specialty.id == Practitioner.specialty_id)
        .outerjoin(SubSpecialty, SubSpecialty.id == Practitioner.sub_specialty_id)
        .outerjoin(StaffDesignation, StaffDesignation.id == Practitioner.designation_id)
        .where(Practitioner.is_active.is_(True))
    )
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
    rows = (await db.execute(query)).all()
    items = []
    for p, spec_name, sub_spec_name, desig_name in rows:
        items.append({
            "uuid": str(p.id),
            "name": p.person_name,
            "specialty": p.specialty or spec_name,
            "specialty_id": str(p.specialty_id) if p.specialty_id else None,
            "specialty_name": spec_name,
            "sub_specialty_id": str(p.sub_specialty_id) if p.sub_specialty_id else None,
            "sub_specialty_name": sub_spec_name,
            "designation_id": str(p.designation_id) if p.designation_id else None,
            "designation_name": desig_name,
            "medical_council_id": str(value) if (value := getattr(p, "medical_council_id", None)) else None,
            "medical_council_reg_no": p.medical_council_reg_no,
            "has_prescription_authority": p.has_prescription_authority,
            "prescription_authority_status": p.prescription_authority_status,
            "organization_uuid": str(p.organization_id),
            "is_active": getattr(p, "is_active", True),
        })
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items), "facility_uuid": str(facility.id) if facility else None},
    }


@router.post("/practitioners", status_code=status.HTTP_201_CREATED)
async def create_practitioner(
    payload: PractitionerCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    _, organization = await _staff_context(db, account)
    org_id = payload.organization_id or organization.id

    spec_name = payload.specialty
    if payload.specialty_id and not spec_name:
        spec = await db.get(Specialty, payload.specialty_id)
        if spec:
            spec_name = spec.name
    if payload.medical_council_id and await db.get(MedicalCouncil, payload.medical_council_id) is None:
        raise HTTPException(status_code=404, detail="Medical council not found.")

    facility_ids = set(payload.facility_ids)
    schedule_query = (
        select(FacilitySchedule)
        .join(Facility, Facility.id == FacilitySchedule.facility_id)
        .where(
            Facility.organization_id == org_id,
            Facility.is_active.is_(True),
        )
    )
    if facility_ids:
        schedule_query = schedule_query.where(Facility.id.in_(facility_ids))
    schedules = list((await db.scalars(schedule_query)).all())
    if facility_ids - {schedule.facility_id for schedule in schedules}:
        raise HTTPException(status_code=404, detail="Active facility schedule not found.")

    practitioner = Practitioner(
        organization_id=org_id,
        person_name=payload.person_name,
        specialty=spec_name,
        specialty_id=payload.specialty_id,
        sub_specialty_id=payload.sub_specialty_id,
        designation_id=payload.designation_id,
        medical_council_id=payload.medical_council_id,
        medical_council_reg_no=payload.medical_council_reg_no,
        has_prescription_authority=payload.has_prescription_authority,
        prescription_authority_status=payload.prescription_authority_status,
        user_account_id=payload.user_account_id,
        is_active=True,
    )
    db.add(practitioner)
    for schedule in schedules:
        for rule in rules_from_facility_schedule(schedule, org_id, schedule.facility_id, practitioner.id):
            db.add(rule)
    await record_audit(
        db,
        organization_id=org_id,
        actor_user_id=account.id,
        action="practitioner.created",
        resource_type="practitioner",
        resource_id=practitioner.id,
    )
    await db.commit()
    await db.refresh(practitioner)
    data = await _practitioner_payload(db, practitioner)
    return {"success": True, "data": data, "meta": {}}


@router.patch("/practitioners/{practitioner_uuid}")
async def update_practitioner(
    practitioner_uuid: UUID,
    payload: PractitionerUpdate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    _, organization = await _staff_context(db, account)
    practitioner = await db.scalar(
        select(Practitioner).where(
            Practitioner.id == practitioner_uuid,
            Practitioner.organization_id == organization.id,
        )
    )
    if practitioner is None:
        raise HTTPException(status_code=404, detail="Practitioner not found.")

    if payload.person_name is not None:
        practitioner.person_name = payload.person_name
    if payload.specialty_id is not None:
        practitioner.specialty_id = payload.specialty_id
        spec = await db.get(Specialty, payload.specialty_id)
        if spec:
            practitioner.specialty = spec.name
    elif payload.specialty is not None:
        practitioner.specialty = payload.specialty
    if payload.sub_specialty_id is not None:
        practitioner.sub_specialty_id = payload.sub_specialty_id
    if payload.designation_id is not None:
        practitioner.designation_id = payload.designation_id
    if payload.medical_council_id is not None:
        if await db.get(MedicalCouncil, payload.medical_council_id) is None:
            raise HTTPException(status_code=404, detail="Medical council not found.")
        practitioner.medical_council_id = payload.medical_council_id
    if payload.medical_council_reg_no is not None:
        practitioner.medical_council_reg_no = payload.medical_council_reg_no
    if payload.has_prescription_authority is not None:
        practitioner.has_prescription_authority = payload.has_prescription_authority
    if payload.prescription_authority_status is not None:
        practitioner.prescription_authority_status = payload.prescription_authority_status
    if payload.is_active is not None:
        practitioner.is_active = payload.is_active

    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="practitioner.updated",
        resource_type="practitioner",
        resource_id=practitioner.id,
    )
    await db.commit()
    await db.refresh(practitioner)
    data = await _practitioner_payload(db, practitioner)
    return {"success": True, "data": data, "meta": {}}



@router.get("/scheduling/availability-rules")
async def availability_rules(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: UUID,
    practitioner_uuid: UUID | None = None,
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    if practitioner_uuid is not None:
        practitioner_uuid = (
            await _canonical_practitioner_id(db, organization.id, practitioner_uuid)
            or practitioner_uuid
        )
        schedule = await db.scalar(
            select(PractitionerSchedule)
            .where(
                PractitionerSchedule.organization_id == organization.id,
                PractitionerSchedule.facility_id == facility_uuid,
                PractitionerSchedule.practitioner_id == practitioner_uuid,
                PractitionerSchedule.is_active.is_(True),
            )
            .order_by(PractitionerSchedule.created_at.desc())
        )
        if schedule is not None:
            items = [
                {
                    "uuid": f"{schedule.id}:{day['day_of_week']}",
                    "practitioner_uuid": str(schedule.practitioner_id),
                    "day_of_week": index + 1,
                    "weekday": index,
                    "start_local_time": day["start_time"],
                    "start_time": day["start_time"],
                    "end_local_time": day["end_time"],
                    "end_time": day["end_time"],
                    "slot_duration_minutes": schedule.slot_interval_minutes,
                    "effective_from": schedule.effective_from.isoformat(),
                    "valid_from": schedule.effective_from.isoformat(),
                    "effective_to": schedule.effective_to.isoformat() if schedule.effective_to else None,
                    "valid_until": schedule.effective_to.isoformat() if schedule.effective_to else None,
                }
                for index, name in enumerate(WEEKDAYS)
                if (day := next((value for value in schedule.weekly_hours if value.get("day_of_week") == name and value.get("is_working")), None))
            ]
            return {"success": True, "data": {"items": items}, "meta": {}}
    rules = (
        await db.scalars(
            select(PractitionerAvailabilityRule).where(
                PractitionerAvailabilityRule.organization_id == organization.id,
                PractitionerAvailabilityRule.facility_id == facility_uuid,
                *([PractitionerAvailabilityRule.practitioner_id == practitioner_uuid] if practitioner_uuid else []),
                PractitionerAvailabilityRule.status == "active",
            )
        )
    ).all()
    items = [
        {
            "uuid": str(r.id),
            "practitioner_uuid": str(r.practitioner_id),
            "day_of_week": r.weekday + 1,
            "weekday": r.weekday,
            "start_local_time": r.start_time.isoformat(timespec="minutes"),
            "start_time": r.start_time.isoformat(),
            "end_local_time": r.end_time.isoformat(timespec="minutes"),
            "end_time": r.end_time.isoformat(),
            "slot_duration_minutes": r.slot_duration_minutes,
            "effective_from": r.valid_from.isoformat(),
            "valid_from": r.valid_from.isoformat(),
            "effective_to": r.valid_until.isoformat() if r.valid_until else None,
            "valid_until": r.valid_until.isoformat() if r.valid_until else None,
            "resource_uuid": str(r.resource_id) if r.resource_id else None,
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
    if practitioner_uuid is not None:
        practitioner_uuid = (
            await _canonical_practitioner_id(db, organization.id, practitioner_uuid)
            or practitioner_uuid
        )
        schedule = await db.scalar(
            select(PractitionerSchedule)
            .where(
                PractitionerSchedule.organization_id == organization.id,
                PractitionerSchedule.facility_id == facility_uuid,
                PractitionerSchedule.practitioner_id == practitioner_uuid,
                PractitionerSchedule.is_active.is_(True),
            )
            .order_by(PractitionerSchedule.created_at.desc())
        )
        if schedule is not None:
            return {
                "success": True,
                "data": {
                    "items": [
                        {
                            "uuid": f"{schedule.id}:{index}",
                            "practitioner_uuid": str(schedule.practitioner_id),
                            "exception_date": value["date"],
                            "start_time": f"{value['date']}T{value.get('start_time') or '00:00:00'}",
                            "end_time": f"{value['date']}T{value.get('end_time') or '23:59:59'}",
                            "exception_type": value["exception_type"],
                            "reason": value.get("reason"),
                            "is_bookable": value["exception_type"] == "custom_hours",
                        }
                        for index, value in enumerate(schedule.date_exceptions)
                    ]
                },
                "meta": {},
            }
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
    practitioner_id = await db.scalar(select(Practitioner.id).where(
        Practitioner.id == payload.practitioner_uuid,
        Practitioner.organization_id == organization.id,
        Practitioner.is_active.is_(True),
    ).with_for_update())
    if practitioner_id is None:
        raise HTTPException(status_code=404, detail="Practitioner not found.")

    conflicts = await find_legacy_rule_conflicts(
        db,
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        effective_date=payload.exception_date,
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AVAILABILITY_EXCEPTION_CONFLICT",
                "message": "Adding this availability exception would conflict with existing future appointments. Please reschedule or resolve these appointments first.",
                "details": conflicts,
            },
        )

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
    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="practitioner_availability_exception.created",
        resource_type="practitioner_availability_exception",
        resource_id=value.id,
        facility_id=payload.facility_uuid,
    )
    await db.commit()
    await publish(make_event(
        "schedule.changed", entity_id=value.id, entity_version=None,
        organization_id=value.organization_id, facility_id=value.facility_id,
        practitioner_id=value.practitioner_id,
        new={"date": value.exception_date, "resource_id": None},
    ))
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
    organization, _ = await _scope(db, account, value.facility_id)
    await db.scalar(select(Practitioner.id).where(Practitioner.id == value.practitioner_id).with_for_update())

    conflicts = await find_legacy_rule_conflicts(
        db,
        organization_id=organization.id,
        facility_id=value.facility_id,
        practitioner_id=value.practitioner_id,
        effective_date=value.exception_date,
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AVAILABILITY_EXCEPTION_CONFLICT",
                "message": "Removing this availability exception would expose conflicts with existing future appointments. Please review and resolve these appointments first.",
                "details": conflicts,
            },
        )

    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="practitioner_availability_exception.deleted",
        resource_type="practitioner_availability_exception",
        resource_id=exception_uuid,
        facility_id=value.facility_id,
    )
    old_date, organization_id, facility_id, practitioner_id = value.exception_date, value.organization_id, value.facility_id, value.practitioner_id
    await db.delete(value)
    await db.commit()
    await publish(make_event(
        "schedule.changed", entity_id=exception_uuid, entity_version=None,
        organization_id=organization_id, facility_id=facility_id,
        practitioner_id=practitioner_id,
        old={"date": old_date, "resource_id": None},
        new={"date": old_date, "resource_id": None},
    ))
    return {"success": True, "data": {}, "meta": {}}


@router.put("/scheduling/availability-rules")
async def replace_availability_rule(
    payload: AvailabilityRuleInput | AvailabilityRulesReplaceInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    if isinstance(payload, AvailabilityRulesReplaceInput):
        organization, _ = await _scope(db, account, payload.facility_uuid)
        practitioner_id = await _canonical_practitioner_id(
            db, organization.id, payload.practitioner_uuid
        )
        if practitioner_id is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "PRACTITIONER_NOT_FOUND",
                    "message": "Practitioner not found or not active in organization.",
                },
            )
        facility_tz = await _facility_timezone(db, payload.facility_uuid)
        slot_interval = payload.rules[0].slot_duration_minutes if payload.rules else 30
        schedule_payload = PractitionerScheduleInput(
            facility_uuid=payload.facility_uuid,
            timezone=str(facility_tz),
            slot_interval_minutes=slot_interval,
            effective_from=payload.effective_from,
            weekly_hours=[
                DaySchedule(
                    day_of_week=WEEKDAYS[rule.day_of_week - 1],
                    start_time=rule.start_local_time.isoformat(timespec="minutes"),
                    end_time=rule.end_local_time.isoformat(timespec="minutes"),
                )
                for rule in payload.rules
            ],
            date_exceptions=[
                ScheduleDateException(
                    date=value.start_time.date(),
                    exception_type=value.exception_type,
                    start_time=value.start_time.time().isoformat(timespec="minutes") if value.exception_type == "custom_hours" else None,
                    end_time=value.end_time.time().isoformat(timespec="minutes") if value.exception_type == "custom_hours" else None,
                    reason=value.reason,
                )
                for value in payload.exceptions
            ],
        )
        return await update_practitioner_schedule(
            practitioner_uuid=practitioner_id,
            payload=schedule_payload,
            account=account,
            db=db,
            facility_uuid=payload.facility_uuid,
            force=payload.force,
        )

    organization, _ = await _scope(db, account, payload.facility_uuid)
    # ponytail: facility row lock favors correctness; use per-schedule advisory locks if booking throughput becomes a bottleneck.
    await db.scalar(select(Facility.id).where(Facility.id == payload.facility_uuid).with_for_update())
    practitioner = await db.scalar(
        select(Practitioner).where(
            Practitioner.id == payload.practitioner_uuid,
            Practitioner.organization_id == organization.id,
            Practitioner.is_active.is_(True),
        ).with_for_update()
    )
    if practitioner is None:
        raise HTTPException(status_code=404, detail="Practitioner not found.")
    if payload.resource_uuid and await db.scalar(select(FacilityResource.id).where(
        FacilityResource.id == payload.resource_uuid,
        FacilityResource.facility_id == payload.facility_uuid,
        FacilityResource.is_active.is_(True),
    )) is None:
        raise HTTPException(status_code=404, detail="Facility room/resource not found.")

    conflicts = await find_legacy_rule_conflicts(
        db,
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        effective_date=payload.valid_from,
    )
    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "AVAILABILITY_RULE_CONFLICT",
                "message": "The proposed availability rule would conflict with existing future appointments. Please reschedule or resolve these appointments first.",
                "details": conflicts,
            },
        )

    existing_rule = await db.scalar(
        select(PractitionerAvailabilityRule).where(
            PractitionerAvailabilityRule.facility_id == payload.facility_uuid,
            PractitionerAvailabilityRule.practitioner_id == payload.practitioner_uuid,
            PractitionerAvailabilityRule.weekday == payload.weekday,
            PractitionerAvailabilityRule.start_time == payload.start_time,
        ).with_for_update()
    )
    if existing_rule:
        existing_rule.end_time = payload.end_time
        existing_rule.slot_duration_minutes = payload.slot_duration_minutes
        existing_rule.valid_from = payload.valid_from
        existing_rule.valid_until = payload.valid_until
        existing_rule.resource_id = payload.resource_uuid
        existing_rule.status = "active"
        rule = existing_rule
    else:
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
            resource_id=payload.resource_uuid,
        )
        db.add(rule)
    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="practitioner_availability_rule.updated",
        resource_type="practitioner_availability_rule",
        resource_id=rule.id,
        facility_id=payload.facility_uuid,
    )
    await db.commit()
    await publish(make_event(
        "schedule.changed", entity_id=rule.id, entity_version=None,
        organization_id=rule.organization_id, facility_id=rule.facility_id,
        practitioner_id=rule.practitioner_id,
        new={"date": rule.valid_from, "resource_id": rule.resource_id},
    ))
    return {"success": True, "data": {"uuid": str(rule.id)}, "meta": {}}


@router.post("/scheduling/availability-rules/reset")
async def reset_availability_rules(
    payload: AvailabilityRulesResetInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    practitioner_id = await _canonical_practitioner_id(
        db, organization.id, payload.practitioner_uuid
    )
    if practitioner_id is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "PRACTITIONER_NOT_FOUND",
                "message": "Practitioner not found or not active in organization.",
            },
        )
    return await reset_practitioner_schedule(
        practitioner_uuid=practitioner_id,
        payload=PractitionerScheduleResetInput(
            effective_date=payload.effective_date
        ),
        account=account,
        db=db,
        facility_uuid=payload.facility_uuid,
    )


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
    slots: list[str] = []
    days: list[dict[str, Any]] = []
    for offset in range(14):
        day = from_date + timedelta(days=offset)
        result = await evaluate_availability(db, organization_id=organization.id, facility_id=facility_uuid, practitioner_id=practitioner_uuid, day=day)
        days.append({key: result[key] for key in ("date", "status", "reason")})
        slots.extend(slot["start"] for slot in result["usable_slots"][:20 - len(slots)])
    return {"success": True, "data": {"slots": slots, "availability": days}, "meta": {}}


@router.post("/appointments", status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: AppointmentCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    await db.scalar(select(Facility.id).where(Facility.id == payload.facility_uuid).with_for_update())
    tz = await _facility_timezone(db, payload.facility_uuid)
    practitioner = await db.scalar(
        select(Practitioner).where(
            Practitioner.id == payload.practitioner_uuid,
            Practitioner.organization_id == organization.id,
            Practitioner.is_active.is_(True),
        ).with_for_update()
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
    try:
        resource_id = await validate_interval(
            db,
            organization_id=organization.id,
            facility_id=payload.facility_uuid,
            practitioner_id=payload.practitioner_uuid,
            start=scheduled_start,
            end=scheduled_end,
            resource_id=payload.resource_uuid,
        )
    except ValueError as exc:
        raise _availability_http_error(exc) from None
    appointment = Appointment(
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        patient_id=payload.patient_uuid,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        resource_id=resource_id,
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
            status_code=409, detail={"code": "SLOT_UNAVAILABLE", "message": "The appointment or required room/resource is no longer available."}
        ) from None
    await publish(make_event(
        "appointment.created", entity_id=appointment.id, entity_version=appointment.version,
        organization_id=appointment.organization_id, facility_id=appointment.facility_id,
        practitioner_id=appointment.practitioner_id,
        new={"date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
    ))
    return {
        "success": True,
        "data": {
            "uuid": str(appointment.id),
            "status": appointment.status,
            "scheduled_start": appointment.scheduled_start.isoformat(),
            "scheduled_end": appointment.scheduled_end.isoformat(),
            "resource_uuid": str(appointment.resource_id) if appointment.resource_id else None,
        },
        "meta": {},
    }


@router.post("/appointments/exception-bookings", status_code=status.HTTP_201_CREATED)
async def create_exception_booking(
    payload: ExceptionBookingCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    await _require_exception_booking_permission(db, account, organization.id)
    await db.scalar(select(Facility.id).where(Facility.id == payload.facility_uuid).with_for_update())
    practitioner = await db.scalar(select(Practitioner).where(
        Practitioner.id == payload.practitioner_uuid,
        Practitioner.organization_id == organization.id,
        Practitioner.is_active.is_(True),
    ).with_for_update())
    patient = await db.scalar(select(Patient).where(
        Patient.id == payload.patient_uuid,
        Patient.organization_id == organization.id,
        Patient.is_active.is_(True),
    ))
    if practitioner is None or patient is None:
        raise HTTPException(status_code=404, detail="Practitioner or patient not found.")
    facility_tz = await _facility_timezone(db, payload.facility_uuid)
    try:
        scheduled_start, scheduled_end = normalize_range(payload.scheduled_start, payload.scheduled_end, facility_tz)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if to_timezone(scheduled_start, facility_tz).date() < datetime.now(facility_tz).date():
        raise HTTPException(status_code=422, detail={"code": "PAST_INTERVAL", "message": "Exception bookings cannot be made for past dates."})

    ordinary_failure = None
    try:
        await validate_interval(
            db,
            organization_id=organization.id,
            facility_id=payload.facility_uuid,
            practitioner_id=payload.practitioner_uuid,
            start=scheduled_start,
            end=scheduled_end,
            resource_id=payload.resource_uuid,
        )
    except ValueError as exc:
        ordinary_failure = str(exc).partition("|")[0]
    if ordinary_failure is None:
        raise HTTPException(status_code=409, detail={"code": "EXCEPTION_NOT_REQUIRED", "message": "The requested interval is ordinarily bookable; use POST /appointments."})
    required_override = {
        "FACILITY_CLOSED": "facility_closed",
        "DOCTOR_UNAVAILABLE": "practitioner_off_hours",
        "MISSING_CONFIGURATION": "practitioner_off_hours",
    }.get(ordinary_failure)
    if required_override not in payload.override_types:
        raise HTTPException(status_code=409, detail={"code": ordinary_failure, "message": "The requested exception does not authorize the availability restriction."})
    try:
        resource_id = await validate_interval(
            db,
            organization_id=organization.id,
            facility_id=payload.facility_uuid,
            practitioner_id=payload.practitioner_uuid,
            start=scheduled_start,
            end=scheduled_end,
            resource_id=payload.resource_uuid,
            allowed_overrides=set(payload.override_types),
        )
    except ValueError as exc:
        raise _availability_http_error(exc) from None

    appointment = Appointment(
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        patient_id=payload.patient_uuid,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        resource_id=resource_id,
        reason_code=payload.reason_code,
        reason_text=payload.reason_text or payload.reason,
        idempotency_key=payload.idempotency_key,
    )
    db.add(appointment)
    booking_exception = AppointmentBookingException(
        appointment_id=appointment.id,
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        practitioner_id=payload.practitioner_uuid,
        patient_id=payload.patient_uuid,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        override_types=list(dict.fromkeys(payload.override_types)),
        reason=payload.reason,
        doctor_agreement_recorded=payload.doctor_agreement_recorded,
        actor_user_id=account.id,
    )
    db.add(booking_exception)
    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="appointment.exception_booked",
        resource_type="appointment",
        resource_id=appointment.id,
        facility_id=payload.facility_uuid,
        patient_id=payload.patient_uuid,
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "SLOT_UNAVAILABLE", "message": "The appointment or required room/resource is no longer available."}) from None
    await publish(make_event(
        "appointment.created", entity_id=appointment.id, entity_version=appointment.version,
        organization_id=appointment.organization_id, facility_id=appointment.facility_id,
        practitioner_id=appointment.practitioner_id,
        new={"date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
    ))
    return {"success": True, "data": {"uuid": str(appointment.id), "status": appointment.status, "scheduled_start": appointment.scheduled_start.isoformat(), "scheduled_end": appointment.scheduled_end.isoformat(), "resource_uuid": str(appointment.resource_id) if appointment.resource_id else None, "exception": {"uuid": str(booking_exception.id), "override_types": list(dict.fromkeys(payload.override_types)), "reason": payload.reason, "doctor_agreement_recorded": True, "actor_user_uuid": str(account.id), "recorded_at": booking_exception.created_at.isoformat()}}, "meta": {}}


@router.get("/appointments")
async def list_appointments(
    facility_uuid: str | None = None,
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
    status_filter: str | None = Query(default=None, alias="status"),
    patient_uuid: UUID | None = None,
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
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
    order_val = order if isinstance(order, str) else getattr(order, "default", "asc")
    order_clause = (
        Appointment.scheduled_start.desc()
        if str(order_val).lower() == "desc"
        else Appointment.scheduled_start.asc()
    )
    rows = (await db.execute(query.order_by(order_clause))).all()
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
    await db.scalar(select(Facility.id).where(Facility.id == appointment.facility_id).with_for_update())
    if appointment.status not in {"booked", "confirmed"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "APPOINTMENT_STATUS_NOT_RESCHEDULABLE", "message": "Only booked or confirmed appointments can be rescheduled.", "details": [{"status": appointment.status}]},
        )
    if payload.version != appointment.version:
        raise HTTPException(
            status_code=409,
            detail={"code": "STALE_VERSION", "message": "The appointment was modified by another transaction. Please reload and try again.", "details": [{"current_version": appointment.version, "submitted_version": payload.version}]},
        )
    old_range = {
        "start": appointment.scheduled_start.isoformat(),
        "end": appointment.scheduled_end.isoformat(),
        "resource_uuid": str(appointment.resource_id) if appointment.resource_id else None,
    }
    try:
        scheduled_start, scheduled_end = normalize_range(
            payload.scheduled_start,
            payload.scheduled_end,
            await _facility_timezone(db, appointment.facility_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await db.scalar(select(Practitioner.id).where(Practitioner.id == appointment.practitioner_id).with_for_update())
    try:
        resource_id = await validate_interval(
            db,
            organization_id=appointment.organization_id,
            facility_id=appointment.facility_id,
            practitioner_id=appointment.practitioner_id,
            start=scheduled_start,
            end=scheduled_end,
            resource_id=payload.resource_uuid,
            ignore_appointment_id=appointment.id,
        )
    except ValueError as exc:
        raise _availability_http_error(exc, prefix="The new slot is unavailable: ") from None
    appointment.scheduled_start, appointment.scheduled_end, appointment.resource_id = scheduled_start, scheduled_end, resource_id
    appointment.version += 1
    new_range = {
        "start": scheduled_start.isoformat(),
        "end": scheduled_end.isoformat(),
        "resource_uuid": str(resource_id) if resource_id else None,
    }
    await record_audit(
        db,
        organization_id=appointment.organization_id,
        actor_user_id=account.id,
        action="appointment.rescheduled",
        resource_type="appointment",
        resource_id=appointment.id,
        facility_id=appointment.facility_id,
        patient_id=appointment.patient_id,
        details={"old": old_range, "new": new_range, "reason": payload.reason, "version": appointment.version},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail={"code": "SLOT_UNAVAILABLE", "message": "The new appointment slot or required room/resource is no longer available."}
        ) from None
    await publish(make_event(
        "appointment.rescheduled", entity_id=appointment.id, entity_version=appointment.version,
        organization_id=appointment.organization_id, facility_id=appointment.facility_id,
        practitioner_id=appointment.practitioner_id,
        old={"date": old_range["start"][:10], "resource_id": old_range["resource_uuid"]},
        new={"date": new_range["start"][:10], "resource_id": new_range["resource_uuid"]},
    ))
    return {"success": True, "data": appointment_view(appointment), "meta": {"affected_ranges": {"old": old_range, "new": new_range}}}


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
    old_status = appointment.status
    appointment.status = "cancelled"
    appointment.version += 1
    await db.commit()
    await publish(make_event(
        "appointment.cancelled", entity_id=appointment.id, entity_version=appointment.version,
        organization_id=appointment.organization_id, facility_id=appointment.facility_id,
        practitioner_id=appointment.practitioner_id,
        old={"status": old_status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
        new={"status": appointment.status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
    ))
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
    old_status = appointment.status
    appointment.status = "no_show"
    appointment.version += 1
    await db.commit()
    await publish(make_event(
        "appointment.status_changed", entity_id=appointment.id, entity_version=appointment.version,
        organization_id=appointment.organization_id, facility_id=appointment.facility_id,
        practitioner_id=appointment.practitioner_id,
        old={"status": old_status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
        new={"status": appointment.status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
    ))
    return {"success": True, "data": appointment_view(appointment), "meta": {}}
