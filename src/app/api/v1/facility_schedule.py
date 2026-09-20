import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.identity import UserAccount
from ...models.organization import (
    Facility,
    FacilityResource,
    FacilitySchedule,
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
    await _scope(db, account, facility_uuid)
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())
    schedule = await db.scalar(select(FacilitySchedule).where(FacilitySchedule.facility_id == facility_uuid))
    if schedule is None:
        schedule = FacilitySchedule(facility_id=facility_uuid)
        db.add(schedule)
    schedule.operating_start, schedule.operating_end, schedule.slot_interval_minutes, schedule.days_of_week, schedule.timezone = payload.operating_start, payload.operating_end, payload.slot_interval_minutes, json.dumps(payload.days_of_week), payload.timezone
    await db.commit()
    return {"success": True, "data": {"schedule": _schedule(schedule, facility_uuid)}, "meta": {}}

def _period(p: ProtectedPeriod) -> dict[str, Any]:
    return {"uuid": str(p.id), "facility_uuid": str(p.facility_id), "title": p.title, "start_time": p.start_time, "end_time": p.end_time, "period_type": p.period_type, "days_of_week": json.loads(p.days_of_week), "is_recurring": p.is_recurring}

@router.get("/facilities/{facility_uuid}/protected-periods")
async def list_periods(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    values = (await db.scalars(select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility_uuid))).all()
    return {"success": True, "data": {"items": [_period(p) for p in values]}, "meta": {}}

@router.post("/facilities/{facility_uuid}/protected-periods", status_code=201)
async def create_period(facility_uuid: UUID, payload: ProtectedPeriodInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())
    period = ProtectedPeriod(facility_id=facility_uuid, title=payload.title, start_time=payload.start_time, end_time=payload.end_time, period_type=payload.period_type, days_of_week=json.dumps(payload.days_of_week), is_recurring=payload.is_recurring)
    db.add(period)
    await db.commit()
    return {"success": True, "data": _period(period), "meta": {}}

@router.delete("/facilities/{facility_uuid}/protected-periods/{period_id}")
async def delete_period(facility_uuid: UUID, period_id: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())
    period = await db.scalar(select(ProtectedPeriod).where(ProtectedPeriod.id == period_id, ProtectedPeriod.facility_id == facility_uuid))
    if period is None:
        raise HTTPException(status_code=404, detail="Protected period not found.")
    await db.delete(period)
    await db.commit()
    return {"success": True, "data": {}, "meta": {}}


def _resource(value: FacilityResource) -> dict[str, Any]:
    return {"uuid": str(value.id), "facility_uuid": str(value.facility_id), "name": value.name, "resource_type": value.resource_type, "is_active": value.is_active}


@router.get("/facilities/{facility_uuid}/resources")
async def list_resources(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
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
            return {"success": True, "data": _resource(existing), "meta": {}}
        raise HTTPException(status_code=409, detail="A room/resource with this name already exists at the facility.")
    value = FacilityResource(facility_id=facility_uuid, name=payload.name, resource_type=payload.resource_type)
    db.add(value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A room/resource with this name already exists at the facility.") from None
    return {"success": True, "data": _resource(value), "meta": {}}


@router.delete("/facilities/{facility_uuid}/resources/{resource_uuid}")
async def deactivate_resource(facility_uuid: UUID, resource_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    await db.scalar(select(Facility.id).where(Facility.id == facility_uuid).with_for_update())
    value = await db.scalar(select(FacilityResource).where(FacilityResource.id == resource_uuid, FacilityResource.facility_id == facility_uuid).with_for_update())
    if value is None:
        raise HTTPException(status_code=404, detail="Facility room/resource not found.")
    value.is_active = False
    await db.commit()
    return {"success": True, "data": _resource(value), "meta": {}}
