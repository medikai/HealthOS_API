from datetime import date, datetime, UTC
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import Appointment, QueueCounter, QueueEntry
from ...models.identity import Patient, UserAccount
from ...schemas.queue import WalkInCreate
from .bootstrap import _staff_context
from .scheduling import _scope
from ...domains.governance.audit import record_audit

router = APIRouter(tags=["queue"])


async def _next_token(db: AsyncSession, facility_id: UUID, queue_date: date) -> int:
    counter = await db.scalar(select(QueueCounter).where(QueueCounter.facility_id == facility_id, QueueCounter.queue_date == queue_date).with_for_update())
    if counter is None:
        counter = QueueCounter(facility_id=facility_id, queue_date=queue_date, last_token_number=0)
        db.add(counter)
        await db.flush()
    counter.last_token_number += 1
    await db.flush()
    return counter.last_token_number


def _item(entry: QueueEntry) -> dict[str, Any]:
    return {"uuid": str(entry.id), "facility_uuid": str(entry.facility_id), "patient_uuid": str(entry.patient_id), "appointment_uuid": str(entry.appointment_id) if entry.appointment_id else None, "practitioner_uuid": str(entry.practitioner_id) if entry.practitioner_id else None, "token_number": entry.token_number, "status": entry.status, "reason_code": entry.reason_code, "called_at": entry.called_at.isoformat() if entry.called_at else None}


@router.get("/queue")
async def queue_list(facility_uuid: str, queue_date: date = Query(default_factory=date.today), account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ...) -> dict[str, Any]:
    organization, facility = await _scope(db, account, facility_uuid)
    query = select(QueueEntry).where(QueueEntry.organization_id == organization.id, QueueEntry.queue_date == queue_date)
    if str(facility_uuid).lower() != "all":
        query = query.where(QueueEntry.facility_id == UUID(facility_uuid))
    rows = (await db.scalars(query.order_by(QueueEntry.token_number))).all()
    return {"success": True, "data": {"items": [_item(row) for row in rows]}, "meta": {"queue_date": queue_date.isoformat(), "facility_uuid": str(facility.id) if facility else "all"}}


@router.post("/walk-ins", status_code=status.HTTP_201_CREATED)
async def create_walk_in(payload: WalkInCreate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    patient = await db.scalar(select(Patient).where(Patient.id == payload.patient_uuid, Patient.organization_id == organization.id, Patient.is_active.is_(True)))
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    today = date.today()
    entry = QueueEntry(organization_id=organization.id, facility_id=payload.facility_uuid, patient_id=payload.patient_uuid, practitioner_id=payload.practitioner_uuid, queue_date=today, token_number=await _next_token(db, payload.facility_uuid, today), reason_code=payload.reason_code, reason_text=payload.reason_text)
    db.add(entry)
    await db.commit()
    return {"success": True, "data": _item(entry), "meta": {}}


@router.get("/queue/{queue_entry_uuid}")
async def get_queue_entry(queue_entry_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    entry = await db.get(QueueEntry, queue_entry_uuid)
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue entry not found.")
    await _scope(db, account, entry.facility_id)
    return {"success": True, "data": _item(entry), "meta": {}}


async def _transition(queue_entry_uuid: UUID, expected: set[str], target: str, account: UserAccount, db: AsyncSession, called: bool = False) -> QueueEntry:
    entry = await db.scalar(select(QueueEntry).where(QueueEntry.id == queue_entry_uuid).with_for_update())
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue entry not found.")
    await _scope(db, account, entry.facility_id)
    if entry.status not in expected:
        raise HTTPException(status_code=409, detail=f"Queue entry cannot transition from {entry.status}.")
    entry.status = target
    if called:
        entry.called_at = datetime.now(UTC)
    await record_audit(db, organization_id=entry.organization_id, actor_user_id=account.id, action=f"queue.{target}", resource_type="queue_entry", resource_id=entry.id, facility_id=entry.facility_id, patient_id=entry.patient_id)
    await db.commit()
    return entry


@router.post("/queue/{queue_entry_uuid}/call")
async def call_queue_entry(queue_entry_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    return {"success": True, "data": _item(await _transition(queue_entry_uuid, {"waiting", "skipped"}, "called", account, db, True)), "meta": {}}


@router.post("/queue/{queue_entry_uuid}/skip")
async def skip_queue_entry(queue_entry_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    entry = await _transition(queue_entry_uuid, {"called"}, "skipped", account, db)
    entry.skip_count += 1
    await db.commit()
    return {"success": True, "data": _item(entry), "meta": {}}


@router.post("/appointments/{appointment_uuid}/check-in")
async def check_in(appointment_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    appointment = await db.scalar(select(Appointment).where(Appointment.id == appointment_uuid).with_for_update())
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status != "booked":
        raise HTTPException(status_code=409, detail="Appointment cannot be checked in from its current state.")
    entry = QueueEntry(organization_id=appointment.organization_id, facility_id=appointment.facility_id, appointment_id=appointment.id, patient_id=appointment.patient_id, practitioner_id=appointment.practitioner_id, queue_date=date.today(), token_number=await _next_token(db, appointment.facility_id, date.today()), reason_code=appointment.reason_code, reason_text=appointment.reason_text)
    appointment.status = "checked_in"
    db.add(entry)
    await db.commit()
    return {"success": True, "data": _item(entry), "meta": {}}


@router.post("/queue/{queue_entry_uuid}/cancel")
async def cancel_queue_entry(queue_entry_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    return {"success": True, "data": _item(await _transition(queue_entry_uuid, {"waiting", "called", "skipped"}, "cancelled", account, db)), "meta": {}}
