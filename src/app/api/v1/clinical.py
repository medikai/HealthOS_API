from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import Appointment, Diagnosis, Encounter, Prescription, PrescriptionItem, Practitioner, QueueEntry, SoapNote
from ...models.identity import UserAccount
from ...schemas.clinical import DiagnosisInput, PrescriptionInput, SoapInput
from ...domains.governance.audit import record_audit
from .scheduling import _scope

router = APIRouter(tags=["clinical"])


async def _encounter(db: AsyncSession, account: UserAccount, encounter_uuid: UUID) -> Encounter:
    encounter = await db.get(Encounter, encounter_uuid)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    await _scope(db, account, encounter.facility_id)
    if encounter.status != "in_progress":
        raise HTTPException(status_code=409, detail="Encounter is no longer editable.")
    return encounter


async def _practitioner(db: AsyncSession, account: UserAccount, encounter: Encounter) -> Practitioner:
    practitioner = await db.scalar(select(Practitioner).where(Practitioner.user_account_id == account.id, Practitioner.organization_id == encounter.organization_id, Practitioner.is_active.is_(True)))
    if practitioner is None:
        raise HTTPException(status_code=403, detail="A practitioner account is required for this clinical action.")
    return practitioner


def _soap_item(note: SoapNote) -> dict[str, Any]:
    return {"uuid": str(note.id), "subjective": note.subjective, "objective": note.objective, "assessment": note.assessment, "plan": note.plan, "status": note.status, "signed_at": note.signed_at.isoformat() if note.signed_at else None}


@router.get("/encounters/{encounter_uuid}/soap")
async def get_soap(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    note = await db.scalar(select(SoapNote).where(SoapNote.encounter_id == encounter.id))
    return {"success": True, "data": _soap_item(note) if note else None, "meta": {}}


@router.put("/encounters/{encounter_uuid}/soap")
@router.post("/encounters/{encounter_uuid}/soap")
async def upsert_soap(encounter_uuid: UUID, payload: SoapInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    await _practitioner(db, account, encounter)
    note = await db.scalar(select(SoapNote).where(SoapNote.encounter_id == encounter.id))
    if note is None:
        note = SoapNote(encounter_id=encounter.id)
        db.add(note)
    if note.status == "signed":
        raise HTTPException(status_code=409, detail="Signed SOAP notes require an amendment workflow.")
    for field in ("subjective", "objective", "assessment", "plan"):
        setattr(note, field, getattr(payload, field))
    await db.commit()
    return {"success": True, "data": _soap_item(note), "meta": {}}


@router.post("/encounters/{encounter_uuid}/soap/sign")
async def sign_soap(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    await _practitioner(db, account, encounter)
    note = await db.scalar(select(SoapNote).where(SoapNote.encounter_id == encounter.id))
    if note is None:
        raise HTTPException(status_code=400, detail="SOAP draft is required before signing.")
    note.status, note.signed_by_user_id, note.signed_at = "signed", account.id, datetime.now(UTC)
    await record_audit(db, organization_id=encounter.organization_id, actor_user_id=account.id, action="clinical.soap.signed", resource_type="soap_note", resource_id=note.id, facility_id=encounter.facility_id, patient_id=encounter.patient_id)
    await db.commit()
    return {"success": True, "data": _soap_item(note), "meta": {}}


@router.get("/encounters/{encounter_uuid}/diagnoses")
async def get_diagnoses(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    values = (await db.scalars(select(Diagnosis).where(Diagnosis.encounter_id == encounter.id))).all()
    return {"success": True, "data": {"items": [{"uuid": str(d.id), "code": d.code, "description": d.description, "is_primary": d.is_primary} for d in values]}, "meta": {}}


@router.put("/encounters/{encounter_uuid}/diagnoses")
async def replace_diagnoses(encounter_uuid: UUID, payload: list[DiagnosisInput], account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    await _practitioner(db, account, encounter)
    await db.execute(delete(Diagnosis).where(Diagnosis.encounter_id == encounter.id))
    values = [Diagnosis(encounter_id=encounter.id, code=d.code, description=d.description, is_primary=d.is_primary) for d in payload]
    db.add_all(values)
    await db.commit()
    return {"success": True, "data": {"items": [{"uuid": str(d.id), "code": d.code, "description": d.description, "is_primary": d.is_primary} for d in values]}, "meta": {}}


@router.post("/encounters/{encounter_uuid}/prescriptions")
async def create_prescription(encounter_uuid: UUID, payload: PrescriptionInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    await _practitioner(db, account, encounter)
    prescription = Prescription(encounter_id=encounter.id, advice=payload.advice)
    db.add(prescription)
    await db.flush()
    db.add_all([PrescriptionItem(prescription_id=prescription.id, **item.model_dump()) for item in payload.items])
    await db.commit()
    return {"success": True, "data": {"uuid": str(prescription.id), "status": prescription.status}, "meta": {}}


async def _prescription(db: AsyncSession, account: UserAccount, prescription_uuid: UUID) -> tuple[Prescription, Encounter]:
    prescription = await db.get(Prescription, prescription_uuid)
    if prescription is None:
        raise HTTPException(status_code=404, detail="Prescription not found.")
    encounter = await db.get(Encounter, prescription.encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    await _scope(db, account, encounter.facility_id)
    return prescription, encounter


def _prescription_item(prescription: Prescription, items: list[PrescriptionItem]) -> dict[str, Any]:
    return {"uuid": str(prescription.id), "encounter_uuid": str(prescription.encounter_id), "status": prescription.status, "advice": prescription.advice, "items": [{"uuid": str(i.id), "medicine_name": i.medicine_name, "dosage": i.dosage, "frequency": i.frequency, "duration": i.duration} for i in items], "signed_at": prescription.signed_at.isoformat() if prescription.signed_at else None}


@router.get("/prescriptions/{prescription_uuid}")
async def get_prescription(prescription_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    prescription, _ = await _prescription(db, account, prescription_uuid)
    items = (await db.scalars(select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id))).all()
    return {"success": True, "data": _prescription_item(prescription, items), "meta": {}}


@router.put("/prescriptions/{prescription_uuid}")
async def update_prescription(prescription_uuid: UUID, payload: PrescriptionInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    prescription, encounter = await _prescription(db, account, prescription_uuid)
    await _practitioner(db, account, encounter)
    if prescription.status != "draft":
        raise HTTPException(status_code=409, detail="Signed prescriptions cannot be edited.")
    prescription.advice = payload.advice
    await db.execute(delete(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id))
    items = [PrescriptionItem(prescription_id=prescription.id, **item.model_dump()) for item in payload.items]
    db.add_all(items)
    await db.commit()
    return {"success": True, "data": _prescription_item(prescription, items), "meta": {}}


@router.post("/prescriptions/{prescription_uuid}/sign")
async def sign_prescription(prescription_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    prescription, encounter = await _prescription(db, account, prescription_uuid)
    await _practitioner(db, account, encounter)
    if prescription.status != "draft":
        raise HTTPException(status_code=409, detail="Prescription is already signed.")
    prescription.status, prescription.signed_by_user_id, prescription.signed_at = "signed", account.id, datetime.now(UTC)
    await record_audit(db, organization_id=encounter.organization_id, actor_user_id=account.id, action="clinical.prescription.signed", resource_type="prescription", resource_id=prescription.id, facility_id=encounter.facility_id, patient_id=encounter.patient_id)
    await db.commit()
    items = (await db.scalars(select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id))).all()
    return {"success": True, "data": _prescription_item(prescription, items), "meta": {}}


@router.get("/prescriptions/{prescription_uuid}/document")
async def prescription_document(prescription_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    prescription, encounter = await _prescription(db, account, prescription_uuid)
    if prescription.status != "signed":
        raise HTTPException(status_code=409, detail="Only signed prescriptions can be rendered.")
    return {"success": True, "data": {"prescription_uuid": str(prescription.id), "encounter_uuid": str(encounter.id), "format": "pdf", "document_status": "available"}, "meta": {}}


@router.post("/encounters/{encounter_uuid}/complete")
async def complete_encounter(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    encounter.status, encounter.completed_at = "completed", datetime.now(UTC)
    if encounter.queue_entry_id:
        queue = await db.get(QueueEntry, encounter.queue_entry_id)
        if queue:
            queue.status = "completed"
    if encounter.appointment_id:
        appointment = await db.get(Appointment, encounter.appointment_id)
        if appointment:
            appointment.status = "completed"
    await db.commit()
    return {"success": True, "data": {"uuid": str(encounter.id), "status": encounter.status, "completed_at": encounter.completed_at.isoformat()}, "meta": {}}
