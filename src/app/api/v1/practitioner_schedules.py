import json
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.availability import find_schedule_conflicts
from ...core.db.database import async_get_db
from ...core.events import make_event, publish
from ...core.timezones import DEFAULT_TIMEZONE, timezone
from ...domains.governance.audit import record_audit
from ...models.care import Practitioner, PractitionerSchedule
from ...models.identity import UserAccount
from ...models.organization import (
    Facility,
    FacilitySchedule,
    StaffAssignment,
    StaffMember,
)
from ...schemas.practitioner_schedule import (
    WEEKDAYS,
    PractitionerScheduleInput,
    PractitionerScheduleResetInput,
    PractitionerScheduleResponse,
)
from .bootstrap import ADMIN_ROLES

router = APIRouter(tags=["practitioner-schedules"])


def _extract_facility_uuid(
    facility_uuid: UUID | None,
    payload_facility_uuid: UUID | None = None,
) -> UUID:
    target = facility_uuid or payload_facility_uuid
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "FACILITY_REQUIRED", "message": "facility_uuid is required."},
        )
    return target


async def _resolve_schedule_authorization(
    db: AsyncSession,
    account: UserAccount,
    target_practitioner_id: UUID,
    target_facility_id: UUID,
    *,
    write: bool = False,
) -> tuple[UUID, Facility, Practitioner]:
    # 1. Fetch user's staff membership
    staff_member = await db.scalar(
        select(StaffMember).where(
            StaffMember.user_account_id == account.id,
            StaffMember.is_active.is_(True),
        )
    )
    if staff_member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "User is not associated with any active organization."},
        )
    organization_id = staff_member.organization_id

    # 2. Check staff assignments and roles
    assignments = (
        await db.scalars(
            select(StaffAssignment).where(
                StaffAssignment.staff_member_id == staff_member.id,
                StaffAssignment.is_active.is_(True),
            )
        )
    ).all()
    roles = {a.role_code for a in assignments}
    is_admin = bool(roles.intersection(ADMIN_ROLES))

    # 3. Check facility existence within organization (lock on write)
    facility_stmt = select(Facility).where(
        Facility.id == target_facility_id,
        Facility.organization_id == organization_id,
        Facility.is_active.is_(True),
    )
    if write:
        facility_stmt = facility_stmt.with_for_update()
    facility = await db.scalar(facility_stmt)
    if facility is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "FACILITY_NOT_FOUND", "message": "Facility not found or not active in organization."},
        )

    # 4. Check practitioner existence within organization (lock on write)
    practitioner_stmt = select(Practitioner).where(
        Practitioner.id == target_practitioner_id,
        Practitioner.organization_id == organization_id,
        Practitioner.is_active.is_(True),
    )
    if write:
        practitioner_stmt = practitioner_stmt.with_for_update()
    practitioner = await db.scalar(practitioner_stmt)
    if practitioner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "PRACTITIONER_NOT_FOUND", "message": "Practitioner not found or not active in organization."},
        )

    # 5. Non-admin write permissions: doctors can only edit their own allowed schedule
    if write and not is_admin:
        if practitioner.user_account_id != account.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "Doctors are only permitted to manage their own schedule."},
            )

    # 6. Check facility assignment for non-admins
    if not is_admin:
        has_facility_access = any(
            a.facility_id is None or a.facility_id == target_facility_id
            for a in assignments
        )
        if not has_facility_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "User does not have access to this facility."},
            )

    return organization_id, facility, practitioner


def _build_inherited_schedule(
    practitioner: Practitioner,
    facility: Facility,
    facility_schedule: FacilitySchedule | None,
) -> dict[str, Any]:
    tz_str = facility_schedule.timezone if facility_schedule else DEFAULT_TIMEZONE
    operating_start = facility_schedule.operating_start if facility_schedule else "08:00"
    operating_end = facility_schedule.operating_end if facility_schedule else "20:00"
    slot_interval = facility_schedule.slot_interval_minutes if facility_schedule else 30
    
    if facility_schedule and facility_schedule.days_of_week:
        try:
            allowed_days = {d.lower() for d in json.loads(facility_schedule.days_of_week)}
        except Exception:
            allowed_days = set(WEEKDAYS)
    else:
        allowed_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday"}

    weekly_hours = [
        {
            "day_of_week": day,
            "is_working": day in allowed_days,
            "start_time": operating_start if day in allowed_days else None,
            "end_time": operating_end if day in allowed_days else None,
            "breaks": [],
        }
        for day in WEEKDAYS
    ]

    return {
        "practitioner_uuid": str(practitioner.id),
        "facility_uuid": str(facility.id),
        "organization_uuid": str(practitioner.organization_id),
        "timezone": tz_str,
        "slot_interval_minutes": slot_interval,
        "effective_from": None,
        "effective_to": None,
        "is_override": False,
        "inherited": True,
        "version": 0,
        "weekly_hours": weekly_hours,
        "date_exceptions": [],
        "updated_at": None,
    }


def _format_schedule_response(
    schedule: PractitionerSchedule,
    practitioner: Practitioner,
    facility: Facility,
) -> dict[str, Any]:
    return {
        "practitioner_uuid": str(practitioner.id),
        "facility_uuid": str(facility.id),
        "organization_uuid": str(practitioner.organization_id),
        "timezone": schedule.timezone,
        "slot_interval_minutes": schedule.slot_interval_minutes,
        "effective_from": schedule.effective_from.isoformat() if schedule.effective_from else None,
        "effective_to": schedule.effective_to.isoformat() if schedule.effective_to else None,
        "is_override": schedule.is_override,
        "inherited": not schedule.is_override,
        "version": schedule.version,
        "weekly_hours": schedule.weekly_hours,
        "date_exceptions": schedule.date_exceptions,
        "updated_at": schedule.updated_at.isoformat() if schedule.updated_at else None,
    }


@router.get(
    "/practitioners/{practitioner_uuid}/schedule",
    response_model=PractitionerScheduleResponse,
)
@router.get(
    "/facilities/{facility_uuid}/practitioners/{practitioner_uuid}/schedule",
    response_model=PractitionerScheduleResponse,
)
async def get_practitioner_schedule(
    practitioner_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: UUID | None = None,
) -> dict[str, Any]:
    target_facility_id = _extract_facility_uuid(facility_uuid)
    organization_id, facility, practitioner = await _resolve_schedule_authorization(
        db, account, practitioner_uuid, target_facility_id, write=False
    )

    # Check for active override
    active_override = await db.scalar(
        select(PractitionerSchedule)
        .where(
            PractitionerSchedule.organization_id == organization_id,
            PractitionerSchedule.facility_id == target_facility_id,
            PractitionerSchedule.practitioner_id == practitioner_uuid,
            PractitionerSchedule.is_active.is_(True),
        )
        .order_by(PractitionerSchedule.created_at.desc())
    )

    if active_override is not None:
        data = _format_schedule_response(active_override, practitioner, facility)
    else:
        facility_schedule = await db.scalar(
            select(FacilitySchedule).where(FacilitySchedule.facility_id == target_facility_id)
        )
        data = _build_inherited_schedule(practitioner, facility, facility_schedule)

    return {"success": True, "data": data, "meta": {}}


@router.put(
    "/practitioners/{practitioner_uuid}/schedule",
    response_model=PractitionerScheduleResponse,
)
@router.put(
    "/facilities/{facility_uuid}/practitioners/{practitioner_uuid}/schedule",
    response_model=PractitionerScheduleResponse,
)
async def update_practitioner_schedule(
    practitioner_uuid: UUID,
    payload: PractitionerScheduleInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: UUID | None = None,
    force: bool = False,
) -> dict[str, Any]:
    target_facility_id = _extract_facility_uuid(facility_uuid, payload.facility_uuid)
    organization_id, facility, practitioner = await _resolve_schedule_authorization(
        db, account, practitioner_uuid, target_facility_id, write=True
    )

    # Lock and query existing active override
    existing = await db.scalar(
        select(PractitionerSchedule)
        .where(
            PractitionerSchedule.organization_id == organization_id,
            PractitionerSchedule.facility_id == target_facility_id,
            PractitionerSchedule.practitioner_id == practitioner_uuid,
            PractitionerSchedule.is_active.is_(True),
        )
        .with_for_update()
    )

    weekly_hours_json = [day.model_dump(mode="json") for day in payload.weekly_hours]
    date_exceptions_json = [exc.model_dump(mode="json") for exc in payload.date_exceptions]

    facility_tz = timezone(payload.timezone)
    effective_from = payload.effective_from or datetime.now(facility_tz).date()

    if existing is not None:
        # Optimistic Concurrency Control Check
        if payload.version is not None and payload.version != existing.version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "STALE_VERSION",
                    "message": "The schedule was modified by another transaction. Please reload and try again.",
                    "details": [{"current_version": existing.version, "submitted_version": payload.version}],
                },
            )

    # Check for conflicts with existing future appointments
    conflicts = await find_schedule_conflicts(
        db,
        organization_id=organization_id,
        facility_id=target_facility_id,
        practitioner_id=practitioner_uuid,
        proposed_weekly_hours=weekly_hours_json,
        proposed_date_exceptions=date_exceptions_json,
        proposed_timezone=payload.timezone,
        effective_from=effective_from,
        effective_to=payload.effective_to,
    )
    if conflicts and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SCHEDULE_CONFLICT",
                "message": "The proposed schedule changes conflict with existing future appointments. Please reschedule or resolve these appointments first.",
                "details": conflicts,
            },
        )

    if existing is not None:
        existing.timezone = payload.timezone
        existing.slot_interval_minutes = payload.slot_interval_minutes
        existing.effective_from = effective_from
        existing.effective_to = payload.effective_to
        existing.weekly_hours = weekly_hours_json
        existing.date_exceptions = date_exceptions_json
        existing.version = existing.version + 1
        existing.updated_at = datetime.now(UTC)
        existing.updated_by_user_id = account.id
        schedule = existing
        action = "practitioner_schedule.updated"
    else:
        schedule = PractitionerSchedule(
            organization_id=organization_id,
            facility_id=target_facility_id,
            practitioner_id=practitioner_uuid,
            timezone=payload.timezone,
            slot_interval_minutes=payload.slot_interval_minutes,
            effective_from=effective_from,
            effective_to=payload.effective_to,
            is_active=True,
            version=1,
            weekly_hours=weekly_hours_json,
            date_exceptions=date_exceptions_json,
            is_override=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            updated_by_user_id=account.id,
        )
        db.add(schedule)
        action = "practitioner_schedule.created"

    await record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=account.id,
        action=action,
        resource_type="practitioner_schedule",
        resource_id=schedule.id,
        facility_id=target_facility_id,
    )
    await db.commit()
    await db.refresh(schedule)
    await publish(make_event(
        "schedule.changed", entity_id=schedule.id, entity_version=schedule.version,
        organization_id=schedule.organization_id, facility_id=schedule.facility_id,
        practitioner_id=schedule.practitioner_id,
        new={"date": schedule.effective_from, "resource_id": None},
    ))

    return {
        "success": True,
        "data": _format_schedule_response(schedule, practitioner, facility),
        "meta": {},
    }


@router.post(
    "/practitioners/{practitioner_uuid}/schedule/reset",
    response_model=PractitionerScheduleResponse,
)
@router.post(
    "/facilities/{facility_uuid}/practitioners/{practitioner_uuid}/schedule/reset",
    response_model=PractitionerScheduleResponse,
)
async def reset_practitioner_schedule(
    practitioner_uuid: UUID,
    payload: PractitionerScheduleResetInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: UUID | None = None,
) -> dict[str, Any]:
    target_facility_id = _extract_facility_uuid(facility_uuid)
    organization_id, facility, practitioner = await _resolve_schedule_authorization(
        db, account, practitioner_uuid, target_facility_id, write=True
    )

    facility_schedule = await db.scalar(
        select(FacilitySchedule).where(FacilitySchedule.facility_id == target_facility_id)
    )
    facility_tz_str = facility_schedule.timezone if facility_schedule else DEFAULT_TIMEZONE
    facility_tz = timezone(facility_tz_str)
    reset_date = payload.effective_date or datetime.now(facility_tz).date()

    existing = await db.scalar(
        select(PractitionerSchedule)
        .where(
            PractitionerSchedule.organization_id == organization_id,
            PractitionerSchedule.facility_id == target_facility_id,
            PractitionerSchedule.practitioner_id == practitioner_uuid,
            PractitionerSchedule.is_active.is_(True),
        )
        .with_for_update()
    )

    # Build inherited defaults to check for conflicts
    inherited_dict = _build_inherited_schedule(practitioner, facility, facility_schedule)
    conflicts = await find_schedule_conflicts(
        db,
        organization_id=organization_id,
        facility_id=target_facility_id,
        practitioner_id=practitioner_uuid,
        proposed_weekly_hours=inherited_dict["weekly_hours"],
        proposed_date_exceptions=[],
        proposed_timezone=facility_tz_str,
        effective_from=reset_date,
        effective_to=None,
    )
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SCHEDULE_CONFLICT",
                "message": "Resetting schedule to facility defaults conflicts with existing future appointments. Please reschedule or resolve these appointments first.",
                "details": conflicts,
            },
        )

    if existing is not None:
        # End active override from effective date while preserving history
        existing.effective_to = reset_date
        existing.is_active = False
        existing.updated_at = datetime.now(UTC)
        existing.updated_by_user_id = account.id

        await record_audit(
            db,
            organization_id=organization_id,
            actor_user_id=account.id,
            action="practitioner_schedule.reset",
            resource_type="practitioner_schedule",
            resource_id=existing.id,
            facility_id=target_facility_id,
        )
        await db.commit()
        await publish(make_event(
            "schedule.changed", entity_id=existing.id, entity_version=existing.version,
            organization_id=organization_id, facility_id=target_facility_id,
            practitioner_id=practitioner_uuid,
            old={"date": reset_date, "resource_id": None},
            new={"date": reset_date, "resource_id": None},
        ))

    return {"success": True, "data": inherited_dict, "meta": {}}
