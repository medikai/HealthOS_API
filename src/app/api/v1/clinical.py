from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, delete, exists, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.events import make_event, publish
from ...domains.governance.audit import record_audit
from ...models.care import (
    Appointment,
    ClinicalDocumentationSetting,
    Diagnosis,
    Encounter,
    Practitioner,
    Prescription,
    PrescriptionItem,
    QueueEntry,
    SoapNote,
)
from ...models.identity import UserAccount
from ...models.organization import Facility, Organization, StaffAssignment, StaffMember
from ...schemas.clinical import (
    DiagnosisInput,
    DocumentationSettingInput,
    DocumentationSettingsResponse,
    PrescriptionInput,
    SoapInput,
    SoapNoteResponse,
)
from .bootstrap import _staff_context
from .scheduling import _scope

router = APIRouter(tags=["clinical"])



def _encounter_access(account_id: UUID):
    return exists(
        select(StaffAssignment.id)
        .select_from(StaffAssignment)
        .join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id)
        .join(Organization, Organization.id == StaffMember.organization_id)
        .join(
            Facility,
            and_(
                Facility.id == Encounter.facility_id,
                Facility.organization_id == Organization.id,
                Facility.is_active.is_(True),
            ),
        )
        .where(
            StaffMember.user_account_id == account_id,
            StaffMember.is_active.is_(True),
            Organization.is_active.is_(True),
            Organization.id == Encounter.organization_id,
            StaffAssignment.is_active.is_(True),
            or_(
                StaffAssignment.role_code == "organization_admin",
                StaffAssignment.facility_id == Encounter.facility_id,
            ),
        )
    )


async def _require_encounter_access(db: AsyncSession, account: UserAccount, allowed: bool) -> None:
    if allowed:
        return
    await _staff_context(db, account)
    raise HTTPException(status_code=403, detail="Facility access is not permitted.")


async def _read_encounter(
    db: AsyncSession,
    account: UserAccount,
    encounter_uuid: UUID,
    *,
    lock: bool = False,
) -> Encounter:
    query = select(
        Encounter, _encounter_access(account.id).label("allowed")
    ).where(Encounter.id == encounter_uuid)
    if lock:
        query = query.with_for_update(of=Encounter)
    row = (await db.execute(query)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    encounter, allowed = row
    await _require_encounter_access(db, account, allowed)
    return encounter


async def _encounter(
    db: AsyncSession,
    account: UserAccount,
    encounter_uuid: UUID,
    *,
    lock: bool = False,
) -> Encounter:
    encounter = await _read_encounter(db, account, encounter_uuid, lock=lock)
    if encounter.status != "in_progress":
        raise HTTPException(status_code=409, detail="Encounter is no longer editable.")
    return encounter


async def _practitioner(db: AsyncSession, account: UserAccount, encounter: Encounter) -> Practitioner:
    practitioner = await db.scalar(select(Practitioner).where(Practitioner.user_account_id == account.id, Practitioner.organization_id == encounter.organization_id, Practitioner.is_active.is_(True)))
    if practitioner is None:
        raise HTTPException(status_code=403, detail="A practitioner account is required for this clinical action.")
    return practitioner


def _soap_item(note: SoapNote) -> dict[str, Any]:
    return {"uuid": str(note.id), "encounter_uuid": str(note.encounter_id), "subjective": note.subjective or "", "objective": note.objective or "", "assessment": note.assessment or "", "plan": note.plan or "", "custom_fields": getattr(note, "custom_fields", None) or {}, "status": note.status, "signed_at": note.signed_at.isoformat() if note.signed_at else None}


@router.get("/encounters/{encounter_uuid}/soap", response_model=SoapNoteResponse)
async def get_soap(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    row = (
        await db.execute(
            select(Encounter, SoapNote, _encounter_access(account.id).label("allowed"))
            .outerjoin(SoapNote, SoapNote.encounter_id == Encounter.id)
            .where(Encounter.id == encounter_uuid)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Encounter not found.")
    encounter, note, allowed = row
    await _require_encounter_access(db, account, allowed)
    data = _soap_item(note) if note else {"uuid": None, "encounter_uuid": str(encounter.id), "subjective": "", "objective": "", "assessment": "", "plan": "", "custom_fields": {}, "status": "draft", "signed_at": None}
    return {"success": True, "data": data, "meta": {}}


@router.put("/encounters/{encounter_uuid}/soap")
@router.post("/encounters/{encounter_uuid}/soap")
async def upsert_soap(
    encounter_uuid: UUID,
    payload: SoapInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    await _practitioner(db, account, encounter)
    note = await db.scalar(select(SoapNote).where(SoapNote.encounter_id == encounter.id))
    if note is None:
        note = SoapNote(encounter_id=encounter.id)
        db.add(note)
    was_signed = note.status == "signed"
    for field in ("subjective", "objective", "assessment", "plan"):
        setattr(note, field, getattr(payload, field))
    if payload.custom_fields is not None:
        note.custom_fields = payload.custom_fields
    if was_signed:
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.soap.amended",
            resource_type="soap_note",
            resource_id=note.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
    await db.commit()
    return {"success": True, "data": _soap_item(note), "meta": {}}


@router.post("/encounters/{encounter_uuid}/soap/sign")
async def sign_soap(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid, lock=True)
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
@router.put("/encounters/{encounter_uuid}/prescriptions")
async def create_prescription(
    encounter_uuid: UUID,
    payload: PrescriptionInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    await _practitioner(db, account, encounter)
    prescription = await db.scalar(
        select(Prescription)
        .where(Prescription.encounter_id == encounter.id)
        .order_by(Prescription.id.desc())
        .limit(1)
    )
    was_signed = False
    if prescription is None:
        prescription = Prescription(encounter_id=encounter.id, advice=payload.advice)
        db.add(prescription)
        await db.flush()
    else:
        was_signed = prescription.status == "signed"
        prescription.advice = payload.advice
        await db.execute(
            delete(PrescriptionItem).where(
                PrescriptionItem.prescription_id == prescription.id
            )
        )
    items = [
        PrescriptionItem(prescription_id=prescription.id, **item.model_dump())
        for item in payload.items
    ]
    db.add_all(items)
    if payload.status == "signed":
        prescription.status, prescription.signed_by_user_id, prescription.signed_at = (
            "signed",
            account.id,
            datetime.now(UTC),
        )
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.prescription.signed",
            resource_type="prescription",
            resource_id=prescription.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
    elif was_signed:
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.prescription.amended",
            resource_type="prescription",
            resource_id=prescription.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
    await db.commit()
    return {"success": True, "data": _prescription_item(prescription, items), "meta": {}}


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
    medications = [{"uuid": str(i.id), "id": str(i.id), "medicine_name": i.medicine_name, "name": i.medicine_name, "dosage": i.dosage, "dose": i.dosage, "frequency": i.frequency, "duration": i.duration, "strength": i.strength, "brand": i.brand, "route": i.route, "timing": i.timing, "instructions": i.instructions} for i in items]
    return {"uuid": str(prescription.id), "encounter_uuid": str(prescription.encounter_id), "status": prescription.status, "advice": prescription.advice, "items": medications, "medications": medications, "signed_at": prescription.signed_at.isoformat() if prescription.signed_at else None}


@router.get("/encounters/{encounter_uuid}/prescriptions")
async def get_encounter_prescription(encounter_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    latest_prescription_id = (
        select(Prescription.id)
        .where(Prescription.encounter_id == encounter.id)
        .order_by(Prescription.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(Prescription, PrescriptionItem)
            .select_from(Encounter)
            .outerjoin(Prescription, Prescription.id == latest_prescription_id)
            .outerjoin(PrescriptionItem, PrescriptionItem.prescription_id == Prescription.id)
            .where(Encounter.id == encounter.id)
        )
    ).all()
    prescription = rows[0][0]
    if prescription is None:
        return {"success": True, "data": {"uuid": None, "encounter_uuid": str(encounter.id), "status": "draft", "advice": None, "items": [], "medications": [], "signed_at": None}, "meta": {}}
    items = [item for _, item in rows if item is not None]
    return {"success": True, "data": _prescription_item(prescription, items), "meta": {}}


@router.get("/prescriptions/{prescription_uuid}")
async def get_prescription(prescription_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    prescription, _ = await _prescription(db, account, prescription_uuid)
    items = (await db.scalars(select(PrescriptionItem).where(PrescriptionItem.prescription_id == prescription.id))).all()
    return {"success": True, "data": _prescription_item(prescription, items), "meta": {}}


@router.put("/prescriptions/{prescription_uuid}")
async def update_prescription(
    prescription_uuid: UUID,
    payload: PrescriptionInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    prescription, encounter = await _prescription(db, account, prescription_uuid)
    await _practitioner(db, account, encounter)
    was_signed = prescription.status == "signed"
    prescription.advice = payload.advice
    await db.execute(
        delete(PrescriptionItem).where(
            PrescriptionItem.prescription_id == prescription.id
        )
    )
    items = [
        PrescriptionItem(prescription_id=prescription.id, **item.model_dump())
        for item in payload.items
    ]
    db.add_all(items)
    if payload.status == "signed":
        prescription.status, prescription.signed_by_user_id, prescription.signed_at = (
            "signed",
            account.id,
            datetime.now(UTC),
        )
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.prescription.signed",
            resource_type="prescription",
            resource_id=prescription.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
    elif was_signed:
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.prescription.amended",
            resource_type="prescription",
            resource_id=prescription.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
    await db.commit()
    return {"success": True, "data": _prescription_item(prescription, items), "meta": {}}


@router.post("/prescriptions/{prescription_uuid}/sign")
async def sign_prescription(
    prescription_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    prescription, encounter = await _prescription(db, account, prescription_uuid)
    await _practitioner(db, account, encounter)
    prescription.status, prescription.signed_by_user_id, prescription.signed_at = (
        "signed",
        account.id,
        datetime.now(UTC),
    )
    await record_audit(
        db,
        organization_id=encounter.organization_id,
        actor_user_id=account.id,
        action="clinical.prescription.signed",
        resource_type="prescription",
        resource_id=prescription.id,
        facility_id=encounter.facility_id,
        patient_id=encounter.patient_id,
    )
    await db.commit()
    items = (
        await db.scalars(
            select(PrescriptionItem).where(
                PrescriptionItem.prescription_id == prescription.id
            )
        )
    ).all()
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
            old_appointment_status = appointment.status
            appointment.version += 1
            appointment.status = "completed"
    await db.commit()
    if encounter.appointment_id and appointment:
        await publish(make_event(
            "appointment.status_changed", entity_id=appointment.id, entity_version=appointment.version,
            organization_id=appointment.organization_id, facility_id=appointment.facility_id,
            practitioner_id=appointment.practitioner_id,
            old={"status": old_appointment_status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
            new={"status": appointment.status, "date": appointment.scheduled_start.date(), "resource_id": appointment.resource_id},
        ))
    if encounter.queue_entry_id and queue:
        await publish(make_event(
            "queue.changed", entity_id=queue.id, entity_version=None,
            organization_id=queue.organization_id, facility_id=queue.facility_id,
            practitioner_id=queue.practitioner_id,
            new={"date": queue.queue_date, "status": queue.status},
        ))
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
    source: str = "default",
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
        vitals_config = _merge_config(defaults["vitals_config"], setting.vitals_config)
        soap_config = _merge_config(defaults["soap_config"], setting.soap_config)
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
        "source": source,
        "triage_expanded_default": triage_expanded_default,
        "vitals_config": vitals_config,
        "soap_config": soap_config,
        "custom_sections": custom_sections,
        "active_modifications_count": len(summary),
        "summary": summary,
    }


def _merge_config(defaults: dict[str, Any], overrides: Any) -> dict[str, Any]:
    result = {key: value.copy() if isinstance(value, dict) else value for key, value in defaults.items()}
    if not isinstance(overrides, dict):
        return result
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_config(result[key], value)
        else:
            result[key] = value
    return result


def _facility_id(value: UUID | str | None) -> UUID | None:
    return None if value in (None, "all") else UUID(str(value))


async def _clinical_settings_scope(
    db: AsyncSession,
    account: UserAccount,
    facility_id: UUID | None,
    *,
    write: bool,
) -> UUID:
    query = (
        select(
            Organization.id,
            StaffAssignment.role_code,
            StaffAssignment.facility_id,
        )
        .select_from(StaffMember)
        .join(Organization, Organization.id == StaffMember.organization_id)
        .outerjoin(
            StaffAssignment,
            and_(
                StaffAssignment.staff_member_id == StaffMember.id,
                StaffAssignment.is_active.is_(True),
            ),
        )
        .where(
            StaffMember.user_account_id == account.id,
            StaffMember.is_active.is_(True),
            Organization.is_active.is_(True),
        )
    )
    if facility_id is None:
        query = query.add_columns(literal(None).label("target_facility_id"))
    else:
        query = query.add_columns(Facility.id.label("target_facility_id")).outerjoin(
            Facility,
            and_(
                Facility.id == facility_id,
                Facility.organization_id == Organization.id,
                Facility.is_active.is_(True),
            ),
        )

    rows = (await db.execute(query)).all()
    if not rows:
        raise HTTPException(status_code=403, detail="No active HealthOS organization access.")

    organization_id = rows[0][0]
    roles = {row[1] for row in rows if row[1]}
    is_admin = bool(roles & {"organization_admin", "owner", "administrator"})

    if facility_id is None:
        if write and not is_admin:
            raise HTTPException(status_code=403, detail="Organization-wide settings require administrator access.")
        return organization_id

    if not any(row[3] is not None for row in rows):
        raise HTTPException(status_code=404, detail="Facility not found.")
    if not is_admin and not any(row[2] == facility_id for row in rows):
        raise HTTPException(status_code=403, detail="Facility access is not permitted.")
    if write and not roles & {"organization_admin", "owner", "administrator", "practitioner", "doctor"}:
        raise HTTPException(status_code=403, detail="Clinical settings require administrator or practitioner access.")
    return organization_id


async def _effective_clinical_setting(
    db: AsyncSession,
    organization_id: UUID,
    facility_id: UUID | None,
) -> tuple[ClinicalDocumentationSetting | None, str]:
    query = select(ClinicalDocumentationSetting).where(
        ClinicalDocumentationSetting.organization_id == organization_id
    )
    if facility_id is None:
        query = query.where(ClinicalDocumentationSetting.facility_id.is_(None))
    else:
        query = query.where(
            or_(
                ClinicalDocumentationSetting.facility_id == facility_id,
                ClinicalDocumentationSetting.facility_id.is_(None),
            )
        ).order_by(
            case(
                (ClinicalDocumentationSetting.facility_id == facility_id, 0),
                else_=1,
            )
        )
    setting = await db.scalar(query.limit(1))
    if setting is None:
        return None, "default"
    return setting, "facility" if setting.facility_id == facility_id and facility_id else "organization"


@router.get("/clinical/documentation-settings", response_model=DocumentationSettingsResponse)
@router.get("/clinical/settings", response_model=DocumentationSettingsResponse)
async def get_clinical_documentation_settings(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: Annotated[UUID | Literal["all"] | None, Query()] = None,
) -> dict[str, Any]:
    target_facility_id = _facility_id(facility_uuid)
    organization_id = await _clinical_settings_scope(db, account, target_facility_id, write=False)
    setting, source = await _effective_clinical_setting(db, organization_id, target_facility_id)

    return {
        "success": True,
        "data": _format_clinical_setting_response(setting, organization_id, target_facility_id, source),
        "meta": {},
    }


@router.put("/clinical/documentation-settings", response_model=DocumentationSettingsResponse)
@router.put("/clinical/settings", response_model=DocumentationSettingsResponse)
async def update_clinical_documentation_settings(
    payload: DocumentationSettingInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    target_facility_id = _facility_id(payload.facility_uuid)
    organization_id = await _clinical_settings_scope(db, account, target_facility_id, write=True)

    query = select(ClinicalDocumentationSetting).where(
        ClinicalDocumentationSetting.organization_id == organization_id
    )
    if target_facility_id:
        query = query.where(ClinicalDocumentationSetting.facility_id == target_facility_id)
    else:
        query = query.where(ClinicalDocumentationSetting.facility_id.is_(None))

    values = payload.model_dump(mode="json", exclude_none=True)
    defaults = _get_default_clinical_settings()
    vitals_config = _merge_config(defaults["vitals_config"], values["vitals_config"])
    soap_config = _merge_config(defaults["soap_config"], values["soap_config"])
    custom_sections = values["custom_sections"]

    setting = await db.scalar(query)
    if setting is None:
        setting = ClinicalDocumentationSetting(
            organization_id=organization_id,
            facility_id=target_facility_id,
            triage_expanded_default=payload.triage_expanded_default,
            vitals_config=vitals_config,
            soap_config=soap_config,
            custom_sections=custom_sections,
        )
        db.add(setting)
    else:
        setting.triage_expanded_default = payload.triage_expanded_default
        setting.vitals_config = vitals_config
        setting.soap_config = soap_config
        setting.custom_sections = custom_sections
        setting.updated_at = datetime.now(UTC)

    await record_audit(db, organization_id=organization_id, actor_user_id=account.id, action="clinical.documentation_settings.updated", resource_type="clinical_documentation_setting", resource_id=setting.id, facility_id=target_facility_id)
    await db.commit()
    await db.refresh(setting)

    return {
        "success": True,
        "data": _format_clinical_setting_response(setting, organization_id, target_facility_id, "facility" if target_facility_id else "organization"),
        "meta": {},
    }


@router.post("/clinical/documentation-settings/reset", response_model=DocumentationSettingsResponse)
@router.post("/clinical/settings/reset", response_model=DocumentationSettingsResponse)
async def reset_clinical_documentation_settings(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: Annotated[UUID | Literal["all"] | None, Query()] = None,
) -> dict[str, Any]:
    target_facility_id = _facility_id(facility_uuid)
    organization_id = await _clinical_settings_scope(db, account, target_facility_id, write=True)

    query = select(ClinicalDocumentationSetting).where(
        ClinicalDocumentationSetting.organization_id == organization_id
    )
    if target_facility_id:
        query = query.where(ClinicalDocumentationSetting.facility_id == target_facility_id)
    else:
        query = query.where(ClinicalDocumentationSetting.facility_id.is_(None))

    setting = await db.scalar(query)
    if setting is not None:
        await record_audit(db, organization_id=organization_id, actor_user_id=account.id, action="clinical.documentation_settings.reset", resource_type="clinical_documentation_setting", resource_id=setting.id, facility_id=target_facility_id)
        await db.delete(setting)
        await db.commit()

    effective, source = await _effective_clinical_setting(db, organization_id, target_facility_id)

    return {
        "success": True,
        "data": _format_clinical_setting_response(effective, organization_id, target_facility_id, source),
        "meta": {},
    }
