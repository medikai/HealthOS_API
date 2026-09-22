from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.appointment_views import patient_view, practitioner_view
from ...core.db.database import async_get_db
from ...core.events import make_event, publish
from ...domains.governance.audit import record_audit
from ...models.care import Appointment, Practitioner, QueueCounter, QueueEntry
from ...models.identity import Patient, Person, UserAccount
from ...models.organization import StaffAssignment, StaffMember
from ...schemas.queue import WalkInCreate
from .bootstrap import ADMIN_ROLES, _staff_context
from .scheduling import _scope

router = APIRouter(tags=["queue"])
ACTIVE_QUEUE_STATUSES = {"waiting", "called", "skipped", "in_consultation"}


async def _next_token(db: AsyncSession, facility_id: UUID, queue_date: date) -> int:
    counter = await db.scalar(
        select(QueueCounter)
        .where(
            QueueCounter.facility_id == facility_id,
            QueueCounter.queue_date == queue_date,
        )
        .with_for_update()
    )
    if counter is None:
        counter = QueueCounter(
            facility_id=facility_id, queue_date=queue_date, last_token_number=0
        )
        db.add(counter)
        await db.flush()
    counter.last_token_number += 1
    await db.flush()
    return counter.last_token_number


def _queue_query(
    organization_id: UUID | None = None,
    staff_member_id: UUID | None = None,
    account_id: UUID | None = None,
):
    query = (
        select(QueueEntry, Patient, Person, Practitioner)
        .outerjoin(
            Patient,
            and_(
                Patient.id == QueueEntry.patient_id,
                Patient.organization_id == QueueEntry.organization_id,
            ),
        )
        .outerjoin(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == QueueEntry.organization_id,
            ),
        )
        .outerjoin(
            Practitioner,
            and_(
                Practitioner.id == QueueEntry.practitioner_id,
                Practitioner.organization_id == QueueEntry.organization_id,
            ),
        )
    )
    if organization_id is not None:
        query = query.where(QueueEntry.organization_id == organization_id)
    if account_id is not None:
        query = query.where(
            exists(
                select(StaffAssignment.id)
                .join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id)
                .where(
                    StaffMember.user_account_id == account_id,
                    StaffMember.organization_id == QueueEntry.organization_id,
                    StaffMember.is_active.is_(True),
                    StaffAssignment.is_active.is_(True),
                    or_(
                        StaffAssignment.role_code.in_(ADMIN_ROLES),
                        StaffAssignment.facility_id == QueueEntry.facility_id,
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
                        StaffAssignment.facility_id == QueueEntry.facility_id,
                    ),
                )
            )
        )
    return query


def _item(
    entry: QueueEntry,
    patient: Patient | None = None,
    person: Person | None = None,
    practitioner: Practitioner | None = None,
) -> dict[str, Any]:
    return {
        "uuid": str(entry.id),
        "facility_uuid": str(entry.facility_id),
        "patient_uuid": str(entry.patient_id),
        "appointment_uuid": str(entry.appointment_id) if entry.appointment_id else None,
        "practitioner_uuid": str(entry.practitioner_id)
        if entry.practitioner_id
        else None,
        "patient": patient_view(patient, person),
        "practitioner": practitioner_view(practitioner),
        "token_number": entry.token_number,
        "token_label": f"T-{entry.token_number}",
        "status": entry.status,
        "is_active": entry.status in ACTIVE_QUEUE_STATUSES,
        "reason_code": entry.reason_code,
        "called_at": entry.called_at.isoformat() if entry.called_at else None,
    }


async def _walk_in_item(db: AsyncSession, entry: QueueEntry) -> dict[str, Any]:
    position = (
        await db.scalar(
            select(func.count())
            .select_from(QueueEntry)
            .where(
                QueueEntry.facility_id == entry.facility_id,
                QueueEntry.queue_date == entry.queue_date,
                QueueEntry.status.in_(ACTIVE_QUEUE_STATUSES),
                QueueEntry.token_number <= entry.token_number,
            )
        )
        or 1
    )
    return {
        **_item(entry),
        "checked_in_at": datetime.now(UTC).isoformat(),
        "queue_position": position,
        "patients_ahead": max(0, position - 1),
    }


@router.get("/queue")
async def queue_list(
    facility_uuid: str,
    queue_date: date = Query(default_factory=date.today),
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
) -> dict[str, Any]:
    if str(facility_uuid).lower() != "all":
        await _scope(db, account, facility_uuid)
    query = _queue_query(account_id=account.id).where(
        QueueEntry.queue_date == queue_date
    )
    if str(facility_uuid).lower() != "all":
        query = query.where(QueueEntry.facility_id == UUID(facility_uuid))
    rows = (await db.execute(query.order_by(QueueEntry.token_number))).all()
    return {
        "success": True,
        "data": {"items": [_item(*row) for row in rows]},
        "meta": {"queue_date": queue_date.isoformat(), "facility_uuid": facility_uuid},
    }


@router.post("/walk-ins", status_code=status.HTTP_201_CREATED)
async def create_walk_in(
    payload: WalkInCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization, _ = await _scope(db, account, payload.facility_uuid)
    patient = await db.scalar(
        select(Patient)
        .where(
            Patient.id == payload.patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
        .with_for_update()
    )
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    today = date.today()
    entry = await db.scalar(
        select(QueueEntry).where(
            QueueEntry.organization_id == organization.id,
            QueueEntry.facility_id == payload.facility_uuid,
            QueueEntry.patient_id == payload.patient_uuid,
            QueueEntry.appointment_id.is_(None),
            QueueEntry.queue_date == today,
            QueueEntry.status.in_(ACTIVE_QUEUE_STATUSES),
        )
    )
    if entry is not None:
        return {
            "success": True,
            "data": {"queue_entry": await _walk_in_item(db, entry)},
            "meta": {"reused": True},
        }
    entry = QueueEntry(
        organization_id=organization.id,
        facility_id=payload.facility_uuid,
        patient_id=payload.patient_uuid,
        practitioner_id=payload.practitioner_uuid,
        queue_date=today,
        token_number=await _next_token(db, payload.facility_uuid, today),
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
    )
    db.add(entry)
    await db.commit()
    await publish(make_event(
        "queue.changed", entity_id=entry.id, entity_version=None,
        organization_id=entry.organization_id, facility_id=entry.facility_id,
        practitioner_id=entry.practitioner_id,
        new={"date": entry.queue_date, "status": entry.status},
    ))
    return {
        "success": True,
        "data": {"queue_entry": await _walk_in_item(db, entry)},
        "meta": {},
    }


@router.get("/queue/{queue_entry_uuid}")
async def get_queue_entry(
    queue_entry_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    row = (
        await db.execute(
            _queue_query(organization.id, staff.id).where(
                QueueEntry.id == queue_entry_uuid
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Queue entry not found.")
    return {"success": True, "data": _item(*row), "meta": {}}


async def _transition(
    queue_entry_uuid: UUID,
    expected: set[str],
    target: str,
    account: UserAccount,
    db: AsyncSession,
    called: bool = False,
    idempotent: bool = False,
) -> QueueEntry:
    entry = await db.scalar(
        select(QueueEntry).where(QueueEntry.id == queue_entry_uuid).with_for_update()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Queue entry not found.")
    await _scope(db, account, entry.facility_id)
    if idempotent and entry.status == target:
        entry._queue_write_committed = False
        return entry
    if entry.status not in expected:
        raise HTTPException(
            status_code=409,
            detail=f"Queue entry cannot transition from {entry.status}.",
        )
    entry.status = target
    if called:
        entry.called_at = datetime.now(UTC)
    await record_audit(
        db,
        organization_id=entry.organization_id,
        actor_user_id=account.id,
        action=f"queue.{target}",
        resource_type="queue_entry",
        resource_id=entry.id,
        facility_id=entry.facility_id,
        patient_id=entry.patient_id,
    )
    await db.commit()
    entry._queue_write_committed = True
    return entry


@router.post("/queue/{queue_entry_uuid}/call")
async def call_queue_entry(
    queue_entry_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    entry = await _transition(
        queue_entry_uuid, {"waiting", "skipped"}, "called", account, db,
        called=True, idempotent=True,
    )
    if entry._queue_write_committed:
        await publish(make_event(
            "queue.changed", entity_id=entry.id, entity_version=None,
            organization_id=entry.organization_id, facility_id=entry.facility_id,
            practitioner_id=entry.practitioner_id,
            new={"date": entry.queue_date, "status": entry.status},
        ))
    return {
        "success": True,
        "data": _item(entry),
        "meta": {},
    }


@router.post("/queue/{queue_entry_uuid}/skip")
async def skip_queue_entry(
    queue_entry_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    entry = await _transition(queue_entry_uuid, {"called"}, "skipped", account, db)
    entry.skip_count += 1
    await db.commit()
    await publish(make_event(
        "queue.changed", entity_id=entry.id, entity_version=None,
        organization_id=entry.organization_id, facility_id=entry.facility_id,
        practitioner_id=entry.practitioner_id,
        new={"date": entry.queue_date, "status": entry.status},
    ))
    return {"success": True, "data": _item(entry), "meta": {}}


@router.post("/appointments/{appointment_uuid}/check-in")
async def check_in(
    appointment_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    appointment = await db.scalar(
        select(Appointment).where(Appointment.id == appointment_uuid).with_for_update()
    )
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    await _scope(db, account, appointment.facility_id)
    if appointment.status not in {"booked", "confirmed", "checked_in"}:
        raise HTTPException(
            status_code=409,
            detail="Appointment cannot be checked in from its current state.",
        )
    entry = await db.scalar(
        select(QueueEntry).where(QueueEntry.appointment_id == appointment.id)
    )
    reused = entry is not None
    if reused and appointment.status == "checked_in":
        return {"success": True, "data": _item(entry), "meta": {"reused": True}}
    if entry is None:
        today = date.today()
        entry = QueueEntry(
            organization_id=appointment.organization_id,
            facility_id=appointment.facility_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            practitioner_id=appointment.practitioner_id,
            queue_date=today,
            token_number=await _next_token(db, appointment.facility_id, today),
            reason_code=appointment.reason_code,
            reason_text=appointment.reason_text,
        )
        db.add(entry)
    old_appointment_status = appointment.status
    appointment.status = "checked_in"
    appointment.version += 1
    await db.commit()
    await publish(make_event(
        "appointment.status_changed", entity_id=appointment.id, entity_version=appointment.version,
        organization_id=appointment.organization_id, facility_id=appointment.facility_id,
        practitioner_id=appointment.practitioner_id,
        old={"status": old_appointment_status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
        new={"status": appointment.status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
    ))
    await publish(make_event(
        "queue.changed", entity_id=entry.id, entity_version=None,
        organization_id=entry.organization_id, facility_id=entry.facility_id,
        practitioner_id=entry.practitioner_id,
        new={"date": entry.queue_date, "status": entry.status},
    ))
    return {
        "success": True,
        "data": _item(entry),
        "meta": {"reused": True} if reused else {},
    }


@router.post("/queue/{queue_entry_uuid}/cancel")
async def cancel_queue_entry(
    queue_entry_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    entry = await _transition(
        queue_entry_uuid, {"waiting", "called", "skipped"}, "cancelled", account, db,
    )
    await publish(make_event(
        "queue.changed", entity_id=entry.id, entity_version=None,
        organization_id=entry.organization_id, facility_id=entry.facility_id,
        practitioner_id=entry.practitioner_id,
        new={"date": entry.queue_date, "status": entry.status},
    ))
    return {
        "success": True,
        "data": _item(entry),
        "meta": {},
    }
