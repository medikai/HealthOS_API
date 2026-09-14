from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import (
    Appointment, ClinicalDocumentationSetting, Diagnosis, Encounter, Prescription,
    PrescriptionItem, Practitioner, QueueEntry, SoapNote,
)
from ...models.identity import UserAccount
from ...schemas.clinical import (
    DiagnosisInput, DocumentationSettingInput, DocumentationSettingResponse,
    PrescriptionInput, SoapInput,
)
from ...domains.governance.audit import record_audit
from .bootstrap import _staff_context
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


def _get_default_clinical_settings() -> dict[str, Any]:
    return {
        "triage_expanded_default": True,
        "vitals_config": {
            "blood_pressure": {"visible": True, "label": "Blood Pressure (BP)", "unit": "mmHg"},
            "pulse_rate": {"visible": True, "label": "Pulse Rate", "unit": "bpm"},
            "spo2": {"visible": True, "label": "Oxygen Saturation (SpO₂)", "unit": "%"},
            "respiratory_rate": {"visible": False, "label": "Respiratory Rate (RR)", "unit": "rpm"},
            "temperature": {"visible": True, "label": "Temperature", "unit": "°C"},
            "height": {"visible": True, "label": "Height", "unit": "cm"},
            "weight": {"visible": True, "label": "Weight", "unit": "kg"},
            "bmi": {"visible": True, "label": "Calculated BMI", "unit": "kg/m²"},
        },
        "soap_config": {
            "subjective": {
                "general_notes": {"visible": True, "mandatory": False, "default_state": "expanded"},
                "chief_complaints": {"visible": True, "mandatory": True, "default_state": "expanded"},
                "hpi": {"visible": True, "mandatory": False, "default_state": "expanded"},
                "family_social": {"visible": True, "mandatory": False, "default_state": "collapsed"},
            },
            "objective": {
                "general_exam": {"visible": True, "mandatory": False, "default_state": "expanded"},
                "systemic_exam": {"visible": True, "mandatory": False, "default_state": "collapsed"},
                "additional_obs": {"visible": True, "mandatory": False, "default_state": "collapsed"},
            },
            "assessment": {
                "diagnoses": {"visible": True, "mandatory": True, "default_state": "expanded"},
            },
            "plan": {
                "general_plan": {"visible": True, "mandatory": False, "default_state": "expanded"},
                "prescription_summary": {"visible": True, "mandatory": False, "default_state": "expanded"},
            },
        },
        "custom_sections": [
            {
                "key": "lifestyle_notes",
                "label": "Lifestyle notes",
                "group": "plan",
                "helper_text": "Dietary habits, physical activity, and stress management guidance",
                "required": False,
                "expanded": False,
            }
        ],
    }


def _format_clinical_setting_response(
    setting: ClinicalDocumentationSetting | None,
    organization_id: UUID,
    facility_id: UUID | None,
) -> dict[str, Any]:
    defaults = _get_default_clinical_settings()
    if setting is None:
        triage_expanded_default = defaults["triage_expanded_default"]
        vitals_config = defaults["vitals_config"]
        soap_config = defaults["soap_config"]
        custom_sections = defaults["custom_sections"]
        setting_id = None
    else:
        setting_id = str(setting.id)
        triage_expanded_default = setting.triage_expanded_default
        vitals_config = setting.vitals_config if setting.vitals_config else defaults["vitals_config"]
        soap_config = setting.soap_config if setting.soap_config else defaults["soap_config"]
        custom_sections = setting.custom_sections if setting.custom_sections is not None else defaults["custom_sections"]

    summary: list[str] = []
    rr_cfg = vitals_config.get("respiratory_rate", {})
    if isinstance(rr_cfg, dict) and not rr_cfg.get("visible", False):
        summary.append("Respiratory Rate (RR) toggled OFF / omitted from triage")

    subj = soap_config.get("subjective", {})
    if isinstance(subj, dict):
        fs = subj.get("family_social", {})
        if isinstance(fs, dict) and fs.get("visible", False):
            summary.append("Family & Social History enabled in Subjective")

    obj = soap_config.get("objective", {})
    if isinstance(obj, dict):
        se = obj.get("systemic_exam", {})
        if isinstance(se, dict) and se.get("default_state") == "collapsed":
            summary.append("Systemic Examination defaulted to Collapsed state")

    for cs in custom_sections:
        if isinstance(cs, dict) and cs.get("label"):
            group_name = cs.get("group", "plan").capitalize()
            summary.append(f"Custom section '{cs.get('label')}' configured under {group_name}")

    return {
        "id": setting_id,
        "organization_id": str(organization_id),
        "facility_id": str(facility_id) if facility_id else None,
        "triage_expanded_default": triage_expanded_default,
        "vitals_config": vitals_config,
        "soap_config": soap_config,
        "custom_sections": custom_sections,
        "active_modifications_count": len(summary),
        "summary": summary,
    }


@router.get("/clinical/documentation-settings")
@router.get("/clinical/settings")
async def get_clinical_documentation_settings(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: str | None = Query(default=None),
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)

    target_facility_id: UUID | None = None
    if facility_uuid and facility_uuid != "all":
        try:
            target_facility_id = UUID(str(facility_uuid))
        except (ValueError, TypeError):
            pass

    setting = None
    if target_facility_id:
        setting = await db.scalar(
            select(ClinicalDocumentationSetting).where(
                ClinicalDocumentationSetting.organization_id == organization.id,
                ClinicalDocumentationSetting.facility_id == target_facility_id,
            )
        )

    if setting is None:
        setting = await db.scalar(
            select(ClinicalDocumentationSetting).where(
                ClinicalDocumentationSetting.organization_id == organization.id,
                ClinicalDocumentationSetting.facility_id.is_(None),
            )
        )

    return {
        "success": True,
        "data": _format_clinical_setting_response(setting, organization.id, target_facility_id),
        "meta": {},
    }


@router.put("/clinical/documentation-settings")
@router.put("/clinical/settings")
async def update_clinical_documentation_settings(
    payload: DocumentationSettingInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)

    target_facility_id: UUID | None = None
    if payload.facility_uuid and payload.facility_uuid != "all":
        try:
            target_facility_id = UUID(str(payload.facility_uuid))
        except (ValueError, TypeError):
            pass

    query = select(ClinicalDocumentationSetting).where(
        ClinicalDocumentationSetting.organization_id == organization.id
    )
    if target_facility_id:
        query = query.where(ClinicalDocumentationSetting.facility_id == target_facility_id)
    else:
        query = query.where(ClinicalDocumentationSetting.facility_id.is_(None))

    setting = await db.scalar(query)
    if setting is None:
        setting = ClinicalDocumentationSetting(
            organization_id=organization.id,
            facility_id=target_facility_id,
            triage_expanded_default=payload.triage_expanded_default,
            vitals_config=payload.vitals_config,
            soap_config=payload.soap_config,
            custom_sections=[cs.model_dump() for cs in payload.custom_sections],
        )
        db.add(setting)
    else:
        setting.triage_expanded_default = payload.triage_expanded_default
        setting.vitals_config = payload.vitals_config
        setting.soap_config = payload.soap_config
        setting.custom_sections = [cs.model_dump() for cs in payload.custom_sections]
        setting.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(setting)

    return {
        "success": True,
        "data": _format_clinical_setting_response(setting, organization.id, target_facility_id),
        "meta": {},
    }


@router.post("/clinical/documentation-settings/reset")
@router.post("/clinical/settings/reset")
async def reset_clinical_documentation_settings(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: str | None = Query(default=None),
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)

    target_facility_id: UUID | None = None
    if facility_uuid and facility_uuid != "all":
        try:
            target_facility_id = UUID(str(facility_uuid))
        except (ValueError, TypeError):
            pass

    query = select(ClinicalDocumentationSetting).where(
        ClinicalDocumentationSetting.organization_id == organization.id
    )
    if target_facility_id:
        query = query.where(ClinicalDocumentationSetting.facility_id == target_facility_id)
    else:
        query = query.where(ClinicalDocumentationSetting.facility_id.is_(None))

    setting = await db.scalar(query)
    defaults = _get_default_clinical_settings()

    if setting is not None:
        setting.triage_expanded_default = defaults["triage_expanded_default"]
        setting.vitals_config = defaults["vitals_config"]
        setting.soap_config = defaults["soap_config"]
        setting.custom_sections = defaults["custom_sections"]
        setting.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(setting)

    return {
        "success": True,
        "data": _format_clinical_setting_response(setting, organization.id, target_facility_id),
        "meta": {},
    }

