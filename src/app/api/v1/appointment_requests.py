"""Staff review of patient appointment requests.

Approval/rejection/alternative proposals are explicit staff actions gated by
organization/facility scope. Approval revalidates the slot atomically.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import normalize_range
from ...domains.patient_portal import appointments as appt_service
from ...models.care import AppointmentRequest
from ...models.identity import Patient, Person, UserAccount
from ...models.organization import Facility, Organization, StaffAssignment
from ...schemas.patient_domain import StaffAlternativeBody, StaffDecisionBody
from .bootstrap import ADMIN_ROLES, _staff_context
from .scheduling import _facility_timezone, _scope

router = APIRouter(prefix="/appointment-requests", tags=["appointment-requests"])

REVIEW_ROLES = set(ADMIN_ROLES) | {
    "practitioner",
    "doctor",
    "clinical_practitioner",
    "nurse",
    "receptionist",
    "facility_operator",
}


async def _review_context(
    db: AsyncSession,
    account: UserAccount,
    facility_uuid: UUID | None = None,
) -> tuple[Any, Organization, Facility | None]:
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
    if not roles & REVIEW_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Appointment request review access is required.",
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
    if isinstance(exc, appt_service.SlotUnavailable):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        )
    if isinstance(exc, (appt_service.RequestNotPending, appt_service.RequestExpired)):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    raise exc


async def _request_for_org(
    db: AsyncSession, organization_id: UUID, request_uuid: UUID
) -> AppointmentRequest:
    request = await db.scalar(
        select(AppointmentRequest).where(
            AppointmentRequest.id == request_uuid,
            AppointmentRequest.organization_id == organization_id,
        )
    )
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Request not found.", "details": []},
        )
    return request


def _payload(request: AppointmentRequest, patient: Patient | None, person: Person | None) -> dict[str, Any]:
    return {
        "uuid": str(request.id),
        "kind": request.kind,
        "status": request.status,
        "organization_uuid": str(request.organization_id),
        "facility_uuid": str(request.facility_id),
        "practitioner_uuid": str(request.practitioner_id) if request.practitioner_id else None,
        "patient_uuid": str(request.patient_id),
        "patient_name": (
            f"{person.first_name} {person.last_name or ''}".strip() if person else None
        ),
        "mrn": patient.mrn if patient else None,
        "appointment_uuid": str(request.appointment_id) if request.appointment_id else None,
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
        "expires_at": request.expires_at.isoformat(),
        "version": request.version,
        "created_at": request.created_at.isoformat() if request.created_at else None,
    }


@router.get("")
async def list_requests(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    status_filter: str = Query(default="pending", pattern="^(pending|approved|rejected|alternative_proposed|withdrawn|expired|all)$"),
    facility_uuid: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=500),
) -> dict[str, Any]:
    _, organization, facility = await _review_context(db, account, facility_uuid)
    await appt_service.expire_stale_requests(db, organization_id=organization.id)
    query = select(AppointmentRequest).where(
        AppointmentRequest.organization_id == organization.id
    )
    if status_filter == "pending":
        query = query.where(
            AppointmentRequest.status.in_(("requested", "alternative_proposed"))
        )
    elif status_filter != "all":
        query = query.where(AppointmentRequest.status == status_filter)
    if facility is not None:
        query = query.where(AppointmentRequest.facility_id == facility.id)
    requests = (
        await db.scalars(
            query.order_by(AppointmentRequest.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    patient_ids = {r.patient_id for r in requests}
    patients: dict[UUID, tuple[Patient, Person | None]] = {}
    if patient_ids:
        rows = (
            await db.execute(
                select(Patient, Person)
                .outerjoin(Person, Person.id == Patient.person_id)
                .where(Patient.id.in_(patient_ids))
            )
        ).all()
        patients = {p.id: (p, person) for p, person in rows}
    items = []
    for request in requests:
        patient, person = patients.get(request.patient_id, (None, None))
        items.append(_payload(request, patient, person))
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.post("/{request_uuid}/approve")
async def approve_request(
    request_uuid: UUID,
    payload: StaffDecisionBody,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, _ = await _review_context(db, account)
    request = await _request_for_org(db, organization.id, request_uuid)
    try:
        request = await appt_service.approve_request(
            db,
            request=request,
            actor_user_id=staff.user_account_id,
            decision_reason=payload.reason,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    patient = await db.get(Patient, request.patient_id)
    person = await db.get(Person, patient.person_id) if patient else None
    return {"success": True, "data": _payload(request, patient, person), "meta": {}}


@router.post("/{request_uuid}/reject")
async def reject_request(
    request_uuid: UUID,
    payload: StaffDecisionBody,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, _ = await _review_context(db, account)
    request = await _request_for_org(db, organization.id, request_uuid)
    try:
        request = await appt_service.reject_request(
            db, request=request, actor_user_id=staff.user_account_id, reason=payload.reason
        )
    except Exception as exc:
        raise _translate(exc) from exc
    patient = await db.get(Patient, request.patient_id)
    person = await db.get(Person, patient.person_id) if patient else None
    return {"success": True, "data": _payload(request, patient, person), "meta": {}}


@router.post("/{request_uuid}/propose-alternative")
async def propose_alternative(
    request_uuid: UUID,
    payload: StaffAlternativeBody,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, _ = await _review_context(db, account)
    request = await _request_for_org(db, organization.id, request_uuid)
    try:
        start, end = normalize_range(
            payload.scheduled_start,
            payload.scheduled_end,
            await _facility_timezone(db, request.facility_id),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "VALIDATION_ERROR", "message": str(exc), "details": []},
        ) from exc
    try:
        request = await appt_service.propose_alternative(
            db,
            request=request,
            actor_user_id=staff.user_account_id,
            start=start,
            end=end,
            resource_id=payload.resource_uuid,
            note=payload.note,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    patient = await db.get(Patient, request.patient_id)
    person = await db.get(Person, patient.person_id) if patient else None
    return {"success": True, "data": _payload(request, patient, person), "meta": {}}
