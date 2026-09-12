from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import Appointment, Encounter, QueueEntry, Vital
from ...models.identity import UserAccount
from ...schemas.encounters import VitalCreate
from .scheduling import _scope
from ...domains.governance.audit import record_audit

router = APIRouter(tags=["encounters"])


def _encounter_item(e: Encounter) -> dict[str, Any]:
    return {"uuid": str(e.id), "facility_uuid": str(e.facility_id), "patient_uuid": str(e.patient_id), "practitioner_uuid": str(e.practitioner_id) if e.practitioner_id else None, "appointment_uuid": str(e.appointment_id) if e.appointment_id else None, "queue_entry_uuid": str(e.queue_entry_id) if e.queue_entry_id else None, "status": e.status, "started_at": e.started_at.isoformat(), "completed_at": e.completed_at.isoformat() if e.completed_at else None}


async def _start(db: AsyncSession, account: UserAccount, appointment: Appointment | None = None, queue: QueueEntry | None = None) -> Encounter:
    facility_id = appointment.facility_id if appointment else queue.facility_id
    await _scope(db, account, facility_id)
    query = select(Encounter).where(Encounter.appointment_id == appointment.id) if appointment else select(Encounter).where(Encounter.queue_entry_id == queue.id)
    existing = await db.scalar(query)
    if existing:
        return existing
    encounter = Encounter(organization_id=appointment.organization_id if appointment else queue.organization_id, facility_id=facility_id, patient_id=appointment.patient_id if appointment else queue.patient_id, practitioner_id=appointment.practitioner_id if appointment else queue.practitioner_id, appointment_id=appointment.id if appointment else None, queue_entry_id=queue.id if queue else None)
    db.add(encounter)
    if appointment:
        appointment.status = "in_consultation"
    if queue:
        queue.status = "in_consultation"
    await db.commit()
    return encounter


@router.post("/queue/{queue_entry_uuid}/start-consultation", status_code=status.HTTP_201_CREATED)
async def start_queue_consultation(queue_entry_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    queue = await db.get(QueueEntry, queue_entry_uuid)
    if queue is None or queue.status not in {"called", "waiting", "skipped"}:
        raise HTTPException(status_code=409, detail="Queue entry cannot start consultation.")
    return {"success": True, "data": _encounter_item(await _start(db, account, queue=queue)), "meta": {}}


@router.post("/appointments/{appointment_uuid}/start-consultation", status_code=status.HTTP_201_CREATED)
async def start_appointment_consultation(appointment_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None or appointment.status not in {"checked_in", "booked"}:
        raise HTTPException(status_code=409, detail="Appointment cannot start consultation.")
    return {"success": True, "data": _encounter_item(await _start(db, account, appointment=appointment)), "meta": {}}


@router.get("/encounters/{encounter_uuid}")
async def get_encounter(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    await _scope(db, account, encounter.facility_id)
    return {"success": True, "data": _encounter_item(encounter), "meta": {}}


@router.get("/encounters/{encounter_uuid}/vitals")
async def get_vitals(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    await _scope(db, account, encounter.facility_id)
    values = (await db.scalars(select(Vital).where(Vital.encounter_id == encounter.id).order_by(Vital.recorded_at))).all()
    return {"success": True, "data": {"items": [{"uuid": str(v.id), "name": v.name, "value": v.value, "unit": v.unit, "recorded_at": v.recorded_at.isoformat()} for v in values]}, "meta": {}}


@router.post("/encounters/{encounter_uuid}/vitals", status_code=status.HTTP_201_CREATED)
async def create_vital(encounter_uuid: UUID, payload: VitalCreate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    if encounter is None or encounter.status != "in_progress":
        raise HTTPException(status_code=409, detail="Encounter is not available for vitals.")
    await _scope(db, account, encounter.facility_id)
    vital = Vital(encounter_id=encounter.id, recorded_by_user_id=account.id, name=payload.name, value=payload.value, unit=payload.unit)
    db.add(vital)
    await record_audit(db, organization_id=encounter.organization_id, actor_user_id=account.id, action="clinical.vitals.recorded", resource_type="vital", resource_id=vital.id, facility_id=encounter.facility_id, patient_id=encounter.patient_id)
    await db.commit()
    return {"success": True, "data": {"uuid": str(vital.id), "name": vital.name, "value": vital.value, "unit": vital.unit, "recorded_at": vital.recorded_at.isoformat()}, "meta": {}}


@router.patch("/encounters/{encounter_uuid}/vitals/{vital_uuid}")
async def update_vital(encounter_uuid: UUID, vital_uuid: UUID, payload: VitalCreate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    vital = await db.get(Vital, vital_uuid)
    if encounter is None or vital is None or vital.encounter_id != encounter.id:
        raise HTTPException(status_code=404, detail="Vital not found.")
    if encounter.status != "in_progress":
        raise HTTPException(status_code=409, detail="Completed encounter vitals cannot be changed.")
    await _scope(db, account, encounter.facility_id)
    vital.name, vital.value, vital.unit, vital.recorded_by_user_id = payload.name, payload.value, payload.unit, account.id
    await db.commit()
    return {"success": True, "data": {"uuid": str(vital.id), "name": vital.name, "value": vital.value, "unit": vital.unit, "recorded_at": vital.recorded_at.isoformat()}, "meta": {}}
