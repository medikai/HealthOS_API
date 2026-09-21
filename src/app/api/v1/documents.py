import hashlib
import hmac
import logging
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import PurePath
from time import time
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from google.api_core import exceptions as google_exceptions
from google.auth import exceptions as google_auth_exceptions
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

from ...api.dependencies import get_current_identity_account
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.patient_document_storage import (
    UPLOAD_TTL,
    StorageNotConfigured,
    StorageObjectTooLarge,
    create_download_url,
    create_upload_url,
    delete_object,
    inspect_content,
    inspect_object,
)
from ...core.timezones import DEFAULT_TIMEZONE, timezone
from ...domains.governance.audit import record_audit
from ...models.care import Encounter, PatientDocument, Practitioner, SoapNote
from ...models.identity import Patient, UserAccount
from ...models.organization import (
    Facility,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffMember,
)
from ...schemas.documents import PatientDocumentUploadInput
from .bootstrap import ADMIN_ROLES, ROLE_SCOPES

router = APIRouter(tags=["patient-documents"])
logger = logging.getLogger(__name__)

MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
DOWNLOAD_CAPABILITY_SECONDS = 5 * 60
CLINICAL_ROLES = {
    role for role, scopes in ROLE_SCOPES.items() if "clinical:author" in scopes
}
MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


def _storage_error(exc: Exception, *, signing: bool = False) -> HTTPException:
    if isinstance(exc, StorageNotConfigured):
        return _error(503, "DOCUMENT_STORAGE_NOT_CONFIGURED", str(exc))
    if isinstance(
        exc,
        (
            google_auth_exceptions.DefaultCredentialsError,
            google_auth_exceptions.RefreshError,
        ),
    ):
        return _error(
            503,
            "GCS_CREDENTIALS_UNAVAILABLE",
            "Google Application Default Credentials are unavailable.",
        )
    if signing or isinstance(exc, google_exceptions.Forbidden):
        return _error(
            503,
            "GCS_SIGNING_UNAVAILABLE" if signing else "DOCUMENT_STORAGE_UNAVAILABLE",
            "Google Cloud Storage signing is unavailable."
            if signing
            else "Google Cloud Storage is unavailable.",
        )
    return _error(
        503, "DOCUMENT_STORAGE_UNAVAILABLE", "Google Cloud Storage is unavailable."
    )


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
            StaffAssignment.role_code.in_(CLINICAL_ROLES),
            or_(
                StaffAssignment.role_code.in_(ADMIN_ROLES),
                StaffAssignment.facility_id == Encounter.facility_id,
            ),
        )
    )


async def _encounter(
    db: AsyncSession,
    account: UserAccount,
    encounter_uuid: UUID,
    *,
    lock: bool = False,
) -> Encounter:
    query = select(Encounter).where(
        Encounter.id == encounter_uuid, _encounter_access(account.id)
    )
    if lock:
        query = query.with_for_update(of=Encounter)
    encounter = await db.scalar(query)
    if encounter is None:
        raise _error(404, "NOT_FOUND", "Encounter not found.")
    patient_exists = await db.scalar(
        select(Patient.id).where(
            Patient.id == encounter.patient_id,
            Patient.organization_id == encounter.organization_id,
        )
    )
    if patient_exists is None:
        raise _error(
            409,
            "ENCOUNTER_DATA_INTEGRITY_ERROR",
            "Encounter patient does not belong to the encounter tenant.",
        )
    return encounter


async def _require_draft_soap(db: AsyncSession, encounter_id: UUID) -> None:
    soap_status = await db.scalar(
        select(SoapNote.status).where(SoapNote.encounter_id == encounter_id)
    )
    if soap_status == "signed":
        raise _error(409, "SOAP_ALREADY_SIGNED", "SOAP note is already signed.")


async def _soap_is_signed(db: AsyncSession, encounter_id: UUID) -> bool:
    return (
        await db.scalar(
            select(SoapNote.status).where(SoapNote.encounter_id == encounter_id)
        )
        == "signed"
    )


def _safe_filename(filename: str, mime_type: str) -> str:
    raw = PurePath(filename.replace("\\", "/")).name
    stem = PurePath(raw).stem
    stem = unicodedata.normalize("NFKC", stem)
    stem = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]+", "_", stem).strip(" ._")
    return f"{(stem or 'document')[:220]}{MIME_EXTENSIONS[mime_type]}"


def _detected_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def _document_query():
    return (
        select(PatientDocument, UserAccount, Encounter, Practitioner, Facility)
        .join(UserAccount, UserAccount.id == PatientDocument.uploaded_by_user_id)
        .join(
            Encounter,
            and_(
                Encounter.id == PatientDocument.encounter_id,
                Encounter.organization_id == PatientDocument.organization_id,
                Encounter.patient_id == PatientDocument.patient_id,
            ),
        )
        .outerjoin(
            Practitioner,
            and_(
                Practitioner.id == Encounter.practitioner_id,
                Practitioner.organization_id == Encounter.organization_id,
            ),
        )
        .join(
            Facility,
            and_(
                Facility.id == Encounter.facility_id,
                Facility.organization_id == Encounter.organization_id,
            ),
        )
    )


def _capability_signature(
    document_uuid: UUID, actor_uuid: UUID, generation: str, expires: int
) -> str:
    message = f"{document_uuid}:{actor_uuid}:{generation}:{expires}".encode()
    return hmac.new(
        settings.SECRET_KEY.get_secret_value().encode(), message, hashlib.sha256
    ).hexdigest()


def _document_item(
    row: tuple[PatientDocument, UserAccount, Encounter, Practitioner | None, Facility],
    request: Request,
    actor_uuid: UUID,
) -> dict[str, Any]:
    document, uploader, encounter, practitioner, facility = row
    download_url = None
    if document.status == "available" and document.storage_generation:
        expires = int(time()) + DOWNLOAD_CAPABILITY_SECONDS
        query = urlencode(
            {
                "actor": str(actor_uuid),
                "expires": expires,
                "signature": _capability_signature(
                    document.id, actor_uuid, document.storage_generation, expires
                ),
            }
        )
        download_url = f"{request.url_for('download_patient_document', document_uuid=str(document.id))}?{query}"
    return {
        "uuid": str(document.id),
        "patient_uuid": str(document.patient_id),
        "encounter_uuid": str(document.encounter_id),
        "category": document.category,
        "title": document.title,
        "document_date": document.document_date.isoformat(),
        "original_filename": document.original_filename,
        "mime_type": document.mime_type or document.declared_mime_type,
        "size_bytes": document.size_bytes or document.expected_size_bytes,
        "status": document.status,
        "uploaded_at": document.uploaded_at.isoformat(),
        "uploaded_by": {
            "uuid": str(uploader.id),
            "display_name": uploader.display_name or uploader.email or "HealthOS user",
        },
        "encounter": {
            "uuid": str(encounter.id),
            "started_at": encounter.started_at.isoformat(),
            "practitioner_name": practitioner.person_name
            if practitioner
            else "Unassigned practitioner",
            "facility_name": facility.name,
        },
        "download_url": download_url,
    }


async def _document_row(
    db: AsyncSession,
    account: UserAccount,
    document_uuid: UUID,
    encounter_uuid: UUID | None = None,
):
    query = _document_query().where(
        PatientDocument.id == document_uuid,
        PatientDocument.deleted_at.is_(None),
        _encounter_access(account.id),
    )
    if encounter_uuid:
        query = query.where(PatientDocument.encounter_id == encounter_uuid)
    row = (await db.execute(query)).first()
    if row is None:
        raise _error(404, "NOT_FOUND", "Document not found.")
    return row


@router.post(
    "/encounters/{encounter_uuid}/documents",
    status_code=status.HTTP_201_CREATED,
)
async def initiate_document_upload(
    encounter_uuid: UUID,
    payload: PatientDocumentUploadInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    if payload.size_bytes > MAX_DOCUMENT_BYTES:
        raise _error(413, "DOCUMENT_TOO_LARGE", "Document must be 20 MB or smaller.")
    encounter = await _encounter(db, account, encounter_uuid, lock=True)
    await _require_draft_soap(db, encounter.id)
    timezone_name = await db.scalar(
        select(FacilitySchedule.timezone).where(
            FacilitySchedule.facility_id == encounter.facility_id
        )
    )
    if (
        payload.document_date
        > datetime.now(timezone(timezone_name or DEFAULT_TIMEZONE)).date()
    ):
        raise _error(422, "VALIDATION_ERROR", "Document date cannot be in the future.")

    document = PatientDocument(
        organization_id=encounter.organization_id,
        patient_id=encounter.patient_id,
        encounter_id=encounter.id,
        uploaded_by_user_id=account.id,
        category=payload.category,
        title=payload.title,
        document_date=payload.document_date,
        original_filename=_safe_filename(payload.filename, payload.content_type),
        declared_mime_type=payload.content_type,
        expected_size_bytes=payload.size_bytes,
        expected_sha256=payload.sha256,
        storage_key=f"patient-documents/{uuid4().hex}",
    )
    db.add(document)
    await db.flush()
    try:
        upload_url, headers = await run_in_threadpool(
            create_upload_url,
            document.storage_key,
            document_uuid=str(document.id),
            content_type=document.declared_mime_type,
            sha256=document.expected_sha256,
        )
    except Exception as exc:
        await db.rollback()
        raise _storage_error(exc, signing=True) from exc
    await db.commit()
    return {
        "success": True,
        "data": {
            "document_uuid": str(document.id),
            "status": document.status,
            "upload": {
                "url": upload_url,
                "method": "PUT",
                "headers": headers,
                "expires_at": (datetime.now(UTC) + UPLOAD_TTL).isoformat(),
            },
        },
        "meta": {},
    }


async def _reject_uploaded_object(
    db: AsyncSession,
    document: PatientDocument,
    encounter: Encounter,
    account: UserAccount,
    *,
    generation: str | None,
    reason: str,
) -> None:
    now = datetime.now(UTC)
    document.status = "rejected"
    document.rejection_reason = reason
    document.storage_generation = generation
    document.confirmed_at = now
    document.updated_at = now
    if generation:
        try:
            await run_in_threadpool(
                delete_object, document.storage_key, generation=generation
            )
            document.storage_deleted_at = now
        except google_exceptions.NotFound:
            document.storage_deleted_at = now
        except Exception:  # noqa: BLE001 - rejection must remain fail-closed
            logger.warning(
                "Rejected patient document object cleanup failed; document_uuid=%s",
                document.id,
            )
    await record_audit(
        db,
        organization_id=encounter.organization_id,
        actor_user_id=account.id,
        action="clinical.patient_document.rejected",
        resource_type="patient_document",
        resource_id=document.id,
        facility_id=encounter.facility_id,
        patient_id=encounter.patient_id,
    )
    await db.commit()


@router.post("/encounters/{encounter_uuid}/documents/{document_uuid}/complete")
async def complete_document_upload(
    encounter_uuid: UUID,
    document_uuid: UUID,
    request: Request,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid, lock=True)
    document = await db.scalar(
        select(PatientDocument)
        .where(
            PatientDocument.id == document_uuid,
            PatientDocument.encounter_id == encounter.id,
            PatientDocument.organization_id == encounter.organization_id,
            PatientDocument.patient_id == encounter.patient_id,
            PatientDocument.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if document is None:
        raise _error(404, "NOT_FOUND", "Document not found.")
    if document.status == "processing":
        if await _soap_is_signed(db, encounter.id):
            raise _error(409, "SOAP_ALREADY_SIGNED", "SOAP note is already signed.")
        document.status = "available"
        document.updated_at = datetime.now(UTC)
        await db.commit()
        row = await _document_row(db, account, document.id, encounter.id)
        return {
            "success": True,
            "data": _document_item(row, request, account.id),
            "meta": {},
        }
    if document.status != "uploading":
        row = await _document_row(db, account, document.id, encounter.id)
        return {
            "success": True,
            "data": _document_item(row, request, account.id),
            "meta": {},
        }
    if await _soap_is_signed(db, encounter.id):
        generation = None
        try:
            generation = (
                await run_in_threadpool(inspect_object, document.storage_key)
            )["generation"]
            await run_in_threadpool(
                delete_object, document.storage_key, generation=generation
            )
        except google_exceptions.NotFound:
            pass
        except Exception as exc:
            raise _storage_error(exc) from exc
        now = datetime.now(UTC)
        document.deleted_at = now
        document.deleted_by_user_id = account.id
        document.storage_generation = generation
        document.storage_deleted_at = now
        document.updated_at = now
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.patient_document.rejected",
            resource_type="patient_document",
            resource_id=document.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
        await db.commit()
        raise _error(409, "SOAP_ALREADY_SIGNED", "SOAP note is already signed.")

    try:
        stored = await run_in_threadpool(inspect_object, document.storage_key)
    except google_exceptions.NotFound as exc:
        raise _error(409, "UPLOAD_NOT_FOUND", "Uploaded object was not found.") from exc
    except Exception as exc:
        raise _storage_error(exc) from exc

    generation = stored["generation"]
    metadata = stored["metadata"]
    failure: HTTPException | None = None
    reason = ""
    if (
        metadata.get("document-uuid") != str(document.id)
        or metadata.get("sha256") != document.expected_sha256
    ):
        failure = _error(
            409,
            "UPLOAD_OBJECT_MISMATCH",
            "Uploaded object does not match the document.",
        )
        reason = "object_metadata_mismatch"
    elif stored["size"] > MAX_DOCUMENT_BYTES:
        failure = _error(
            413, "DOCUMENT_TOO_LARGE", "Document must be 20 MB or smaller."
        )
        reason = "size_limit_exceeded"
    elif stored["size"] != document.expected_size_bytes:
        failure = _error(
            409, "UPLOAD_OBJECT_MISMATCH", "Uploaded object size does not match."
        )
        reason = "size_mismatch"
    elif stored["content_type"] != document.declared_mime_type:
        failure = _error(
            415, "UNSUPPORTED_DOCUMENT_TYPE", "Uploaded content type does not match."
        )
        reason = "content_type_mismatch"

    detected_mime: str | None = None
    actual_sha256 = ""
    actual_size = 0
    if failure is None:
        try:
            verified = await run_in_threadpool(
                inspect_content,
                stored["blob"],
                generation,
                max_bytes=MAX_DOCUMENT_BYTES,
            )
        except StorageObjectTooLarge:
            failure = _error(
                413, "DOCUMENT_TOO_LARGE", "Document must be 20 MB or smaller."
            )
            reason = "size_limit_exceeded"
        except Exception as exc:
            raise _storage_error(exc) from exc
        if failure is None:
            detected_mime = _detected_mime(verified["prefix"])
            actual_size = verified["size"]
            actual_sha256 = verified["sha256"]
        if failure is None and (
            detected_mime is None or detected_mime != document.declared_mime_type
        ):
            failure = _error(
                415,
                "UNSUPPORTED_DOCUMENT_TYPE",
                "Only PDF, JPEG, and PNG documents are supported.",
            )
            reason = "magic_bytes_mismatch"
        elif failure is None and not hmac.compare_digest(
            actual_sha256, document.expected_sha256
        ):
            failure = _error(
                409, "UPLOAD_HASH_MISMATCH", "Uploaded object hash does not match."
            )
            reason = "sha256_mismatch"

    if failure is not None:
        await _reject_uploaded_object(
            db,
            document,
            encounter,
            account,
            generation=generation,
            reason=reason,
        )
        raise failure

    now = datetime.now(UTC)
    document.status = "available"
    document.mime_type = detected_mime
    document.size_bytes = actual_size
    document.sha256 = actual_sha256
    document.storage_generation = generation
    document.original_filename = _safe_filename(
        document.original_filename, detected_mime
    )
    document.confirmed_at = now
    document.updated_at = now
    await record_audit(
        db,
        organization_id=encounter.organization_id,
        actor_user_id=account.id,
        action="clinical.patient_document.uploaded",
        resource_type="patient_document",
        resource_id=document.id,
        facility_id=encounter.facility_id,
        patient_id=encounter.patient_id,
    )
    await db.commit()
    row = await _document_row(db, account, document.id, encounter.id)
    return {
        "success": True,
        "data": _document_item(row, request, account.id),
        "meta": {},
    }


@router.get("/encounters/{encounter_uuid}/documents")
async def encounter_documents(
    encounter_uuid: UUID,
    request: Request,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid)
    rows = (
        await db.execute(
            _document_query()
            .where(
                PatientDocument.encounter_id == encounter.id,
                PatientDocument.status != "uploading",
                PatientDocument.deleted_at.is_(None),
                _encounter_access(account.id),
            )
            .order_by(
                PatientDocument.document_date.desc(),
                PatientDocument.uploaded_at.desc(),
            )
        )
    ).all()
    return {
        "success": True,
        "data": {"items": [_document_item(row, request, account.id) for row in rows]},
        "meta": {"count": len(rows)},
    }


@router.get("/patients/{patient_uuid}/documents")
async def patient_documents(
    patient_uuid: UUID,
    request: Request,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    patient_allowed = await db.scalar(
        select(Patient.id).where(
            Patient.id == patient_uuid,
            exists(
                select(StaffAssignment.id)
                .select_from(StaffAssignment)
                .join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id)
                .join(Organization, Organization.id == StaffMember.organization_id)
                .where(
                    StaffMember.user_account_id == account.id,
                    StaffMember.organization_id == Patient.organization_id,
                    StaffMember.is_active.is_(True),
                    Organization.is_active.is_(True),
                    StaffAssignment.is_active.is_(True),
                    StaffAssignment.role_code.in_(CLINICAL_ROLES),
                    or_(
                        StaffAssignment.role_code.in_(ADMIN_ROLES),
                        exists(
                            select(Encounter.id).where(
                                Encounter.patient_id == Patient.id,
                                Encounter.organization_id == Patient.organization_id,
                                Encounter.facility_id == StaffAssignment.facility_id,
                            )
                        ),
                    ),
                )
            ),
        )
    )
    if patient_allowed is None:
        raise _error(404, "NOT_FOUND", "Patient not found.")
    rows = (
        await db.execute(
            _document_query()
            .where(
                PatientDocument.patient_id == patient_uuid,
                PatientDocument.status != "uploading",
                PatientDocument.deleted_at.is_(None),
                _encounter_access(account.id),
            )
            .order_by(
                PatientDocument.document_date.desc(),
                PatientDocument.uploaded_at.desc(),
            )
        )
    ).all()
    return {
        "success": True,
        "data": {"items": [_document_item(row, request, account.id) for row in rows]},
        "meta": {"count": len(rows)},
    }


@router.get("/encounters/{encounter_uuid}/documents/{document_uuid}")
async def get_document(
    encounter_uuid: UUID,
    document_uuid: UUID,
    request: Request,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    await _encounter(db, account, encounter_uuid)
    row = await _document_row(db, account, document_uuid, encounter_uuid)
    return {
        "success": True,
        "data": _document_item(row, request, account.id),
        "meta": {},
    }


@router.get("/documents/{document_uuid}/download", name="download_patient_document")
async def download_patient_document(
    document_uuid: UUID,
    actor: UUID,
    expires: int,
    signature: str,
    db: Annotated[AsyncSession, Depends(async_get_db)],
):
    if expires < int(time()) or expires > int(time()) + DOWNLOAD_CAPABILITY_SECONDS:
        raise _error(401, "DOWNLOAD_URL_EXPIRED", "Download URL has expired.")
    account = await db.get(UserAccount, actor)
    if account is None or not account.is_active:
        raise _error(404, "NOT_FOUND", "Document not found.")
    row = await _document_row(db, account, document_uuid)
    document, _, encounter, _, _ = row
    if document.status != "available" or not document.storage_generation:
        raise _error(409, "DOCUMENT_NOT_AVAILABLE", "Document is not available.")
    expected = _capability_signature(
        document.id, actor, document.storage_generation, expires
    )
    if not hmac.compare_digest(signature, expected):
        raise _error(401, "INVALID_DOWNLOAD_URL", "Download URL is invalid.")
    try:
        url = await run_in_threadpool(
            create_download_url,
            document.storage_key,
            generation=document.storage_generation,
            filename=document.original_filename,
            content_type=document.mime_type or document.declared_mime_type,
        )
    except Exception as exc:
        raise _storage_error(exc, signing=True) from exc
    await record_audit(
        db,
        organization_id=document.organization_id,
        actor_user_id=account.id,
        action="clinical.patient_document.viewed",
        resource_type="patient_document",
        resource_id=document.id,
        facility_id=encounter.facility_id,
        patient_id=document.patient_id,
    )
    await db.commit()
    return RedirectResponse(url=url, status_code=307)


@router.delete("/encounters/{encounter_uuid}/documents/{document_uuid}")
async def delete_document(
    encounter_uuid: UUID,
    document_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await _encounter(db, account, encounter_uuid, lock=True)
    document = await db.scalar(
        select(PatientDocument)
        .where(
            PatientDocument.id == document_uuid,
            PatientDocument.encounter_id == encounter.id,
            PatientDocument.organization_id == encounter.organization_id,
            PatientDocument.patient_id == encounter.patient_id,
        )
        .with_for_update()
    )
    if document is None:
        raise _error(404, "NOT_FOUND", "Document not found.")
    if document.deleted_at is None:
        await _require_draft_soap(db, encounter.id)
        document.deleted_at = datetime.now(UTC)
        document.deleted_by_user_id = account.id
        document.updated_at = document.deleted_at
        await record_audit(
            db,
            organization_id=encounter.organization_id,
            actor_user_id=account.id,
            action="clinical.patient_document.removed",
            resource_type="patient_document",
            resource_id=document.id,
            facility_id=encounter.facility_id,
            patient_id=encounter.patient_id,
        )
        await db.commit()

    if document.storage_deleted_at is None:
        generation = document.storage_generation
        if generation is None:
            try:
                generation = (
                    await run_in_threadpool(inspect_object, document.storage_key)
                )["generation"]
            except google_exceptions.NotFound:
                generation = None
            except Exception as exc:
                raise _storage_error(exc) from exc
        if generation:
            try:
                await run_in_threadpool(
                    delete_object, document.storage_key, generation=generation
                )
            except google_exceptions.NotFound:
                pass
            except Exception as exc:
                raise _storage_error(exc) from exc
        document.storage_deleted_at = datetime.now(UTC)
        await db.commit()
    return {"success": True, "data": None, "meta": {}}

