"""Staff-authorized patient record release/revoke.

A release freezes an allowlisted patient-facing snapshot. Signing or document
availability alone is never release authorization. Roles and facility scope are
enforced here; legacy staff endpoints stay untouched and staff-restricted.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...domains.patient_portal import records as record_service
from ...domains.patient_portal.notifications import create_patient_notification
from ...models.care import Encounter, PatientDocument, Prescription, RecordRelease
from ...models.identity import UserAccount
from ...models.organization import Organization, StaffAssignment
from ...schemas.patient_domain import RecordReleaseBody, RecordReleaseRevokeBody
from .bootstrap import ADMIN_ROLES, _staff_context
from .scheduling import _scope

router = APIRouter(prefix="/record-releases", tags=["record-releases"])

RELEASE_ROLES = set(ADMIN_ROLES) | {
    "practitioner",
    "doctor",
    "clinical_practitioner",
    "nurse",
}

async def _release_context(
    db: AsyncSession, account: UserAccount, facility_uuid: UUID | None = None
) -> tuple[Any, Organization, Any]:
    staff, organization = await _staff_context(db, account)
    roles = set(
        (
            await db.scalars(
                select(StaffAssignment.role_code).where(
                    StaffAssignment.staff_member_id == staff.id,
                    StaffAssignment.is_active.is_(True),
                )
            )
        ).all()
    )
    if not roles & RELEASE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Record release access is required.",
        )
    facility = None
    if facility_uuid is not None:
        _, facility = await _scope(db, account, facility_uuid)
        if facility is None or facility.organization_id != organization.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Facility access is not permitted.",
            )
    return staff, organization, facility


def _translate(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (record_service.ActiveReleaseExists, record_service.ReleaseNotActive, record_service.NotReleased),
    ):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(exc, record_service.RecordReleaseError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    raise exc


def _release_payload(release: RecordRelease) -> dict[str, Any]:
    return {
        "uuid": str(release.id),
        "resource_type": release.resource_type,
        "resource_uuid": str(release.resource_id),
        "patient_uuid": str(release.patient_id),
        "organization_uuid": str(release.organization_id),
        "facility_uuid": str(release.facility_id),
        "version": release.version,
        "released_at": release.released_at.isoformat(),
        "revoked_at": release.revoked_at.isoformat() if release.revoked_at else None,
    }


@router.get("")
async def list_releases(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    patient_uuid: UUID | None = None,
    resource_type: str | None = Query(default=None, pattern="^(encounter_summary|vitals|prescription|patient_document)$"),
    status_filter: str = Query(default="active", pattern="^(active|revoked|all)$"),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    _, organization, _ = await _release_context(db, account)
    query = select(RecordRelease).where(RecordRelease.organization_id == organization.id)
    if patient_uuid:
        query = query.where(RecordRelease.patient_id == patient_uuid)
    if resource_type:
        query = query.where(RecordRelease.resource_type == resource_type)
    if status_filter == "active":
        query = query.where(RecordRelease.revoked_at.is_(None))
    elif status_filter == "revoked":
        query = query.where(RecordRelease.revoked_at.is_not(None))
    releases = (
        await db.scalars(query.order_by(RecordRelease.released_at.desc()).limit(limit))
    ).all()
    return {
        "success": True,
        "data": {"items": [_release_payload(r) for r in releases]},
        "meta": {"count": len(releases)},
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_release(
    payload: RecordReleaseBody,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    if payload.resource_type == "encounter_summary" and not (payload.summary or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": "summary is required for encounter_summary releases.", "details": [{"field": "summary"}]},
        )
    resource_type = payload.resource_type
    resource = await db.get(
        {
            "encounter_summary": Encounter,
            "vitals": Encounter,
            "prescription": Prescription,
            "patient_document": PatientDocument,
        }[resource_type],
        payload.resource_uuid,
    )
    if resource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Record not found.", "details": []},
        )
    encounter: Encounter | None
    if resource_type in ("encounter_summary", "vitals"):
        encounter = resource  # type: ignore[assignment]
    elif resource_type == "prescription":
        encounter = await db.get(Encounter, resource.encounter_id)  # type: ignore[union-attr]
    else:
        encounter = await db.get(Encounter, resource.encounter_id)  # type: ignore[union-attr]
    if encounter is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Encounter not found.", "details": []},
        )
    staff, organization, _ = await _release_context(db, account, encounter.facility_id)
    if encounter.organization_id != organization.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Record not found.", "details": []},
        )
    if resource_type in ("encounter_summary", "vitals") and encounter.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ENCOUNTER_NOT_COMPLETED", "message": "Only completed encounters can be released.", "details": []},
        )
    if resource_type == "prescription":
        prescription = resource  # type: ignore[assignment]
        if prescription.status != "signed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "PRESCRIPTION_NOT_SIGNED", "message": "Only signed prescriptions can be released.", "details": []},
            )
        snapshot = await record_service.build_prescription_snapshot(db, prescription=prescription)
    elif resource_type == "patient_document":
        document = resource  # type: ignore[assignment]
        if document.status != "available" or document.deleted_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "DOCUMENT_NOT_AVAILABLE", "message": "Only available documents can be released.", "details": []},
            )
        snapshot = await record_service.build_document_snapshot(db, document=document)
    elif resource_type == "vitals":
        snapshot = await record_service.build_vitals_snapshot(db, encounter=encounter)
    else:
        snapshot = await record_service.build_encounter_summary_snapshot(
            db,
            encounter=encounter,
            summary=payload.summary or "",
            allow_vitals=payload.include_vitals,
        )
    try:
        release = await record_service.create_release(
            db,
            actor_user_id=staff.user_account_id,
            organization_id=organization.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
            encounter_id=encounter.id,
            resource_type=resource_type,
            resource_id=payload.resource_uuid,
            snapshot=snapshot,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    from ...models.identity import PatientRecordLink

    link = await db.scalar(
        select(PatientRecordLink).where(
            PatientRecordLink.patient_id == encounter.patient_id,
            PatientRecordLink.status == "verified",
        )
    )
    if link is not None:
        await create_patient_notification(
            db,
            account_id=link.patient_account_id,
            kind="record_released",
            category="records",
            title="New record available",
            body="A clinic released a record to your portal.",
            organization_id=organization.id,
            facility_id=encounter.facility_id,
            deep_link="/records",
            dedup_key=f"patient:release:{release.id}",
        )
    await db.commit()
    await db.refresh(release)
    return {"success": True, "data": _release_payload(release), "meta": {}}


@router.post("/{release_uuid}/revoke")
async def revoke_release(
    release_uuid: UUID,
    payload: RecordReleaseRevokeBody,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, _ = await _release_context(db, account)
    release = await db.scalar(
        select(RecordRelease).where(
            RecordRelease.id == release_uuid,
            RecordRelease.organization_id == organization.id,
        )
    )
    if release is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Release not found.", "details": []},
        )
    try:
        release = await record_service.revoke_release(
            db,
            release=release,
            actor_user_id=staff.user_account_id,
            reason=payload.reason,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    await db.commit()
    await db.refresh(release)
    return {"success": True, "data": _release_payload(release), "meta": {}}
