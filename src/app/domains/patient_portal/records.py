"""Explicit record-release metadata and patient-facing snapshot builders.

Release snapshots freeze exactly what the patient may see. Later clinical
amendments never silently change a released version; re-release creates a new
version after revocation. Signing or document availability alone is never
treated as release authorization.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.simple_pdf import render_text_pdf
from ...models.care import (
    Diagnosis,
    Encounter,
    PatientDocument,
    Practitioner,
    Prescription,
    PrescriptionItem,
    RecordRelease,
    Vital,
)
from ...models.identity import Patient, Person
from ...models.organization import Facility
from ..governance.audit import record_audit

RESOURCE_TYPES = ("encounter_summary", "vitals", "prescription", "patient_document")


class RecordReleaseError(RuntimeError):
    code = "RECORD_RELEASE_ERROR"


class ReleaseNotFound(RecordReleaseError):
    code = "NOT_FOUND"


class NotReleased(RecordReleaseError):
    code = "RECORD_NOT_RELEASED"


class ActiveReleaseExists(RecordReleaseError):
    code = "RELEASE_ALREADY_ACTIVE"


class ReleaseNotActive(RecordReleaseError):
    code = "RELEASE_NOT_ACTIVE"


def _name(person: Person | None) -> str | None:
    if person is None:
        return None
    return f"{person.first_name} {person.last_name or ''}".strip()


async def create_release(
    db: AsyncSession,
    *,
    actor_user_id: UUID,
    organization_id: UUID,
    facility_id: UUID,
    patient_id: UUID,
    encounter_id: UUID | None,
    resource_type: str,
    resource_id: UUID,
    snapshot: dict[str, Any],
) -> RecordRelease:
    if resource_type not in RESOURCE_TYPES:
        raise ReleaseNotFound("Unsupported release type.")
    active = await db.scalar(
        select(RecordRelease).where(
            RecordRelease.resource_type == resource_type,
            RecordRelease.resource_id == resource_id,
            RecordRelease.revoked_at.is_(None),
        )
    )
    if active is not None:
        raise ActiveReleaseExists(
            "An active release exists for this record. Revoke it before releasing a new version."
        )
    previous_version = await db.scalar(
        select(func.coalesce(func.max(RecordRelease.version), 0)).where(
            RecordRelease.resource_type == resource_type,
            RecordRelease.resource_id == resource_id,
        )
    )
    release = RecordRelease(
        organization_id=organization_id,
        facility_id=facility_id,
        patient_id=patient_id,
        encounter_id=encounter_id,
        resource_type=resource_type,
        resource_id=resource_id,
        version=int(previous_version or 0) + 1,
        snapshot=snapshot,
        released_by_user_id=actor_user_id,
    )
    db.add(release)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ActiveReleaseExists("An active release already exists.") from exc
    await record_audit(
        db,
        organization_id=organization_id,
        facility_id=facility_id,
        actor_user_id=actor_user_id,
        action="patient.record_released",
        resource_type=resource_type,
        resource_id=resource_id,
        patient_id=patient_id,
        details={"release_id": str(release.id), "version": release.version},
    )
    return release


async def revoke_release(
    db: AsyncSession,
    *,
    release: RecordRelease,
    actor_user_id: UUID,
    reason: str | None,
) -> RecordRelease:
    if release.revoked_at is not None:
        raise ReleaseNotActive("This release is already revoked.")
    release.revoked_at = datetime.now(UTC)
    release.revoked_by_user_id = actor_user_id
    release.revoke_reason = reason
    release.updated_at = release.revoked_at
    await record_audit(
        db,
        organization_id=release.organization_id,
        facility_id=release.facility_id,
        actor_user_id=actor_user_id,
        action="patient.record_release_revoked",
        resource_type=release.resource_type,
        resource_id=release.resource_id,
        patient_id=release.patient_id,
        details={"release_id": str(release.id), "reason": reason},
    )
    return release


async def active_release(
    db: AsyncSession, *, resource_type: str, resource_id: UUID
) -> RecordRelease | None:
    return await db.scalar(
        select(RecordRelease).where(
            RecordRelease.resource_type == resource_type,
            RecordRelease.resource_id == resource_id,
            RecordRelease.revoked_at.is_(None),
        )
    )


async def releases_for_patient(
    db: AsyncSession,
    *,
    patient_id: UUID,
    resource_type: str | None = None,
) -> list[RecordRelease]:
    query = select(RecordRelease).where(
        RecordRelease.patient_id == patient_id,
        RecordRelease.revoked_at.is_(None),
    )
    if resource_type:
        query = query.where(RecordRelease.resource_type == resource_type)
    return list(
        (await db.scalars(query.order_by(RecordRelease.released_at.desc()))).all()
    )


async def build_encounter_summary_snapshot(
    db: AsyncSession,
    *,
    encounter: Encounter,
    summary: str,
    allow_vitals: bool,
) -> dict[str, Any]:
    facility = await db.get(Facility, encounter.facility_id)
    practitioner = (
        await db.get(Practitioner, encounter.practitioner_id)
        if encounter.practitioner_id
        else None
    )
    diagnoses = (
        await db.scalars(
            select(Diagnosis).where(Diagnosis.encounter_id == encounter.id)
        )
    ).all()
    snapshot: dict[str, Any] = {
        "summary": summary.strip()[:4000],
        "summary_note": "Summary authored and released by the treating clinic.",
        "started_at": encounter.started_at.isoformat() if encounter.started_at else None,
        "completed_at": encounter.completed_at.isoformat() if encounter.completed_at else None,
        "facility_name": facility.name if facility else None,
        "practitioner_name": practitioner.person_name if practitioner else None,
        "diagnoses": [
            {"code": d.code, "description": d.description, "is_primary": bool(d.is_primary)}
            for d in diagnoses
        ],
    }
    if allow_vitals:
        snapshot["vitals"] = await _vitals_payload(db, encounter.id)
    return snapshot


async def _vitals_payload(db: AsyncSession, encounter_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(
            select(Vital)
            .where(Vital.encounter_id == encounter_id)
            .order_by(Vital.recorded_at.asc())
        )
    ).all()
    grouped: dict[str, dict[str, Any]] = {}
    for vital in rows:
        key = str(vital.recording_id) if vital.recording_id else f"single:{vital.id}"
        group = grouped.setdefault(
            key,
            {
                "recording_id": key,
                "recorded_at": vital.recorded_at.isoformat() if vital.recorded_at else None,
                "measurements": [],
            },
        )
        group["measurements"].append(
            {
                "name": vital.name,
                "value": vital.value,
                "unit": vital.unit,
                "recorded_at": vital.recorded_at.isoformat() if vital.recorded_at else None,
            }
        )
    return list(grouped.values())


async def build_vitals_snapshot(db: AsyncSession, *, encounter: Encounter) -> dict[str, Any]:
    return {
        "vitals": await _vitals_payload(db, encounter.id),
        "note": "Recorded vitals only; values are shown as entered by clinical staff.",
    }


async def build_prescription_snapshot(
    db: AsyncSession, *, prescription: Prescription
) -> dict[str, Any]:
    if prescription.status != "signed":
        raise NotReleased("Only signed prescriptions can be released.")
    items = (
        await db.scalars(
            select(PrescriptionItem).where(
                PrescriptionItem.prescription_id == prescription.id
            )
        )
    ).all()
    encounter = await db.get(Encounter, prescription.encounter_id)
    facility = await db.get(Facility, encounter.facility_id) if encounter else None
    practitioner = (
        await db.get(Practitioner, encounter.practitioner_id)
        if encounter and encounter.practitioner_id
        else None
    )
    return {
        "advice": prescription.advice,
        "signed_at": prescription.signed_at.isoformat() if prescription.signed_at else None,
        "signing_note": (
            "Clinical signing metadata is recorded in MedikAI; this is not a "
            "cryptographic signature."
        ),
        "facility_name": facility.name if facility else None,
        "practitioner_name": practitioner.person_name if practitioner else None,
        "items": [
            {
                "medicine_name": item.medicine_name,
                "strength": item.strength,
                "dosage": item.dosage,
                "frequency": item.frequency,
                "duration": item.duration,
                "route": item.route,
                "timing": item.timing,
                "instructions": item.instructions,
            }
            for item in items
        ],
    }


async def build_document_snapshot(
    db: AsyncSession, *, document: PatientDocument
) -> dict[str, Any]:
    return {
        "title": document.title,
        "category": document.category,
        "document_date": document.document_date.isoformat() if document.document_date else None,
        "original_filename": document.original_filename,
        "mime_type": document.mime_type or document.declared_mime_type,
        "size_bytes": document.size_bytes or document.expected_size_bytes,
    }


async def patient_identity_for_release(
    db: AsyncSession, *, patient_id: UUID
) -> dict[str, Any]:
    patient = await db.get(Patient, patient_id)
    person = await db.get(Person, patient.person_id) if patient else None
    return {
        "mrn": patient.mrn if patient else None,
        "display_name": _name(person),
        "date_of_birth": person.date_of_birth if person else None,
    }


def render_prescription_pdf(
    *,
    snapshot: dict[str, Any],
    patient: dict[str, Any],
) -> bytes:
    sections: list[tuple[str, list[str]]] = []
    if patient.get("display_name"):
        sections.append(("Patient", [f"Name: {patient['display_name']}"]))
    if patient.get("mrn"):
        sections.append(("Medical record", [f"MRN: {patient['mrn']}"]))
    if snapshot.get("facility_name"):
        sections.append(("Clinic", [f"Facility: {snapshot['facility_name']}"]))
    if snapshot.get("practitioner_name"):
        sections.append(("Prescriber", [f"Practitioner: {snapshot['practitioner_name']}"]))
    items = snapshot.get("items") or []
    sections.append(
        (
            "Prescription",
            [
                f"{index + 1}. {item.get('medicine_name') or ''}"
                f"{' ' + item['strength'] if item.get('strength') else ''}"
                f" | {item.get('dosage') or ''} | {item.get('frequency') or ''}"
                f" | {item.get('duration') or ''}"
                for index, item in enumerate(items)
            ]
            or ["No medication items recorded."],
        )
    )
    if snapshot.get("advice"):
        sections.append(("Advice", [snapshot["advice"]]))
    return render_text_pdf(
        title="Prescription",
        subtitle="MedikAI patient portal copy",
        sections=sections,
        footer_note=(
            snapshot.get("signing_note")
            or "Clinical signing metadata is recorded in MedikAI; not a cryptographic signature."
        ),
    )
