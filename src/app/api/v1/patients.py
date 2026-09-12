import secrets
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.identity import Patient, Person, UserAccount
from ...models.care import Appointment, Encounter, Prescription
from ...schemas.patients import PatientCreate, PatientUpdate
from .bootstrap import _staff_context
from ...domains.governance.audit import record_audit

router = APIRouter(prefix="/patients", tags=["patients"])


async def _org(db: AsyncSession, account: UserAccount):
    _, organization = await _staff_context(db, account)
    return organization


def _item(patient: Patient, person: Person) -> dict[str, Any]:
    name = f"{person.first_name} {person.last_name or ''}".strip()
    return {"uuid": str(patient.id), "mrn": patient.mrn, "medical_record_number": patient.mrn, "display_name": name, "first_name": person.first_name, "last_name": person.last_name, "phone": person.phone, "phone_masked": person.phone, "email": person.email, "date_of_birth": person.date_of_birth, "gender": person.gender, "status": "active" if patient.is_active else "inactive"}


@router.get("/search")
async def search_patients(q: str = Query(default=""), account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ..., limit: int = Query(default=20, ge=1, le=50), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    organization = await _org(db, account)
    pattern = f"%{q}%"
    rows = await db.execute(select(Patient, Person).join(Person, Person.id == Patient.person_id).where(Patient.organization_id == organization.id, Patient.is_active.is_(True), or_(Person.first_name.ilike(pattern), Person.last_name.ilike(pattern), Person.phone.ilike(pattern), Patient.mrn.ilike(pattern))).offset(offset).limit(limit))
    items = [_item(patient, person) for patient, person in rows.all()]
    return {"success": True, "data": {"items": items, "total": len(items), "limit": limit, "offset": offset}, "meta": {"count": len(items)}}


@router.get("/duplicates")
async def duplicate_patients(phone: str | None = None, first_name: str | None = None, last_name: str | None = None, account: Annotated[UserAccount, Depends(get_current_identity_account)] = ..., db: Annotated[AsyncSession, Depends(async_get_db)] = ...) -> dict[str, Any]:
    organization = await _org(db, account)
    query = select(Patient, Person).join(Person, Person.id == Patient.person_id).where(Patient.organization_id == organization.id, Patient.is_active.is_(True))
    if phone:
        query = query.where(Person.phone == phone)
    if first_name:
        query = query.where(Person.first_name.ilike(first_name))
    if last_name:
        query = query.where(Person.last_name.ilike(last_name))
    rows = (await db.execute(query.limit(20))).all()
    return {"success": True, "data": {"items": [_item(patient, person) for patient, person in rows]}, "meta": {"count": len(rows)}}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_patient(payload: PatientCreate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization = await _org(db, account)
    normalized = payload.normalized()
    if normalized.phone:
        existing = await db.scalar(select(Person).where(Person.organization_id == organization.id, Person.phone == normalized.phone))
        if existing:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A patient with this phone already exists.")
    person = Person(organization_id=organization.id, first_name=normalized.first_name, last_name=normalized.last_name, phone=normalized.phone, email=normalized.email, date_of_birth=normalized.date_of_birth, gender=normalized.gender)
    db.add(person)
    await db.flush()
    patient = Patient(organization_id=organization.id, person_id=person.id, mrn=f"MRN-{secrets.token_hex(5).upper()}")
    db.add(patient)
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="patient.created", resource_type="patient", resource_id=patient.id)
    await db.commit()
    return {"success": True, "data": _item(patient, person), "meta": {}}


@router.get("/{patient_uuid}")
async def get_patient(patient_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization = await _org(db, account)
    row = await db.execute(select(Patient, Person).join(Person, Person.id == Patient.person_id).where(Patient.id == patient_uuid, Patient.organization_id == organization.id, Patient.is_active.is_(True)))
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return {"success": True, "data": _item(*result), "meta": {}}


@router.patch("/{patient_uuid}")
async def update_patient(patient_uuid: UUID, payload: PatientUpdate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization = await _org(db, account)
    row = await db.execute(select(Patient, Person).join(Person, Person.id == Patient.person_id).where(Patient.id == patient_uuid, Patient.organization_id == organization.id, Patient.is_active.is_(True)))
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    _, person = result
    normalized = payload.normalized()
    person.first_name, person.last_name, person.phone, person.email = normalized.first_name, normalized.last_name, normalized.phone, normalized.email
    person.date_of_birth, person.gender = normalized.date_of_birth, normalized.gender
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="patient.updated", resource_type="patient", resource_id=patient_uuid)
    await db.commit()
    return {"success": True, "data": _item(*result), "meta": {}}


@router.get("/{patient_uuid}/context")
async def patient_context(patient_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    organization = await _org(db, account)
    patient = await db.scalar(select(Patient).where(Patient.id == patient_uuid, Patient.organization_id == organization.id, Patient.is_active.is_(True)))
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    person = await db.get(Person, patient.person_id)
    active_appointments = await db.scalar(select(func.count(Appointment.id)).where(Appointment.patient_id == patient.id, Appointment.organization_id == organization.id, Appointment.status.in_(["booked", "checked_in", "in_consultation"])))
    return {"success": True, "data": {"patient": _item(patient, person), "active_appointments": active_appointments or 0}, "meta": {}}


@router.get("/{patient_uuid}/timeline")
async def patient_timeline(patient_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    organization = await _org(db, account)
    patient = await db.scalar(select(Patient).where(Patient.id == patient_uuid, Patient.organization_id == organization.id, Patient.is_active.is_(True)))
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    appointments = (await db.scalars(select(Appointment).where(Appointment.patient_id == patient.id, Appointment.organization_id == organization.id).order_by(Appointment.scheduled_start.desc()).limit(limit))).all()
    encounters = (await db.scalars(select(Encounter).where(Encounter.patient_id == patient.id, Encounter.organization_id == organization.id).order_by(Encounter.started_at.desc()).limit(limit))).all()
    items = [{"type": "appointment", "uuid": str(a.id), "status": a.status, "occurred_at": a.scheduled_start.isoformat()} for a in appointments] + [{"type": "encounter", "uuid": str(e.id), "status": e.status, "occurred_at": e.started_at.isoformat()} for e in encounters]
    items.sort(key=lambda x: x["occurred_at"], reverse=True)
    return {"success": True, "data": {"items": items[:limit]}, "meta": {"count": min(len(items), limit)}}
