"""Request-first appointment workflow for the patient portal.

Request status is separate from legacy Appointment status. Pending requests do
not reserve capacity: approval (staff or auto-confirm policy) revalidates the
slot atomically with the facility row locked and then creates/moves the
Appointment transactionally. Identical idempotent retries replay; the same key
with a different payload is rejected.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.availability import validate_interval
from ...models.care import Appointment, AppointmentRequest, Practitioner
from ...models.identity import PatientPortalAccount, PatientRecordLink
from ...models.organization import Facility
from ..communication.notifications.workflow import emit_appointment_request_created
from ..governance.audit import record_audit
from .notifications import create_patient_notification

REQUEST_TTL_HOURS = 72

PENDING_STATUSES = ("requested", "alternative_proposed")

# Explicit API-to-patient-label mapping. Legacy appointment statuses never leak
# raw staff vocabulary; `confirmed` is a patient label, not a stored status.
APPOINTMENT_PATIENT_LABELS = {
    "booked": "confirmed",
    "confirmed": "confirmed",
    "checked_in": "in_progress",
    "in_consultation": "in_progress",
    "completed": "completed",
    "cancelled": "cancelled",
    "no_show": "missed",
}


class AppointmentRequestError(RuntimeError):
    code = "APPOINTMENT_REQUEST_ERROR"


class RequestNotFound(AppointmentRequestError):
    code = "NOT_FOUND"


class RequestNotPending(AppointmentRequestError):
    code = "REQUEST_NOT_PENDING"


class RequestExpired(AppointmentRequestError):
    code = "REQUEST_EXPIRED"


class SlotUnavailable(AppointmentRequestError):
    code = "SLOT_UNAVAILABLE"

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


class IdempotencyConflict(AppointmentRequestError):
    code = "IDEMPOTENCY_KEY_REUSED"


class RequestValidation(AppointmentRequestError):
    code = "VALIDATION_ERROR"


def request_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def patient_appointment_label(status: str) -> str:
    return APPOINTMENT_PATIENT_LABELS.get(status, "unknown")


def _slot_details(exc: ValueError) -> list[dict[str, Any]]:
    text = str(exc)
    code, _, message = text.partition("|")
    return [{"availability_code": code or "VALIDATION", "message": message or text}]


async def _validate_slot(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID,
    start: datetime,
    end: datetime,
    resource_id: UUID | None = None,
    ignore_appointment_id: UUID | None = None,
) -> UUID | None:
    await db.scalar(select(Facility.id).where(Facility.id == facility_id).with_for_update())
    try:
        return await validate_interval(
            db,
            organization_id=organization_id,
            facility_id=facility_id,
            practitioner_id=practitioner_id,
            start=start,
            end=end,
            resource_id=resource_id,
            ignore_appointment_id=ignore_appointment_id,
        )
    except ValueError as exc:
        raise SlotUnavailable(
            "The selected slot is no longer available. Choose another time.",
            _slot_details(exc),
        ) from exc


async def expire_stale_requests(
    db: AsyncSession, *, organization_id: UUID | None = None, account_id: UUID | None = None
) -> int:
    now = datetime.now(UTC)
    query = (
        update(AppointmentRequest)
        .where(
            AppointmentRequest.status.in_(PENDING_STATUSES),
            AppointmentRequest.expires_at <= now,
        )
        .values(status="expired", updated_at=now)
    )
    if organization_id is not None:
        query = query.where(AppointmentRequest.organization_id == organization_id)
    if account_id is not None:
        query = query.where(AppointmentRequest.patient_account_id == account_id)
    result = await db.execute(query)
    if result.rowcount:
        await db.commit()
    return int(result.rowcount or 0)


async def _find_by_idempotency(
    db: AsyncSession, *, organization_id: UUID, idempotency_key: str
) -> AppointmentRequest | None:
    return await db.scalar(
        select(AppointmentRequest).where(
            AppointmentRequest.organization_id == organization_id,
            AppointmentRequest.idempotency_key == idempotency_key,
        )
    )


async def _create_request_row(
    db: AsyncSession,
    *,
    account: PatientPortalAccount,
    link: PatientRecordLink,
    facility: Facility,
    practitioner_id: UUID | None,
    kind: str,
    appointment_id: UUID | None,
    requested_start: datetime | None,
    requested_end: datetime | None,
    reason_code: str | None,
    reason_text: str | None,
    idempotency_key: str | None,
    payload_hash: str | None,
) -> tuple[AppointmentRequest, bool]:
    if idempotency_key:
        existing = await _find_by_idempotency(
            db, organization_id=link.organization_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            if existing.payload_hash and existing.payload_hash != payload_hash:
                raise IdempotencyConflict(
                    "This idempotency key was already used with a different request."
                )
            return existing, True
    now = datetime.now(UTC)
    request = AppointmentRequest(
        organization_id=link.organization_id,
        facility_id=facility.id,
        patient_account_id=account.id,
        patient_id=link.patient_id,
        practitioner_id=practitioner_id,
        appointment_id=appointment_id,
        kind=kind,
        status="requested",
        requested_start=requested_start,
        requested_end=requested_end,
        reason_code=reason_code,
        reason_text=reason_text,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        expires_at=now + timedelta(hours=REQUEST_TTL_HOURS),
    )
    db.add(request)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        if idempotency_key:
            existing = await _find_by_idempotency(
                db, organization_id=link.organization_id, idempotency_key=idempotency_key
            )
            if existing is not None and existing.payload_hash == payload_hash:
                return existing, True
        raise
    await record_audit(
        db,
        organization_id=link.organization_id,
        facility_id=facility.id,
        actor_user_id=None,
        action="patient.appointment_request_created",
        resource_type="appointment_request",
        resource_id=request.id,
        patient_id=link.patient_id,
        details={"kind": kind, "requested_start": requested_start.isoformat() if requested_start else None},
    )
    return request, False


async def _auto_confirm(
    db: AsyncSession,
    *,
    account: PatientPortalAccount,
    request: AppointmentRequest,
    facility: Facility,
    practitioner_id: UUID,
    start: datetime,
    end: datetime,
    resource_id: UUID | None,
) -> bool:
    if not facility.portal_auto_confirm:
        return False
    try:
        resolved = await _validate_slot(
            db,
            organization_id=request.organization_id,
            facility_id=request.facility_id,
            practitioner_id=practitioner_id,
            start=start,
            end=end,
            resource_id=resource_id,
        )
    except SlotUnavailable:
        return False
    appointment = Appointment(
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        practitioner_id=practitioner_id,
        patient_id=request.patient_id,
        scheduled_start=start,
        scheduled_end=end,
        resource_id=resolved,
        reason_code=request.reason_code,
        reason_text=request.reason_text,
        status="booked",
        idempotency_key=f"portal-request:{request.id}",
    )
    db.add(appointment)
    await db.flush()
    request.appointment_id = appointment.id
    request.status = "approved"
    request.decision_reason = "auto_confirmed_by_facility_policy"
    request.decided_at = datetime.now(UTC)
    request.updated_at = request.decided_at
    await record_audit(
        db,
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        actor_user_id=None,
        action="patient.appointment_request_auto_confirmed",
        resource_type="appointment_request",
        resource_id=request.id,
        patient_id=request.patient_id,
        details={"appointment_id": str(appointment.id), "policy": "facility_portal_auto_confirm"},
    )
    await create_patient_notification(
        db,
        account_id=account.id,
        kind="appointment_confirmed",
        category="appointments",
        title="Appointment confirmed",
        body="Your appointment has been confirmed as requested.",
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        deep_link=f"/appointments/{appointment.id}",
        dedup_key=f"patient:request:{request.id}:approved:1",
    )
    return True


async def create_new_request(
    db: AsyncSession,
    *,
    account: PatientPortalAccount,
    link: PatientRecordLink,
    facility: Facility,
    practitioner: Practitioner,
    start: datetime,
    end: datetime,
    reason_code: str | None,
    reason_text: str | None,
    idempotency_key: str | None,
) -> tuple[AppointmentRequest, bool]:
    fingerprint = request_fingerprint(
        {
            "kind": "new",
            "facility": str(facility.id),
            "practitioner": str(practitioner.id),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "reason_code": reason_code,
            "reason_text": reason_text,
        }
    )
    request, replayed = await _create_request_row(
        db,
        account=account,
        link=link,
        facility=facility,
        practitioner_id=practitioner.id,
        kind="new",
        appointment_id=None,
        requested_start=start,
        requested_end=end,
        reason_code=reason_code,
        reason_text=reason_text,
        idempotency_key=idempotency_key,
        payload_hash=fingerprint,
    )
    if replayed:
        return request, True
    # Soft availability check at request time; selection is explicitly not a
    # reservation and is revalidated on approval.
    try:
        await _validate_slot(
            db,
            organization_id=link.organization_id,
            facility_id=facility.id,
            practitioner_id=practitioner.id,
            start=start,
            end=end,
        )
    except SlotUnavailable:
        await db.rollback()
        raise
    if await _auto_confirm(
        db,
        account=account,
        request=request,
        facility=facility,
        practitioner_id=practitioner.id,
        start=start,
        end=end,
        resource_id=None,
    ):
        await db.commit()
        await db.refresh(request)
        return request, False
    # Still pending review: notify facility reception/admins in the same
    # transaction (in-app + realtime + push outbox). Auto-confirmed requests
    # are not sent to staff for review.
    await emit_appointment_request_created(db, request=request)
    await db.commit()
    await db.refresh(request)
    return request, False


async def create_change_request(
    db: AsyncSession,
    *,
    account: PatientPortalAccount,
    link: PatientRecordLink,
    facility: Facility,
    appointment: Appointment,
    kind: str,
    start: datetime | None,
    end: datetime | None,
    reason: str | None,
    idempotency_key: str | None,
) -> tuple[AppointmentRequest, bool]:
    fingerprint = request_fingerprint(
        {
            "kind": kind,
            "appointment": str(appointment.id),
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "reason": reason,
        }
    )
    request, replayed = await _create_request_row(
        db,
        account=account,
        link=link,
        facility=facility,
        practitioner_id=appointment.practitioner_id,
        kind=kind,
        appointment_id=appointment.id,
        requested_start=start,
        requested_end=end,
        reason_code=None,
        reason_text=reason,
        idempotency_key=idempotency_key,
        payload_hash=fingerprint,
    )
    if replayed:
        return request, True
    if kind == "reschedule" and start is not None and end is not None:
        try:
            await _validate_slot(
                db,
                organization_id=link.organization_id,
                facility_id=appointment.facility_id,
                practitioner_id=appointment.practitioner_id,
                start=start,
                end=end,
                ignore_appointment_id=appointment.id,
            )
        except SlotUnavailable:
            await db.rollback()
            raise
    await db.commit()
    await db.refresh(request)
    return request, False


async def approve_request(
    db: AsyncSession,
    *,
    request: AppointmentRequest,
    actor_user_id: UUID,
    decision_reason: str | None = None,
) -> AppointmentRequest:
    await expire_stale_requests(db, organization_id=request.organization_id)
    request = await db.scalar(
        select(AppointmentRequest)
        .where(AppointmentRequest.id == request.id)
        .with_for_update()
    )
    assert request is not None
    if request.status not in PENDING_STATUSES:
        raise RequestNotPending("This request is no longer pending.")
    now = datetime.now(UTC)
    if request.kind == "new":
        resolved = await _validate_slot(
            db,
            organization_id=request.organization_id,
            facility_id=request.facility_id,
            practitioner_id=request.practitioner_id,
            start=request.requested_start,
            end=request.requested_end,
        )
        appointment = Appointment(
            organization_id=request.organization_id,
            facility_id=request.facility_id,
            practitioner_id=request.practitioner_id,
            patient_id=request.patient_id,
            scheduled_start=request.requested_start,
            scheduled_end=request.requested_end,
            resource_id=resolved,
            reason_code=request.reason_code,
            reason_text=request.reason_text,
            status="booked",
            idempotency_key=f"portal-request:{request.id}",
        )
        db.add(appointment)
        await db.flush()
        request.appointment_id = appointment.id
    elif request.kind == "reschedule":
        appointment = await db.get(Appointment, request.appointment_id)
        if appointment is None or appointment.status not in {"booked", "confirmed"}:
            raise RequestNotPending("The original appointment can no longer be rescheduled.")
        resolved = await _validate_slot(
            db,
            organization_id=request.organization_id,
            facility_id=appointment.facility_id,
            practitioner_id=appointment.practitioner_id,
            start=request.requested_start,
            end=request.requested_end,
            ignore_appointment_id=appointment.id,
        )
        appointment.scheduled_start = request.requested_start
        appointment.scheduled_end = request.requested_end
        appointment.resource_id = resolved
        appointment.version += 1
    else:  # cancel
        appointment = await db.get(Appointment, request.appointment_id)
        if appointment is None or appointment.status in {"completed", "cancelled", "no_show"}:
            raise RequestNotPending("The original appointment can no longer be cancelled.")
        appointment.status = "cancelled"
        appointment.version += 1
    request.status = "approved"
    request.decided_by_user_id = actor_user_id
    request.decided_at = now
    request.decision_reason = decision_reason
    request.version += 1
    request.updated_at = now
    await record_audit(
        db,
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        actor_user_id=actor_user_id,
        action="patient.appointment_request_approved",
        resource_type="appointment_request",
        resource_id=request.id,
        patient_id=request.patient_id,
        details={"kind": request.kind, "appointment_id": str(appointment.id) if appointment else None},
    )
    await create_patient_notification(
        db,
        account_id=request.patient_account_id,
        kind="appointment_confirmed",
        category="appointments",
        title="Appointment confirmed",
        body="Your request was reviewed and is now confirmed.",
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        deep_link=f"/appointments/{appointment.id}" if appointment else "/appointments",
        dedup_key=f"patient:request:{request.id}:approved:{request.version}",
    )
    await db.commit()
    await db.refresh(request)
    return request


async def reject_request(
    db: AsyncSession,
    *,
    request: AppointmentRequest,
    actor_user_id: UUID,
    reason: str | None,
) -> AppointmentRequest:
    await expire_stale_requests(db, organization_id=request.organization_id)
    request = await db.scalar(
        select(AppointmentRequest)
        .where(AppointmentRequest.id == request.id)
        .with_for_update()
    )
    if request is None or request.status not in PENDING_STATUSES:
        raise RequestNotPending("This request is no longer pending.")
    now = datetime.now(UTC)
    request.status = "rejected"
    request.decision_reason = reason
    request.decided_by_user_id = actor_user_id
    request.decided_at = now
    request.version += 1
    request.updated_at = now
    await record_audit(
        db,
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        actor_user_id=actor_user_id,
        action="patient.appointment_request_rejected",
        resource_type="appointment_request",
        resource_id=request.id,
        patient_id=request.patient_id,
        details={"reason": reason},
    )
    await create_patient_notification(
        db,
        account_id=request.patient_account_id,
        kind="appointment_rejected",
        category="appointments",
        title="Appointment request declined",
        body=reason or "The clinic could not confirm the requested time.",
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        deep_link=f"/appointments/requests/{request.id}",
        dedup_key=f"patient:request:{request.id}:rejected:{request.version}",
    )
    await db.commit()
    await db.refresh(request)
    return request


async def propose_alternative(
    db: AsyncSession,
    *,
    request: AppointmentRequest,
    actor_user_id: UUID,
    start: datetime,
    end: datetime,
    resource_id: UUID | None,
    note: str | None,
) -> AppointmentRequest:
    await expire_stale_requests(db, organization_id=request.organization_id)
    request = await db.scalar(
        select(AppointmentRequest)
        .where(AppointmentRequest.id == request.id)
        .with_for_update()
    )
    if request is None or request.status not in PENDING_STATUSES:
        raise RequestNotPending("This request is no longer pending.")
    resolved = await _validate_slot(
        db,
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        practitioner_id=request.practitioner_id,
        start=start,
        end=end,
        resource_id=resource_id,
        ignore_appointment_id=request.appointment_id,
    )
    now = datetime.now(UTC)
    request.status = "alternative_proposed"
    request.alternative_start = start
    request.alternative_end = end
    request.alternative_resource_id = resolved
    request.alternative_note = note
    request.expires_at = now + timedelta(hours=REQUEST_TTL_HOURS)
    request.version += 1
    request.updated_at = now
    await record_audit(
        db,
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        actor_user_id=actor_user_id,
        action="patient.appointment_request_alternative_proposed",
        resource_type="appointment_request",
        resource_id=request.id,
        patient_id=request.patient_id,
        details={"start": start.isoformat(), "end": end.isoformat()},
    )
    await create_patient_notification(
        db,
        account_id=request.patient_account_id,
        kind="appointment_alternative",
        category="appointments",
        title="Alternative time proposed",
        body="The clinic proposed a different time for your appointment.",
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        deep_link=f"/appointments/requests/{request.id}",
        dedup_key=f"patient:request:{request.id}:alternative:{request.version}",
    )
    await db.commit()
    await db.refresh(request)
    return request


async def respond_to_alternative(
    db: AsyncSession,
    *,
    request: AppointmentRequest,
    account: PatientPortalAccount,
    accept: bool,
) -> AppointmentRequest:
    await expire_stale_requests(db, organization_id=request.organization_id)
    request = await db.scalar(
        select(AppointmentRequest)
        .where(AppointmentRequest.id == request.id)
        .with_for_update()
    )
    if request is None or request.patient_account_id != account.id:
        raise RequestNotFound("Request not found.")
    if request.status == "expired":
        raise RequestExpired("This request has expired.")
    if request.status != "alternative_proposed":
        raise RequestNotPending("No alternative is awaiting a response.")
    now = datetime.now(UTC)
    if not accept:
        request.status = "withdrawn"
        request.decision_reason = "alternative_declined_by_patient"
        request.decided_at = now
        request.version += 1
        request.updated_at = now
        await record_audit(
            db,
            organization_id=request.organization_id,
            facility_id=request.facility_id,
            actor_user_id=None,
            action="patient.appointment_request_withdrawn",
            resource_type="appointment_request",
            resource_id=request.id,
            patient_id=request.patient_id,
            details={"reason": "alternative_declined"},
        )
        await db.commit()
        await db.refresh(request)
        return request
    if request.kind == "new":
        resolved = await _validate_slot(
            db,
            organization_id=request.organization_id,
            facility_id=request.facility_id,
            practitioner_id=request.practitioner_id,
            start=request.alternative_start,
            end=request.alternative_end,
            resource_id=request.alternative_resource_id,
        )
        appointment = Appointment(
            organization_id=request.organization_id,
            facility_id=request.facility_id,
            practitioner_id=request.practitioner_id,
            patient_id=request.patient_id,
            scheduled_start=request.alternative_start,
            scheduled_end=request.alternative_end,
            resource_id=resolved,
            reason_code=request.reason_code,
            reason_text=request.reason_text,
            status="booked",
            idempotency_key=f"portal-request:{request.id}",
        )
        db.add(appointment)
        await db.flush()
        request.appointment_id = appointment.id
    elif request.kind == "reschedule":
        appointment = await db.get(Appointment, request.appointment_id)
        if appointment is None or appointment.status not in {"booked", "confirmed"}:
            raise RequestNotPending("The original appointment can no longer be rescheduled.")
        resolved = await _validate_slot(
            db,
            organization_id=request.organization_id,
            facility_id=appointment.facility_id,
            practitioner_id=appointment.practitioner_id,
            start=request.alternative_start,
            end=request.alternative_end,
            resource_id=request.alternative_resource_id,
            ignore_appointment_id=appointment.id,
        )
        appointment.scheduled_start = request.alternative_start
        appointment.scheduled_end = request.alternative_end
        appointment.resource_id = resolved
        appointment.version += 1
    else:
        appointment = await db.get(Appointment, request.appointment_id)
        if appointment is None or appointment.status in {"completed", "cancelled", "no_show"}:
            raise RequestNotPending("The original appointment can no longer be cancelled.")
        appointment.status = "cancelled"
        appointment.version += 1
    request.status = "approved"
    request.decision_reason = "alternative_accepted_by_patient"
    request.decided_at = now
    request.version += 1
    request.updated_at = now
    await record_audit(
        db,
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        actor_user_id=None,
        action="patient.appointment_request_alternative_accepted",
        resource_type="appointment_request",
        resource_id=request.id,
        patient_id=request.patient_id,
        details={"appointment_id": str(appointment.id) if appointment else None},
    )
    await create_patient_notification(
        db,
        account_id=request.patient_account_id,
        kind="appointment_confirmed",
        category="appointments",
        title="Appointment confirmed",
        body="The proposed time is confirmed.",
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        deep_link=f"/appointments/{appointment.id}" if appointment else "/appointments",
        dedup_key=f"patient:request:{request.id}:approved:{request.version}",
    )
    await db.commit()
    await db.refresh(request)
    return request


async def withdraw_request(
    db: AsyncSession,
    *,
    request: AppointmentRequest,
    account: PatientPortalAccount,
) -> AppointmentRequest:
    request = await db.scalar(
        select(AppointmentRequest)
        .where(AppointmentRequest.id == request.id)
        .with_for_update()
    )
    if request is None or request.patient_account_id != account.id:
        raise RequestNotFound("Request not found.")
    if request.status not in PENDING_STATUSES:
        raise RequestNotPending("This request can no longer be withdrawn.")
    now = datetime.now(UTC)
    request.status = "withdrawn"
    request.decided_at = now
    request.decision_reason = "withdrawn_by_patient"
    request.version += 1
    request.updated_at = now
    await record_audit(
        db,
        organization_id=request.organization_id,
        facility_id=request.facility_id,
        actor_user_id=None,
        action="patient.appointment_request_withdrawn",
        resource_type="appointment_request",
        resource_id=request.id,
        patient_id=request.patient_id,
    )
    await db.commit()
    await db.refresh(request)
    return request
