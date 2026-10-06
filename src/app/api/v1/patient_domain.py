"""Patient-owned domain projections: directory, appointments, records, bills, notifications.

Every route requires a patient session and resolves objects through verified
``patient_record_link`` rows. Staff list/search endpoints are never exposed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.patient_dependencies import PatientPrincipal, get_current_patient
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.events import make_event, publish
from ...core.timezones import normalize_range
from ...domains.patient_auth.links import PatientLinkError
from ...domains.patient_portal import appointments as appt_service
from ...domains.patient_portal import billing as billing_service
from ...domains.patient_portal import directory as directory_service
from ...domains.patient_portal import notifications as notify_service
from ...domains.patient_portal import records as record_service
from ...models.billing import Invoice, Payment, Refund
from ...models.care import (
    Appointment,
    AppointmentRequest,
    Encounter,
    PatientDocument,
    Practitioner,
    Prescription,
    QueueEntry,
    RecordRelease,
)
from ...models.organization import Facility, Organization
from ...schemas.patient_domain import (
    AlternativeResponseBody,
    AppointmentRequestBody,
    AppointmentStatusQuery,
    CancelRequestBody,
    PreferenceUpdateBody,
    PushDeviceBody,
    RescheduleRequestBody,
)
from .patient import patient_link_service
from .scheduling import _facility_timezone

router = APIRouter(prefix="/patient", tags=["patient-domain"])


def _pepper() -> str:
    if settings.PATIENT_OTP_PEPPER is not None:
        return settings.PATIENT_OTP_PEPPER.get_secret_value()
    return settings.SECRET_KEY.get_secret_value()


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, appt_service.SlotUnavailable):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        )
    if isinstance(exc, appt_service.IdempotencyConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(
        exc,
        (
            appt_service.RequestNotFound,
            record_service.ReleaseNotFound,
            billing_service.ReceiptUnavailable,
        ),
    ):
        status_code = status.HTTP_404_NOT_FOUND
        return HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(
        exc,
        (
            appt_service.RequestNotPending,
            appt_service.RequestExpired,
            appt_service.RequestValidation,
            record_service.NotReleased,
            billing_service.InvoiceNotDiscountable,
            billing_service.InvalidDiscount,
            directory_service.DirectoryError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT
            if not isinstance(exc, directory_service.DirectoryError)
            else status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(exc, PatientLinkError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(exc, HTTPException):
        return exc
    raise exc


async def _link_for_org(
    db: AsyncSession,
    principal: PatientPrincipal,
    organization_uuid: UUID,
) -> tuple[Any, Organization]:
    link = await patient_link_service.verified_link_for_organization(
        db, account_id=principal.account.id, organization_id=organization_uuid
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "RECORD_NOT_LINKED", "message": "No verified record link for this clinic.", "details": []},
        )
    organization = await db.get(Organization, organization_uuid)
    if organization is None or not organization.is_active or not organization.portal_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CLINIC_NOT_AVAILABLE", "message": "This clinic is not available in the patient portal.", "details": []},
        )
    return link, organization


async def _facility_for_link(
    db: AsyncSession, link: Any, facility_uuid: UUID
) -> Facility:
    facility = await db.get(Facility, facility_uuid)
    if (
        facility is None
        or facility.organization_id != link.organization_id
        or not facility.is_active
        or not patient_link_service.facility_permitted(link, facility.id)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Facility not found for this clinic.", "details": []},
        )
    return facility


async def _linked_patients(
    db: AsyncSession, principal: PatientPrincipal
) -> tuple[list[Any], dict[UUID, Any]]:
    links = await patient_link_service.verified_links(
        db, account_id=principal.account.id
    )
    return links, {link.patient_id: link for link in links}


def _patient_label(status_value: str) -> str:
    return appt_service.patient_appointment_label(status_value)


def _request_payload(request: AppointmentRequest) -> dict[str, Any]:
    return {
        "uuid": str(request.id),
        "item_type": "request",
        "kind": request.kind,
        "status": request.status,
        "patient_label": _patient_label(
            "booked" if request.status == "approved" else request.status
        )
        if request.status == "approved"
        else request.status,
        "organization_uuid": str(request.organization_id),
        "facility_uuid": str(request.facility_id),
        "practitioner_uuid": str(request.practitioner_id) if request.practitioner_id else None,
        "origin_appointment_uuid": str(request.appointment_id) if request.appointment_id else None,
        "requested": (
            {
                "start": request.requested_start.isoformat(),
                "end": request.requested_end.isoformat(),
            }
            if request.requested_start and request.requested_end
            else None
        ),
        "alternative": (
            {
                "start": request.alternative_start.isoformat(),
                "end": request.alternative_end.isoformat(),
                "note": request.alternative_note,
            }
            if request.alternative_start and request.alternative_end
            else None
        ),
        "reason_text": request.reason_text,
        "decision_reason": request.decision_reason,
        "expires_at": request.expires_at.isoformat() if request.expires_at else None,
        "version": request.version,
        "created_at": request.created_at.isoformat() if request.created_at else None,
        "updated_at": request.updated_at.isoformat() if request.updated_at else None,
    }


def _appointment_payload(
    appointment: Appointment,
    *,
    practitioner_name: str | None = None,
    token: QueueEntry | None = None,
) -> dict[str, Any]:
    return {
        "uuid": str(appointment.id),
        "item_type": "appointment",
        "status": appointment.status,
        "patient_label": _patient_label(appointment.status),
        "organization_uuid": str(appointment.organization_id),
        "facility_uuid": str(appointment.facility_id),
        "practitioner_uuid": str(appointment.practitioner_id),
        "practitioner_name": practitioner_name,
        "scheduled_start": appointment.scheduled_start.isoformat(),
        "scheduled_end": appointment.scheduled_end.isoformat(),
        "reason_code": appointment.reason_code,
        "reason_text": appointment.reason_text,
        "version": appointment.version,
        "queue_token": (
            {
                "uuid": str(token.id),
                "token_number": token.token_number,
                "status": token.status,
                "queue_date": token.queue_date.isoformat(),
            }
            if token is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------


@router.get("/clinics/{organization_uuid}/specialties")
async def clinic_specialties(
    organization_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    _, organization = await _link_for_org(db, principal, organization_uuid)
    items = await directory_service.list_specialties(db, organization_id=organization.id)
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/practitioners")
async def list_practitioners(
    organization_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: UUID | None = None,
    specialty_uuid: UUID | None = None,
) -> dict[str, Any]:
    link, organization = await _link_for_org(db, principal, organization_uuid)
    if facility_uuid is not None:
        await _facility_for_link(db, link, facility_uuid)
    items = await directory_service.list_practitioners(
        db,
        organization_id=organization.id,
        specialty_id=specialty_uuid,
        facility_id=facility_uuid,
    )
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/practitioners/{practitioner_uuid}")
async def get_practitioner(
    practitioner_uuid: UUID,
    organization_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    _, organization = await _link_for_org(db, principal, organization_uuid)
    payload = await directory_service.get_practitioner(
        db, organization_id=organization.id, practitioner_id=practitioner_uuid
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Practitioner not found.", "details": []},
        )
    return {"success": True, "data": {"practitioner": payload}, "meta": {}}


@router.get("/practitioners/{practitioner_uuid}/availability")
async def practitioner_availability(
    practitioner_uuid: UUID,
    organization_uuid: UUID,
    facility_uuid: UUID,
    from_date: str,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    to_date: str | None = None,
) -> dict[str, Any]:
    from datetime import date as date_type

    link, organization = await _link_for_org(db, principal, organization_uuid)
    facility = await _facility_for_link(db, link, facility_uuid)
    try:
        start = date_type.fromisoformat(from_date)
        end = date_type.fromisoformat(to_date) if to_date else start
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": str(exc), "details": []},
        ) from exc
    try:
        days = await directory_service.availability(
            db,
            organization_id=organization.id,
            facility_id=facility.id,
            practitioner_id=practitioner_uuid,
            from_date=start,
            to_date=end,
        )
    except directory_service.DirectoryError as exc:
        raise _http_error(exc) from exc
    return {"success": True, "data": {"days": days}, "meta": {}}


# ---------------------------------------------------------------------------
# Appointment requests and appointments
# ---------------------------------------------------------------------------


@router.post("/appointment-requests", status_code=status.HTTP_201_CREATED)
async def create_appointment_request(
    payload: AppointmentRequestBody,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": "Idempotency-Key header is required.", "details": []},
        )
    link, organization = await _link_for_org(db, principal, payload.organization_uuid)
    facility = await _facility_for_link(db, link, payload.facility_uuid)
    practitioner = await db.scalar(
        select(Practitioner).where(
            Practitioner.id == payload.practitioner_uuid,
            Practitioner.organization_id == organization.id,
            Practitioner.is_active.is_(True),
        )
    )
    if practitioner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Practitioner not found.", "details": []},
        )
    try:
        start, end = normalize_range(
            payload.scheduled_start,
            payload.scheduled_end,
            await _facility_timezone(db, facility.id),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": str(exc), "details": []},
        ) from exc
    try:
        request, replayed = await appt_service.create_new_request(
            db,
            account=principal.account,
            link=link,
            facility=facility,
            practitioner=practitioner,
            start=start,
            end=end,
            reason_code=payload.reason_code,
            reason_text=payload.reason_text,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    if not replayed and request.status == "requested":
        # SSE invalidation for staff request pages; best-effort after commit
        # (a missed publish is repaired by HTTP refetch).
        await publish(
            make_event(
                "appointment_request.created",
                entity_id=request.id,
                entity_version=request.version,
                organization_id=request.organization_id,
                facility_id=request.facility_id,
                practitioner_id=request.practitioner_id,
                new={"status": request.status},
            )
        )
    return {
        "success": True,
        "data": _request_payload(request),
        "meta": {"idempotent": replayed, "approval_required": request.status == "requested"},
    }


@router.get("/appointments")
async def list_appointments(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    scope: str = Query(default="upcoming", pattern="^(upcoming|past)$"),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> dict[str, Any]:
    await appt_service.expire_stale_requests(db, account_id=principal.account.id)
    _, links_by_patient = await _linked_patients(db, principal)
    patient_ids = list(links_by_patient.keys())
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    if patient_ids:
        appointment_query = select(Appointment).where(Appointment.patient_id.in_(patient_ids))
        if scope == "upcoming":
            appointment_query = appointment_query.where(
                Appointment.status.in_(("booked", "confirmed", "checked_in", "in_consultation")),
                Appointment.scheduled_end >= now - timedelta(hours=6),
            ).order_by(Appointment.scheduled_start.asc())
        else:
            appointment_query = appointment_query.where(
                or_(
                    Appointment.status.in_(("completed", "cancelled", "no_show")),
                    Appointment.scheduled_end < now - timedelta(hours=6),
                )
            ).order_by(Appointment.scheduled_start.desc())
        appointments = (
            await db.scalars(appointment_query.limit(limit).offset(offset))
        ).all()
        practitioner_names = await _practitioner_names(db, {a.practitioner_id for a in appointments})
        tokens = await _queue_tokens(db, {a.id for a in appointments})
        items.extend(
            _appointment_payload(
                a, practitioner_name=practitioner_names.get(a.practitioner_id), token=tokens.get(a.id)
            )
            for a in appointments
        )
    request_query = select(AppointmentRequest).where(
        AppointmentRequest.patient_account_id == principal.account.id
    )
    if scope == "upcoming":
        request_query = request_query.where(
            AppointmentRequest.status.in_(("requested", "alternative_proposed"))
        ).order_by(AppointmentRequest.created_at.desc())
    else:
        request_query = request_query.where(
            AppointmentRequest.status.in_(("rejected", "withdrawn", "expired"))
        ).order_by(AppointmentRequest.created_at.desc())
    requests = (await db.scalars(request_query.limit(limit).offset(offset))).all()
    items.extend(_request_payload(r) for r in requests)
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items), "has_more": len(items) == limit},
    }


async def _practitioner_names(
    db: AsyncSession, practitioner_ids: set[UUID]
) -> dict[UUID, str]:
    if not practitioner_ids:
        return {}
    rows = await db.scalars(
        select(Practitioner).where(Practitioner.id.in_(practitioner_ids))
    )
    return {p.id: p.person_name for p in rows}


async def _queue_tokens(
    db: AsyncSession, appointment_ids: set[UUID]
) -> dict[UUID, QueueEntry]:
    if not appointment_ids:
        return {}
    rows = await db.scalars(
        select(QueueEntry).where(
            QueueEntry.appointment_id.in_(appointment_ids),
            QueueEntry.status.in_(("waiting", "called", "in_consultation")),
        )
    )
    return {entry.appointment_id: entry for entry in rows if entry.appointment_id}


async def _owned_appointment(
    db: AsyncSession, principal: PatientPrincipal, appointment_uuid: UUID
) -> tuple[Appointment, Any]:
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Appointment not found.", "details": []},
        )
    link = await patient_link_service.verified_link_for_patient(
        db, account_id=principal.account.id, patient_id=appointment.patient_id
    )
    if link is None or link.organization_id != appointment.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Appointment not found.", "details": []},
        )
    return appointment, link


async def _owned_request(
    db: AsyncSession, principal: PatientPrincipal, request_uuid: UUID
) -> AppointmentRequest:
    request = await db.get(AppointmentRequest, request_uuid)
    if request is None or request.patient_account_id != principal.account.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Request not found.", "details": []},
        )
    return request


@router.get("/appointments/{appointment_uuid}")
async def get_appointment(
    appointment_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    await appt_service.expire_stale_requests(db, account_id=principal.account.id)
    appointment = await db.get(Appointment, appointment_uuid)
    if appointment is not None:
        appointment, _ = await _owned_appointment(db, principal, appointment_uuid)
        names = await _practitioner_names(db, {appointment.practitioner_id})
        tokens = await _queue_tokens(db, {appointment.id})
        return {
            "success": True,
            "data": {
                "item": _appointment_payload(
                    appointment,
                    practitioner_name=names.get(appointment.practitioner_id),
                    token=tokens.get(appointment.id),
                )
            },
            "meta": {},
        }
    request = await db.get(AppointmentRequest, appointment_uuid)
    if request is None or request.patient_account_id != principal.account.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Appointment not found.", "details": []},
        )
    return {"success": True, "data": {"item": _request_payload(request)}, "meta": {}}


@router.post("/appointments/{appointment_uuid}/reschedule-request")
async def reschedule_request(
    appointment_uuid: UUID,
    payload: RescheduleRequestBody,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": "Idempotency-Key header is required.", "details": []},
        )
    appointment, link = await _owned_appointment(db, principal, appointment_uuid)
    if appointment.status not in {"booked", "confirmed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "APPOINTMENT_STATUS_NOT_RESCHEDULABLE", "message": "This appointment cannot be rescheduled.", "details": []},
        )
    if payload.version != appointment.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "STALE_VERSION", "message": "The appointment changed. Reload and retry.", "details": [{"current_version": appointment.version}]},
        )
    facility = await _facility_for_link(db, link, payload.facility_uuid or appointment.facility_id)
    try:
        start, end = normalize_range(
            payload.scheduled_start,
            payload.scheduled_end,
            await _facility_timezone(db, facility.id),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": str(exc), "details": []},
        ) from exc
    try:
        request, replayed = await appt_service.create_change_request(
            db,
            account=principal.account,
            link=link,
            facility=facility,
            appointment=appointment,
            kind="reschedule",
            start=start,
            end=end,
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return {
        "success": True,
        "data": _request_payload(request),
        "meta": {"idempotent": replayed, "original_preserved": True},
    }


@router.post("/appointments/{appointment_uuid}/cancel-request")
async def cancel_request(
    appointment_uuid: UUID,
    payload: CancelRequestBody,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": "Idempotency-Key header is required.", "details": []},
        )
    appointment, link = await _owned_appointment(db, principal, appointment_uuid)
    if appointment.status in {"completed", "cancelled", "no_show"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "APPOINTMENT_STATUS_NOT_CANCELLABLE", "message": "This appointment cannot be cancelled.", "details": []},
        )
    if payload.version != appointment.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "STALE_VERSION", "message": "The appointment changed. Reload and retry.", "details": [{"current_version": appointment.version}]},
        )
    facility = await db.get(Facility, appointment.facility_id)
    try:
        request, replayed = await appt_service.create_change_request(
            db,
            account=principal.account,
            link=link,
            facility=facility,
            appointment=appointment,
            kind="cancel",
            start=None,
            end=None,
            reason=payload.reason,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return {
        "success": True,
        "data": _request_payload(request),
        "meta": {"idempotent": replayed, "original_preserved": True},
    }


@router.post("/appointment-requests/{request_uuid}/withdraw")
async def withdraw_request(
    request_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    request = await _owned_request(db, principal, request_uuid)
    try:
        request = await appt_service.withdraw_request(
            db, request=request, account=principal.account
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return {"success": True, "data": _request_payload(request), "meta": {}}


@router.post("/appointment-requests/{request_uuid}/respond")
async def respond_to_alternative(
    request_uuid: UUID,
    payload: AlternativeResponseBody,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    request = await _owned_request(db, principal, request_uuid)
    try:
        request = await appt_service.respond_to_alternative(
            db, request=request, account=principal.account, accept=payload.accept
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return {"success": True, "data": _request_payload(request), "meta": {}}


@router.post("/appointments/status-query")
async def appointment_status_query(
    payload: AppointmentStatusQuery,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Bounded polling for own statuses; no facility staff stream is exposed."""
    await appt_service.expire_stale_requests(db, account_id=principal.account.id)
    _, links_by_patient = await _linked_patients(db, principal)
    patient_ids = set(links_by_patient)
    items: list[dict[str, Any]] = []
    if payload.appointment_uuids and patient_ids:
        appointments = (
            await db.scalars(
                select(Appointment).where(
                    Appointment.id.in_(payload.appointment_uuids),
                    Appointment.patient_id.in_(patient_ids),
                )
            )
        ).all()
        items.extend(
            {"uuid": str(a.id), "item_type": "appointment", "status": a.status, "patient_label": _patient_label(a.status), "version": a.version}
            for a in appointments
        )
    if payload.request_uuids:
        requests = (
            await db.scalars(
                select(AppointmentRequest).where(
                    AppointmentRequest.id.in_(payload.request_uuids),
                    AppointmentRequest.patient_account_id == principal.account.id,
                )
            )
        ).all()
        items.extend(
            {"uuid": str(r.id), "item_type": "request", "status": r.status, "version": r.version}
            for r in requests
        )
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/queue-token")
async def own_queue_token(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Read-only own staff-issued token. No creation/call/skip/check-in."""
    _, links_by_patient = await _linked_patients(db, principal)
    patient_ids = list(links_by_patient.keys())
    if not patient_ids:
        return {"success": True, "data": {"item": None}, "meta": {}}
    entry = await db.scalar(
        select(QueueEntry)
        .where(
            QueueEntry.patient_id.in_(patient_ids),
            QueueEntry.status.in_(("waiting", "called", "in_consultation")),
        )
        .order_by(QueueEntry.queue_date.desc())
    )
    if entry is None:
        return {"success": True, "data": {"item": None}, "meta": {}}
    facility = await db.get(Facility, entry.facility_id)
    practitioner = await db.get(Practitioner, entry.practitioner_id) if entry.practitioner_id else None
    return {
        "success": True,
        "data": {
            "item": {
                "uuid": str(entry.id),
                "token_number": entry.token_number,
                "status": entry.status,
                "queue_date": entry.queue_date.isoformat(),
                "facility_uuid": str(entry.facility_id),
                "facility_name": facility.name if facility else None,
                "practitioner_name": practitioner.person_name if practitioner else None,
            }
        },
        "meta": {},
    }


# ---------------------------------------------------------------------------
# Released records
# ---------------------------------------------------------------------------


@router.get("/records/visits")
async def list_visits(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> dict[str, Any]:
    _, links_by_patient = await _linked_patients(db, principal)
    patient_ids = list(links_by_patient.keys())
    if not patient_ids:
        return {"success": True, "data": {"items": []}, "meta": {"count": 0, "has_more": False}}
    encounters = (
        await db.scalars(
            select(Encounter)
            .where(
                Encounter.patient_id.in_(patient_ids),
                Encounter.status == "completed",
            )
            .order_by(Encounter.completed_at.desc().nullslast(), Encounter.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    release_map = await _release_map(
        db, encounter_ids={e.id for e in encounters}, resource_type="encounter_summary"
    )
    facility_names = await _facility_names(db, {e.facility_id for e in encounters})
    items = []
    for encounter in encounters:
        release = release_map.get(encounter.id)
        items.append(
            {
                "uuid": str(encounter.id),
                "item_type": "visit",
                "visit_date": (encounter.completed_at or encounter.started_at).isoformat(),
                "facility_uuid": str(encounter.facility_id),
                "facility_name": facility_names.get(encounter.facility_id),
                "release_status": "released" if release else "not_released",
                "has_vitals": bool(
                    release and (release.snapshot or {}).get("vitals")
                ),
            }
        )
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items), "has_more": len(items) == limit},
    }


async def _release_map(
    db: AsyncSession, *, encounter_ids: set[UUID], resource_type: str
) -> dict[UUID, RecordRelease]:
    if not encounter_ids:
        return {}
    rows = await db.scalars(
        select(RecordRelease).where(
            RecordRelease.resource_type == resource_type,
            RecordRelease.encounter_id.in_(encounter_ids),
            RecordRelease.revoked_at.is_(None),
        )
    )
    return {row.encounter_id: row for row in rows if row.encounter_id}


async def _facility_names(db: AsyncSession, facility_ids: set[UUID]) -> dict[UUID, str]:
    if not facility_ids:
        return {}
    rows = await db.scalars(select(Facility).where(Facility.id.in_(facility_ids)))
    return {f.id: f.name for f in rows}


@router.get("/records/visits/{visit_uuid}")
async def visit_detail(
    visit_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter, _ = await _owned_encounter(db, principal, visit_uuid)
    release = await record_service.active_release(
        db, resource_type="encounter_summary", resource_id=encounter.id
    )
    if release is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RECORD_NOT_RELEASED", "message": "This visit summary has not been released.", "details": []},
        )
    return {
        "success": True,
        "data": {
            "visit": {
                "uuid": str(encounter.id),
                "facility_uuid": str(encounter.facility_id),
                "release_version": release.version,
                "released_at": release.released_at.isoformat(),
                "summary": release.snapshot,
            }
        },
        "meta": {},
    }


async def _owned_encounter(
    db: AsyncSession, principal: PatientPrincipal, encounter_uuid: UUID
) -> tuple[Encounter, Any]:
    encounter = await db.get(Encounter, encounter_uuid)
    if encounter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Visit not found.", "details": []})
    link = await patient_link_service.verified_link_for_patient(
        db, account_id=principal.account.id, patient_id=encounter.patient_id
    )
    if link is None or link.organization_id != encounter.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Visit not found.", "details": []})
    return encounter, link


@router.get("/records/prescriptions")
async def list_prescriptions(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> dict[str, Any]:
    _, links_by_patient = await _linked_patients(db, principal)
    patient_ids = list(links_by_patient.keys())
    if not patient_ids:
        return {"success": True, "data": {"items": []}, "meta": {"count": 0, "has_more": False}}
    releases = (
        await db.scalars(
            select(RecordRelease)
            .where(
                RecordRelease.patient_id.in_(patient_ids),
                RecordRelease.resource_type == "prescription",
                RecordRelease.revoked_at.is_(None),
            )
            .order_by(RecordRelease.released_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = []
    for release in releases:
        snapshot = release.snapshot or {}
        items.append(
            {
                "uuid": str(release.resource_id),
                "item_type": "prescription",
                "release_version": release.version,
                "released_at": release.released_at.isoformat(),
                "signed_at": snapshot.get("signed_at"),
                "facility_name": snapshot.get("facility_name"),
                "practitioner_name": snapshot.get("practitioner_name"),
                "item_count": len(snapshot.get("items") or []),
            }
        )
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items), "has_more": len(items) == limit},
    }


async def _owned_prescription_release(
    db: AsyncSession, principal: PatientPrincipal, prescription_uuid: UUID
) -> tuple[Prescription, RecordRelease]:
    prescription = await db.get(Prescription, prescription_uuid)
    if prescription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Prescription not found.", "details": []})
    encounter, _ = await _owned_encounter(db, principal, prescription.encounter_id)
    release = await record_service.active_release(
        db, resource_type="prescription", resource_id=prescription.id
    )
    if release is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RECORD_NOT_RELEASED", "message": "This prescription has not been released.", "details": []},
        )
    assert encounter is not None
    return prescription, release


@router.get("/records/prescriptions/{prescription_uuid}")
async def prescription_detail(
    prescription_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    _, release = await _owned_prescription_release(db, principal, prescription_uuid)
    return {
        "success": True,
        "data": {
            "prescription": {
                "uuid": str(release.resource_id),
                "release_version": release.version,
                "released_at": release.released_at.isoformat(),
                **release.snapshot,
            }
        },
        "meta": {},
    }


@router.get("/records/prescriptions/{prescription_uuid}/document")
async def prescription_document(
    prescription_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    _, release = await _owned_prescription_release(db, principal, prescription_uuid)
    patient = await record_service.patient_identity_for_release(
        db, patient_id=release.patient_id
    )
    pdf = record_service.render_prescription_pdf(
        snapshot=release.snapshot or {}, patient=patient
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="prescription-{prescription_uuid}.pdf"'
        },
    )


@router.get("/records/documents")
async def list_documents(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> dict[str, Any]:
    _, links_by_patient = await _linked_patients(db, principal)
    patient_ids = list(links_by_patient.keys())
    if not patient_ids:
        return {"success": True, "data": {"items": []}, "meta": {"count": 0, "has_more": False}}
    releases = (
        await db.scalars(
            select(RecordRelease)
            .where(
                RecordRelease.patient_id.in_(patient_ids),
                RecordRelease.resource_type == "patient_document",
                RecordRelease.revoked_at.is_(None),
            )
            .order_by(RecordRelease.released_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = []
    for release in releases:
        document = await db.get(PatientDocument, release.resource_id)
        if document is None or document.deleted_at is not None or document.status != "available":
            continue
        items.append(_document_payload(document, release))
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items), "has_more": len(items) == limit},
    }


def _document_payload(document: PatientDocument, release: RecordRelease | None) -> dict[str, Any]:
    snapshot = release.snapshot if release else {}
    return {
        "uuid": str(document.id),
        "item_type": "document",
        "title": snapshot.get("title") or document.title,
        "category": document.category,
        "document_date": document.document_date.isoformat(),
        "mime_type": document.mime_type or document.declared_mime_type,
        "size_bytes": document.size_bytes or document.expected_size_bytes,
        "release_version": release.version if release else None,
        "released_at": release.released_at.isoformat() if release else None,
    }


async def _owned_released_document(
    db: AsyncSession, principal: PatientPrincipal, document_uuid: UUID
) -> tuple[PatientDocument, RecordRelease]:
    document = await db.get(PatientDocument, document_uuid)
    if document is None or document.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Document not found.", "details": []})
    release = await record_service.active_release(
        db, resource_type="patient_document", resource_id=document.id
    )
    if release is None or release.patient_id != document.patient_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RECORD_NOT_RELEASED", "message": "This document has not been released.", "details": []},
        )
    link = await patient_link_service.verified_link_for_patient(
        db, account_id=principal.account.id, patient_id=document.patient_id
    )
    if link is None or link.organization_id != document.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Document not found.", "details": []})
    if document.status != "available" or not document.storage_generation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DOCUMENT_NOT_AVAILABLE", "message": "The document bytes are not available.", "details": []},
        )
    return document, release


@router.get("/records/documents/{document_uuid}")
async def document_detail(
    document_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    document, release = await _owned_released_document(db, principal, document_uuid)
    return {"success": True, "data": {"document": _document_payload(document, release)}, "meta": {}}


def _stream_document(
    document: PatientDocument, *, disposition: str, inline: bool
) -> StreamingResponse:
    from ...core.patient_document_storage import (
        StorageNotConfigured,
        open_object_stream,
    )

    try:
        stream = open_object_stream(
            document.storage_key, generation=document.storage_generation
        )
    except StorageNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "DOCUMENT_STORAGE_NOT_CONFIGURED", "message": "Document storage is not configured.", "details": [{"reason": str(exc)}]},
        ) from exc
    media_type = document.mime_type or document.declared_mime_type
    return StreamingResponse(
        iter(lambda: stream.read(1024 * 1024), b""),
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{document.original_filename}"'},
    )


@router.get("/records/documents/{document_uuid}/preview")
async def preview_document(
    document_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> StreamingResponse:
    document, _ = await _owned_released_document(db, principal, document_uuid)
    return _stream_document(document, disposition="inline", inline=True)


@router.get("/records/documents/{document_uuid}/download")
async def download_document(
    document_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> StreamingResponse:
    document, _ = await _owned_released_document(db, principal, document_uuid)
    return _stream_document(document, disposition="attachment", inline=False)


# ---------------------------------------------------------------------------
# Bills and receipts
# ---------------------------------------------------------------------------


async def _linked_patient_ids(db: AsyncSession, principal: PatientPrincipal) -> list[UUID]:
    _, links_by_patient = await _linked_patients(db, principal)
    return list(links_by_patient.keys())


async def _invoice_amounts(
    db: AsyncSession, invoice_id: UUID
) -> tuple[int, int, int, int]:
    paid = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
            Payment.invoice_id == invoice_id, Payment.status == "captured"
        )
    ) or 0
    refunded = await db.scalar(
        select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
            Refund.invoice_id == invoice_id, Refund.status == "completed"
        )
    ) or 0
    captured_count = await db.scalar(
        select(func.count(Payment.id)).where(
            Payment.invoice_id == invoice_id, Payment.status == "captured"
        )
    ) or 0
    return int(paid), int(refunded), int(captured_count), 0


def _invoice_payload(
    invoice: Invoice,
    *,
    paid: int,
    refunded: int,
    balance: int,
    receipt_available: bool,
) -> dict[str, Any]:
    return {
        "uuid": str(invoice.id),
        "encounter_uuid": str(invoice.encounter_id),
        "facility_uuid": str(invoice.facility_id),
        "status": invoice.status,
        "issued_at": invoice.issued_at.isoformat(),
        "currency": invoice.currency,
        "gross_amount_minor": invoice.gross_amount_minor if invoice.gross_amount_minor is not None else invoice.amount_minor,
        "discount_minor": invoice.discount_minor or 0,
        "discount_kind": invoice.discount_kind,
        "net_amount_minor": invoice.amount_minor,
        "paid_minor": paid,
        "refunded_minor": refunded,
        "balance_minor": balance,
        "receipt_available": receipt_available,
    }


@router.get("/bills")
async def list_bills(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> dict[str, Any]:
    patient_ids = await _linked_patient_ids(db, principal)
    if not patient_ids:
        return {"success": True, "data": {"items": []}, "meta": {"count": 0, "has_more": False}}
    invoices = (
        await db.scalars(
            select(Invoice)
            .where(Invoice.patient_id.in_(patient_ids))
            .order_by(Invoice.issued_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = []
    for invoice in invoices:
        paid, refunded, captured_count, _ = await _invoice_amounts(db, invoice.id)
        balance = max(invoice.amount_minor - (paid - refunded), 0)
        items.append(
            _invoice_payload(
                invoice,
                paid=paid,
                refunded=refunded,
                balance=balance,
                receipt_available=captured_count > 0,
            )
        )
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items), "has_more": len(items) == limit},
    }


@router.get("/bills/{bill_uuid}")
async def bill_detail(
    bill_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    invoice = await _owned_invoice(db, principal, bill_uuid)
    paid, refunded, captured_count, _ = await _invoice_amounts(db, invoice.id)
    balance = max(invoice.amount_minor - (paid - refunded), 0)
    payments = (
        await db.scalars(
            select(Payment)
            .where(Payment.invoice_id == invoice.id)
            .order_by(Payment.received_at.asc())
        )
    ).all()
    refunds = (
        await db.scalars(
            select(Refund)
            .where(Refund.invoice_id == invoice.id)
            .order_by(Refund.refunded_at.asc())
        )
    ).all()
    return {
        "success": True,
        "data": {
            "bill": _invoice_payload(
                invoice,
                paid=paid,
                refunded=refunded,
                balance=balance,
                receipt_available=captured_count > 0,
            ),
            "payments": [
                {
                    "uuid": str(payment.id),
                    "amount_minor": payment.amount_minor,
                    "status": payment.status,
                    "received_at": payment.received_at.isoformat(),
                    "receipt_available": payment.status == "captured",
                }
                for payment in payments
            ],
            "refunds": [
                {
                    "uuid": str(refund.id),
                    "amount_minor": refund.amount_minor,
                    "status": refund.status,
                    "refunded_at": refund.refunded_at.isoformat(),
                }
                for refund in refunds
            ],
        },
        "meta": {},
    }


async def _owned_invoice(
    db: AsyncSession, principal: PatientPrincipal, invoice_uuid: UUID
) -> Invoice:
    invoice = await db.get(Invoice, invoice_uuid)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Bill not found.", "details": []})
    link = await patient_link_service.verified_link_for_patient(
        db, account_id=principal.account.id, patient_id=invoice.patient_id
    )
    if link is None or link.organization_id != invoice.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Bill not found.", "details": []})
    return invoice


@router.get("/bills/{bill_uuid}/receipt")
async def bill_receipt(
    bill_uuid: UUID,
    payment_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    invoice = await _owned_invoice(db, principal, bill_uuid)
    payment = await db.get(Payment, payment_uuid)
    if payment is None or payment.invoice_id != invoice.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Payment not found.", "details": []})
    try:
        receipt = await billing_service.get_or_create_receipt(
            db, invoice=invoice, payment=payment
        )
    except billing_service.BillingPortalError as exc:
        raise _http_error(exc) from exc
    paid, refunded, _, _ = await _invoice_amounts(db, invoice.id)
    balance = max(invoice.amount_minor - (paid - refunded), 0)
    facility = await db.get(Facility, invoice.facility_id)
    patient = await record_service.patient_identity_for_release(
        db, patient_id=invoice.patient_id
    )
    pdf = billing_service.render_receipt_pdf(
        receipt=receipt,
        invoice=invoice,
        payment=payment,
        facility_name=facility.name if facility else None,
        patient=patient,
        paid_to_date_minor=paid,
        refunded_minor=refunded,
        balance_minor=balance,
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="receipt-{receipt.receipt_number}.pdf"'},
    )


# ---------------------------------------------------------------------------
# Notifications, preferences, push
# ---------------------------------------------------------------------------


def _notification_payload(notification: Any) -> dict[str, Any]:
    return {
        "uuid": str(notification.id),
        "kind": notification.kind,
        "category": notification.category,
        "title": notification.title,
        "body": notification.body,
        "deep_link": notification.deep_link,
        "read": notification.read_at is not None,
        "created_at": notification.created_at.isoformat(),
    }


@router.get("/notifications")
async def list_notifications(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    unread: bool = False,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> dict[str, Any]:
    rows, total, unread_count = await notify_service.list_notifications(
        db,
        account_id=principal.account.id,
        unread_only=unread,
        limit=limit,
        offset=offset,
    )
    return {
        "success": True,
        "data": {"items": [_notification_payload(n) for n in rows]},
        "meta": {"count": len(rows), "total": total, "unread": unread_count},
    }


@router.get("/notifications/{notification_uuid}")
async def notification_detail(
    notification_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    from ...models.communication import PatientNotification

    notification = await db.scalar(
        select(PatientNotification).where(
            PatientNotification.id == notification_uuid,
            PatientNotification.patient_account_id == principal.account.id,
        )
    )
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Notification not found.", "details": []})
    return {"success": True, "data": {"notification": _notification_payload(notification)}, "meta": {}}


@router.post("/notifications/{notification_uuid}/read")
async def mark_notification_read(
    notification_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    notification = await notify_service.mark_read(
        db, account_id=principal.account.id, notification_id=notification_uuid
    )
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "NOT_FOUND", "message": "Notification not found.", "details": []})
    return {"success": True, "data": {"notification": _notification_payload(notification)}, "meta": {}}


@router.get("/preferences/communication")
async def communication_preferences(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    preference = await notify_service.get_preferences(db, account_id=principal.account.id)
    return {
        "success": True,
        "data": {
            "preferences": {
                "in_app": preference.in_app,
                "push": preference.push,
                "push_available": notify_service.push_status()["available"],
            }
        },
        "meta": {},
    }


@router.put("/preferences/communication")
async def update_communication_preferences(
    payload: PreferenceUpdateBody,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    preference = await notify_service.update_preferences(
        db, account_id=principal.account.id, in_app=payload.in_app, push=payload.push
    )
    return {
        "success": True,
        "data": {
            "preferences": {
                "in_app": preference.in_app,
                "push": preference.push,
                "push_available": notify_service.push_status()["available"],
            }
        },
        "meta": {},
    }


@router.get("/push/status")
async def push_status(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
) -> dict[str, Any]:
    return {"success": True, "data": notify_service.push_status(), "meta": {}}


@router.post("/push-devices", status_code=status.HTTP_201_CREATED)
async def register_push_device(
    payload: PushDeviceBody,
    request: Request,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    device = await notify_service.register_device(
        db,
        account_id=principal.account.id,
        installation_id=payload.installation_id,
        token=payload.token,
        platform=payload.platform,
        user_agent=request.headers.get("User-Agent"),
    )
    return {
        "success": True,
        "data": {
            "device": {
                "uuid": str(device.id),
                "platform": device.platform,
                "is_active": device.is_active,
            },
            "push_available": notify_service.push_status()["available"],
        },
        "meta": {"delivery": "push_unavailable_until_configured" if not notify_service.push_status()["available"] else "in_app_always"},
    }


@router.delete("/push-devices/{device_uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_push_device(
    device_uuid: UUID,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    await notify_service.revoke_device(
        db, account_id=principal.account.id, device_id=device_uuid
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Clinic support data
# ---------------------------------------------------------------------------


@router.get("/support")
async def clinic_support(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    links, _ = await _linked_patients(db, principal)
    items = []
    for link in links:
        organization = await db.get(Organization, link.organization_id)
        facility = await db.get(Facility, link.facility_id) if link.facility_id else None
        items.append(
            {
                "organization_uuid": str(link.organization_id),
                "organization_name": organization.name if organization else None,
                "facility_uuid": str(facility.id) if facility else None,
                "facility_name": facility.name if facility else None,
                "facility_phone": facility.phone if facility else None,
                "facility_address": facility.street_address if facility else None,
            }
        )
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}
