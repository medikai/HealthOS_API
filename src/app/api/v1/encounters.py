from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.events import make_event, publish
from ...domains.communication.notifications.recipients import practitioner_staff_ids
from ...domains.communication.status.service import work_status_service
from ...domains.governance.audit import record_audit
from ...models.care import Appointment, Encounter, Practitioner, QueueEntry, Vital
from ...models.identity import Patient, Person, UserAccount
from ...models.organization import Facility
from ...schemas.encounters import (
    EncounterDetailResponse,
    StartConsultationResponse,
    VitalCreate,
    VitalInput,
    VitalsResponse,
)
from .scheduling import _scope

router = APIRouter(tags=["encounters"])

VITAL_UNITS = {
    "systolic_bp_mmhg": "mmHg",
    "diastolic_bp_mmhg": "mmHg",
    "pulse_bpm": "bpm",
    "respiratory_rate_per_min": "rpm",
    "spo2_percent": "%",
    "temperature_c": "°C",
    "height_cm": "cm",
    "weight_kg": "kg",
}
VITAL_NAMES = {
    "systolic": "systolic_bp_mmhg",
    "diastolic": "diastolic_bp_mmhg",
    "heart_rate": "pulse_bpm",
    "respiratory_rate": "respiratory_rate_per_min",
    "spo2": "spo2_percent",
    **{name: name for name in VITAL_UNITS},
}


def _encounter_item(e: Encounter) -> dict[str, Any]:
    return {
        "uuid": str(e.id),
        "facility_uuid": str(e.facility_id),
        "patient_uuid": str(e.patient_id),
        "practitioner_uuid": str(e.practitioner_id) if e.practitioner_id else None,
        "appointment_uuid": str(e.appointment_id) if e.appointment_id else None,
        "queue_entry_uuid": str(e.queue_entry_id) if e.queue_entry_id else None,
        "status": e.status,
        "started_at": e.started_at.isoformat(),
        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
    }


def _started_response(encounter: Encounter) -> dict[str, Any]:
    return {
        "success": True,
        "data": {"status": "in_consultation", "encounter": _encounter_item(encounter)},
        "meta": {},
    }


def _encounter_detail_item(
    encounter: Encounter,
    facility: Facility,
    patient: Patient,
    person: Person,
    practitioner: Practitioner,
    queue: QueueEntry | None,
) -> dict[str, Any]:
    item = _encounter_item(encounter)
    item.update(
        {
            "token_label": f"T-{queue.token_number}" if queue else None,
            "facility": {"uuid": str(facility.id), "name": facility.name},
            "patient": {
                "uuid": str(patient.id),
                "display_name": f"{person.first_name} {person.last_name or ''}".strip(),
                "mrn": patient.mrn,
                "gender": person.gender,
                "date_of_birth": person.date_of_birth,
            },
            "practitioner": {
                "uuid": str(practitioner.id),
                "name": practitioner.person_name,
                "specialty": practitioner.specialty,
            },
        }
    )
    return item


def _encounter_detail_query(encounter_uuid: UUID):
    return (
        select(Encounter, Facility, Patient, Person, Practitioner, QueueEntry)
        .outerjoin(
            Facility,
            and_(
                Facility.id == Encounter.facility_id,
                Facility.organization_id == Encounter.organization_id,
            ),
        )
        .outerjoin(
            Patient,
            and_(
                Patient.id == Encounter.patient_id,
                Patient.organization_id == Encounter.organization_id,
            ),
        )
        .outerjoin(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Encounter.organization_id,
            ),
        )
        .outerjoin(
            Practitioner,
            and_(
                Practitioner.id == Encounter.practitioner_id,
                Practitioner.organization_id == Encounter.organization_id,
            ),
        )
        .outerjoin(
            QueueEntry,
            and_(
                QueueEntry.id == Encounter.queue_entry_id,
                QueueEntry.organization_id == Encounter.organization_id,
                QueueEntry.facility_id == Encounter.facility_id,
            ),
        )
        .where(Encounter.id == encounter_uuid)
    )


def _number(value: str) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _vital_items(values: list[Vital]) -> list[dict[str, Any]]:
    grouped: dict[UUID, dict[str, Any]] = {}
    for vital in values:
        key = vital.recording_id or vital.id
        item = grouped.setdefault(
            key, {"uuid": str(key), "recorded_at": vital.recorded_at.isoformat()}
        )
        field = VITAL_NAMES.get(vital.name)
        if field:
            try:
                item[field] = _number(vital.value)
            except ValueError:
                pass
        if vital.recording_id is None:
            item.update({"name": vital.name, "value": vital.value, "unit": vital.unit})
    return list(grouped.values())


async def _start(
    db: AsyncSession,
    account: UserAccount,
    appointment: Appointment | None = None,
    queue: QueueEntry | None = None,
) -> Encounter:
    facility_id = appointment.facility_id if appointment else queue.facility_id
    await _scope(db, account, facility_id)
    query = (
        select(Encounter).where(Encounter.appointment_id == appointment.id)
        if appointment
        else select(Encounter).where(Encounter.queue_entry_id == queue.id)
    )
    existing = await db.scalar(query)
    if existing:
        if existing.status == "in_progress":
            return existing
        raise HTTPException(
            status_code=409, detail="Completed encounters cannot be restarted."
        )

    if appointment:
        queue = await db.scalar(
            select(QueueEntry).where(QueueEntry.appointment_id == appointment.id)
        )
        if appointment.status != "checked_in" or queue is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "APPOINTMENT_NOT_CHECKED_IN",
                    "message": "Appointment must be checked in before starting consultation.",
                },
            )
    elif queue.status not in {"called", "waiting", "skipped", "in_consultation"}:
        raise HTTPException(
            status_code=409, detail="Queue entry cannot start consultation."
        )

    linked_appointment = appointment or (
        await db.get(Appointment, queue.appointment_id)
        if queue.appointment_id
        else None
    )
    encounter = Encounter(
        organization_id=appointment.organization_id
        if appointment
        else queue.organization_id,
        facility_id=facility_id,
        patient_id=appointment.patient_id if appointment else queue.patient_id,
        practitioner_id=appointment.practitioner_id
        if appointment
        else queue.practitioner_id,
        appointment_id=linked_appointment.id if linked_appointment else None,
        queue_entry_id=queue.id,
    )
    db.add(encounter)
    if linked_appointment:
        linked_appointment.version += 1
        linked_appointment.status = "in_consultation"
    queue.status = "in_consultation"
    if encounter.practitioner_id:
        for staff_id in await practitioner_staff_ids(
            db,
            organization_id=encounter.organization_id,
            practitioner_id=encounter.practitioner_id,
        ):
            await work_status_service.apply_derived_status(
                db,
                organization_id=encounter.organization_id,
                facility_id=encounter.facility_id,
                staff_member_id=staff_id,
                work_state="with-patient",
            )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    if linked_appointment:
        await publish(make_event(
            "appointment.status_changed", entity_id=linked_appointment.id, entity_version=linked_appointment.version,
            organization_id=linked_appointment.organization_id, facility_id=linked_appointment.facility_id,
            practitioner_id=linked_appointment.practitioner_id,
            new={"status": linked_appointment.status, "date": linked_appointment.scheduled_start.date(), "resource_id": linked_appointment.resource_id},
        ))
    await publish(make_event(
        "queue.changed", entity_id=queue.id, entity_version=None,
        organization_id=queue.organization_id, facility_id=queue.facility_id,
        practitioner_id=queue.practitioner_id,
        new={"date": queue.queue_date, "status": queue.status},
    ))
    return encounter


@router.post(
    "/queue/{queue_entry_uuid}/start-consultation",
    response_model=StartConsultationResponse,
    status_code=status.HTTP_200_OK,
)
async def start_queue_consultation(
    queue_entry_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    queue = await db.get(QueueEntry, queue_entry_uuid)
    if queue is None:
        raise HTTPException(status_code=404, detail="Queue entry not found.")
    return _started_response(await _start(db, account, queue=queue))


@router.post(
    "/appointments/{appointment_uuid}/start-consultation",
    response_model=StartConsultationResponse,
    status_code=status.HTTP_200_OK,
)
async def start_appointment_consultation(
    appointment_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    return _started_response(await _start(db, account, appointment=appointment))


@router.get("/encounters/{encounter_uuid}", response_model=EncounterDetailResponse)
async def get_encounter(
    encounter_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    row = (await db.execute(_encounter_detail_query(encounter_uuid))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    encounter, facility, patient, person, practitioner, queue = row
    await _scope(db, account, encounter.facility_id)
    if facility is None or patient is None or person is None or practitioner is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ENCOUNTER_DATA_INTEGRITY_ERROR",
                "message": "Encounter references missing facility, patient, or practitioner data.",
            },
        )
    return {
        "success": True,
        "data": _encounter_detail_item(
            encounter, facility, patient, person, practitioner, queue
        ),
        "meta": {},
    }


@router.get("/encounters/{encounter_uuid}/vitals", response_model=VitalsResponse)
async def get_vitals(
    encounter_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    await _scope(db, account, encounter.facility_id)
    values = (
        await db.scalars(
            select(Vital)
            .where(Vital.encounter_id == encounter.id)
            .order_by(Vital.recorded_at.desc())
        )
    ).all()
    return {"success": True, "data": {"items": _vital_items(values)}, "meta": {}}


@router.post("/encounters/{encounter_uuid}/vitals", status_code=status.HTTP_201_CREATED)
async def create_vital(
    encounter_uuid: UUID,
    payload: VitalInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    if encounter is None or encounter.status != "in_progress":
        raise HTTPException(
            status_code=409, detail="Encounter is not available for vitals."
        )
    await _scope(db, account, encounter.facility_id)
    if isinstance(payload, VitalCreate):
        vital = Vital(
            encounter_id=encounter.id,
            recorded_by_user_id=account.id,
            name=payload.name,
            value=payload.value,
            unit=payload.unit,
        )
        db.add(vital)
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.vitals.recorded",
            resource_type="vital",
            resource_id=vital.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
        await db.commit()
        return {
            "success": True,
            "data": {
                "uuid": str(vital.id),
                "name": vital.name,
                "value": vital.value,
                "unit": vital.unit,
                "recorded_at": vital.recorded_at.isoformat(),
            },
            "meta": {},
        }
    recording_id, recorded_at = uuid7(), datetime.now(UTC)
    values = [
        Vital(
            encounter_id=encounter.id,
            recording_id=recording_id,
            recorded_by_user_id=account.id,
            name=name,
            value=str(value),
            unit=VITAL_UNITS[name],
            recorded_at=recorded_at,
        )
        for name, value in payload.model_dump().items()
        if value is not None
    ]
    db.add_all(values)
    await record_audit(
        db,
        organization_id=encounter.organization_id,
        actor_user_id=account.id,
        action="clinical.vitals.recorded",
        resource_type="vital_recording",
        resource_id=recording_id,
        facility_id=encounter.facility_id,
        patient_id=encounter.patient_id,
    )
    await db.commit()
    return {"success": True, "data": _vital_items(values)[0], "meta": {}}


@router.patch("/encounters/{encounter_uuid}/vitals/{vital_uuid}")
async def update_vital(
    encounter_uuid: UUID,
    vital_uuid: UUID,
    payload: VitalCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    vital = await db.get(Vital, vital_uuid)
    if encounter is None or vital is None or vital.encounter_id != encounter.id:
        raise HTTPException(status_code=404, detail="Vital not found.")
    if encounter.status != "in_progress":
        raise HTTPException(
            status_code=409, detail="Completed encounter vitals cannot be changed."
        )
    await _scope(db, account, encounter.facility_id)
    vital.name, vital.value, vital.unit, vital.recorded_by_user_id = (
        payload.name,
        payload.value,
        payload.unit,
        account.id,
    )
    await db.commit()
    return {
        "success": True,
        "data": {
            "uuid": str(vital.id),
            "name": vital.name,
            "value": vital.value,
            "unit": vital.unit,
            "recorded_at": vital.recorded_at.isoformat(),
        },
        "meta": {},
    }
