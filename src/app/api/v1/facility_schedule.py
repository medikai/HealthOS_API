import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.availability import find_facility_schedule_conflicts
from ...core.db.database import async_get_db
from ...core.events import make_event, publish
from ...core.timezones import DEFAULT_TIMEZONE
from ...domains.governance.audit import record_audit
from ...models.identity import UserAccount
from ...models.organization import (
    Facility,
    FacilityResource,
    FacilitySchedule,
    Organization,
    ProtectedPeriod,
)
from ...schemas.facility_schedule import (
    FacilityResourceInput,
    FacilityScheduleInput,
    ProtectedPeriodInput,
)
from .scheduling import _scope

router = APIRouter(tags=["facility-scheduling"])

def _schedule(s: FacilitySchedule, facility_uuid: UUID, configured: bool = True) -> dict[str, Any]:
    return {"facility_uuid": str(facility_uuid), "operating_start": s.operating_start, "operating_end": s.operating_end, "slot_interval_minutes": s.slot_interval_minutes, "days_of_week": json.loads(s.days_of_week), "timezone": s.timezone, "configured": configured}

@router.get("/facilities/{facility_uuid}/schedule")
async def get_schedule(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    schedule = await db.scalar(select(FacilitySchedule).where(FacilitySchedule.facility_id == facility_uuid))
    configured = schedule is not None
    if schedule is None:
        schedule = FacilitySchedule(facility_id=facility_uuid)
    periods = (await db.scalars(select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility_uuid))).all()
    return {"success": True, "data": {"schedule": _schedule(schedule, facility_uuid, configured), "protected_periods": [_period(p) for p in periods]}, "meta": {}}

@router.put("/facilities/{facility_uuid}/schedule")
async def update_schedule(facility_uuid: UUID, payload: FacilityScheduleInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    org, facility = await _scope(db, account, facility_uuid)
    organization_id = getattr(org, "id", None) or getattr(facility, "organization_id", None)
    if organization_id is None:
        org_row = await db.scalar(select(Facility.organization_id).where(Facility.id == facility_uuid))
        organization_id = org_row
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())

    conflicts = await find_facility_schedule_conflicts(
        db,
        organization_id=organization_id,
        facility_id=facility_uuid,
        proposed_operating_start=payload.operating_start,
        proposed_operating_end=payload.operating_end,
        proposed_days_of_week=payload.days_of_week,
        proposed_timezone=payload.timezone,
        proposed_slot_interval_minutes=payload.slot_interval_minutes,
    )
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "FACILITY_SCHEDULE_CONFLICT",
                "message": "The proposed facility schedule changes conflict with existing future appointments. Please reschedule or resolve these appointments first.",
                "details": conflicts,
            },
        )

    schedule = await db.scalar(select(FacilitySchedule).where(FacilitySchedule.facility_id == facility_uuid))
    if schedule is None:
        schedule = FacilitySchedule(facility_id=facility_uuid)
        db.add(schedule)
        action = "facility_schedule.created"
    else:
        action = "facility_schedule.updated"
    schedule.operating_start, schedule.operating_end, schedule.slot_interval_minutes, schedule.days_of_week, schedule.timezone = payload.operating_start, payload.operating_end, payload.slot_interval_minutes, json.dumps(payload.days_of_week), payload.timezone
    await record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=account.id,
        action=action,
        resource_type="facility_schedule",
        resource_id=facility_uuid,
        facility_id=facility_uuid,
    )
    await db.commit()
    await publish(make_event(
        "schedule.changed", entity_id=facility_uuid, entity_version=None,
        organization_id=organization_id, facility_id=facility_uuid,
        new={"date": None, "resource_id": None},
    ))
    return {"success": True, "data": {"schedule": _schedule(schedule, facility_uuid)}, "meta": {}}

def _period(p: ProtectedPeriod) -> dict[str, Any]:
    return {"uuid": str(p.id), "facility_uuid": str(p.facility_id), "title": p.title, "start_time": p.start_time, "end_time": p.end_time, "period_type": p.period_type, "days_of_week": json.loads(p.days_of_week), "is_recurring": p.is_recurring}

@router.get("/facilities/{facility_uuid}/protected-periods")
async def list_periods(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    values = (await db.scalars(select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility_uuid))).all()
    return {"success": True, "data": {"items": [_period(p) for p in values]}, "meta": {}}

async def _resolve_org_id(db: AsyncSession, facility_uuid: UUID, org_from_scope: Any) -> UUID:
    organization_id = getattr(org_from_scope, "id", None)
    if organization_id is None:
        organization_id = await db.scalar(select(Facility.organization_id).where(Facility.id == facility_uuid))
    return organization_id


async def _current_facility_schedule_values(db: AsyncSession, facility_uuid: UUID) -> tuple[str, str, list[str], str]:
    schedule = await db.scalar(select(FacilitySchedule).where(FacilitySchedule.facility_id == facility_uuid))
    if schedule is None:
        return "08:00", "20:00", ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"], DEFAULT_TIMEZONE
    try:
        days = json.loads(schedule.days_of_week)
    except Exception:
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
    return schedule.operating_start, schedule.operating_end, days, schedule.timezone


@router.post("/facilities/{facility_uuid}/protected-periods", status_code=201)
async def create_period(facility_uuid: UUID, payload: ProtectedPeriodInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    org, _ = await _scope(db, account, facility_uuid)
    organization_id = await _resolve_org_id(db, facility_uuid, org)
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())

    existing_periods = (await db.scalars(select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility_uuid))).all()
    proposed_periods = [
        {"start_time": p.start_time, "end_time": p.end_time, "days_of_week": json.loads(p.days_of_week)}
        for p in existing_periods
    ]
    proposed_periods.append({
        "start_time": payload.start_time,
        "end_time": payload.end_time,
        "days_of_week": list(payload.days_of_week),
    })
    op_start, op_end, op_days, op_tz = await _current_facility_schedule_values(db, facility_uuid)

    conflicts = await find_facility_schedule_conflicts(
        db,
        organization_id=organization_id,
        facility_id=facility_uuid,
        proposed_operating_start=op_start,
        proposed_operating_end=op_end,
        proposed_days_of_week=op_days,
        proposed_timezone=op_tz,
        proposed_protected_periods=proposed_periods,
    )
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROTECTED_PERIOD_CONFLICT",
                "message": "Adding this protected period would conflict with existing future appointments. Please reschedule or resolve these appointments first.",
                "details": conflicts,
            },
        )

    period = ProtectedPeriod(facility_id=facility_uuid, title=payload.title, start_time=payload.start_time, end_time=payload.end_time, period_type=payload.period_type, days_of_week=json.dumps(payload.days_of_week), is_recurring=payload.is_recurring)
    db.add(period)
    await record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=account.id,
        action="protected_period.created",
        resource_type="protected_period",
        resource_id=facility_uuid,
        facility_id=facility_uuid,
    )
    await db.commit()
    await publish(make_event(
        "schedule.changed", entity_id=period.id, entity_version=None,
        organization_id=organization_id, facility_id=facility_uuid,
        new={"date": None, "resource_id": None},
    ))
    return {"success": True, "data": _period(period), "meta": {}}

@router.delete("/facilities/{facility_uuid}/protected-periods/{period_id}")
async def delete_period(facility_uuid: UUID, period_id: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    org, _ = await _scope(db, account, facility_uuid)
    organization_id = await _resolve_org_id(db, facility_uuid, org)
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())
    period = await db.scalar(select(ProtectedPeriod).where(ProtectedPeriod.id == period_id, ProtectedPeriod.facility_id == facility_uuid))
    if period is None:
        raise HTTPException(status_code=404, detail="Protected period not found.")

    remaining_periods = (await db.scalars(select(ProtectedPeriod).where(
        ProtectedPeriod.facility_id == facility_uuid,
        ProtectedPeriod.id != period_id,
    ))).all()
    proposed_periods = [
        {"start_time": p.start_time, "end_time": p.end_time, "days_of_week": json.loads(p.days_of_week)}
        for p in remaining_periods
    ]
    op_start, op_end, op_days, op_tz = await _current_facility_schedule_values(db, facility_uuid)

    conflicts = await find_facility_schedule_conflicts(
        db,
        organization_id=organization_id,
        facility_id=facility_uuid,
        proposed_operating_start=op_start,
        proposed_operating_end=op_end,
        proposed_days_of_week=op_days,
        proposed_timezone=op_tz,
        proposed_protected_periods=proposed_periods,
    )
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "PROTECTED_PERIOD_CONFLICT",
                "message": "Removing this protected period would expose conflicts with existing future appointments. Please review and resolve these appointments first.",
                "details": conflicts,
            },
        )

    await db.delete(period)
    await record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=account.id,
        action="protected_period.deleted",
        resource_type="protected_period",
        resource_id=period_id,
        facility_id=facility_uuid,
    )
    await db.commit()
    await publish(make_event(
        "schedule.changed", entity_id=period_id, entity_version=None,
        organization_id=organization_id, facility_id=facility_uuid,
        new={"date": None, "resource_id": None},
    ))
    return {"success": True, "data": {}, "meta": {}}


def _resource(value: FacilityResource) -> dict[str, Any]:
    return {"uuid": str(value.id), "facility_uuid": str(value.facility_id), "name": value.name, "resource_type": value.resource_type, "is_active": value.is_active}


@router.get("/facilities/{facility_uuid}/resources")
async def list_resources(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    values = (await db.scalars(select(FacilityResource).where(FacilityResource.facility_id == facility_uuid, FacilityResource.is_active.is_(True)).order_by(FacilityResource.name))).all()
    return {"success": True, "data": {"items": [_resource(value) for value in values]}, "meta": {}}


@router.post("/facilities/{facility_uuid}/resources", status_code=201)
async def create_resource(facility_uuid: UUID, payload: FacilityResourceInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    existing = await db.scalar(
        select(FacilityResource).where(
            FacilityResource.facility_id == facility_uuid,
            FacilityResource.name == payload.name,
        )
    )
    if existing:
        if not existing.is_active:
            existing.is_active = True
            existing.resource_type = payload.resource_type
            await db.commit()
            await publish(make_event(
                "schedule.changed", entity_id=existing.id, entity_version=None,
                organization_id=organization.id, facility_id=facility_uuid,
                new={"date": None, "resource_id": existing.id},
            ))
            return {"success": True, "data": _resource(existing), "meta": {}}
        raise HTTPException(status_code=409, detail="A room/resource with this name already exists at the facility.")
    value = FacilityResource(facility_id=facility_uuid, name=payload.name, resource_type=payload.resource_type)
    db.add(value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A room/resource with this name already exists at the facility.") from None
    await publish(make_event(
        "schedule.changed", entity_id=value.id, entity_version=None,
        organization_id=organization.id, facility_id=facility_uuid,
        new={"date": None, "resource_id": value.id},
    ))
    return {"success": True, "data": _resource(value), "meta": {}}


@router.delete("/facilities/{facility_uuid}/resources/{resource_uuid}")
async def deactivate_resource(facility_uuid: UUID, resource_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    org, _ = await _scope(db, account, facility_uuid)
    organization_id = await _resolve_org_id(db, facility_uuid, org)
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())
    value = await db.scalar(select(FacilityResource).where(FacilityResource.id == resource_uuid, FacilityResource.facility_id == facility_uuid).with_for_update())
    if value is None:
        raise HTTPException(status_code=404, detail="Facility room/resource not found.")

    from datetime import UTC, datetime as _dt
    from ...models.care import Appointment as _Appt
    from ...core.availability import ACTIVE_APPOINTMENT_STATUSES as _ACTIVE
    now_utc = _dt.now(UTC)
    using_appointments = list((await db.scalars(
        select(_Appt).where(
            _Appt.organization_id == organization_id,
            _Appt.facility_id == facility_uuid,
            _Appt.resource_id == resource_uuid,
            _Appt.status.in_(_ACTIVE),
            _Appt.scheduled_end > now_utc,
        ).order_by(_Appt.scheduled_start)
    )).all())
    if using_appointments:
        conflicts = [
            {
                "appointment_uuid": str(a.id),
                "practitioner_uuid": str(a.practitioner_id),
                "patient_uuid": str(a.patient_id),
                "scheduled_start": a.scheduled_start.isoformat(),
                "scheduled_end": a.scheduled_end.isoformat(),
                "conflict_reason": "RESOURCE_IN_USE",
            }
            for a in using_appointments
        ]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESOURCE_IN_USE",
                "message": "This room/resource is assigned to existing future appointments. Reassign or resolve those appointments first.",
                "details": conflicts,
            },
        )

    value.is_active = False
    await record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=account.id,
        action="facility_resource.deactivated",
        resource_type="facility_resource",
        resource_id=resource_uuid,
        facility_id=facility_uuid,
    )
    await db.commit()
    await publish(make_event(
        "schedule.changed", entity_id=resource_uuid, entity_version=None,
        organization_id=organization_id, facility_id=facility_uuid,
        new={"date": None, "resource_id": resource_uuid},
    ))
    return {"success": True, "data": _resource(value), "meta": {}}
