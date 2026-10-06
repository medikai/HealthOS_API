"""Staff-authorized patient portal link management.

Creating an invitation, approving/filing a patient link request, and revoking a
verified link are explicit staff actions. Clinical patients are resolved through
the existing organization-scoped patient workflow (``POST /api/v1/patients``);
this router never auto-creates or name/DOB/phone-matches clinical records.
Non-admin staff are constrained to their assigned facilities.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.config import settings
from ...core.db.database import async_get_db
from ...domains.patient_auth.links import (
    ClinicalPatientInvalid,
    InvitationNotPending,
    LinkAlreadyExists,
    LinkNotPending,
    PatientLinkError,
    PatientLinkService,
)
from ...domains.patient_auth.phone import InvalidPhoneNumber, mask_phone
from ...models.identity import (
    Patient,
    PatientLinkInvitation,
    PatientPortalAccount,
    PatientRecordLink,
    Person,
    UserAccount,
)
from ...models.organization import Facility, Organization, StaffAssignment
from ...schemas.patient_portal import (
    StaffPatientInvitationCreate,
    StaffPatientLinkApprove,
    StaffPatientLinkReject,
    StaffPatientLinkRevoke,
)
from .bootstrap import ADMIN_ROLES, _staff_context
from .scheduling import _scope

router = APIRouter(prefix="/patient-links", tags=["patient-links"])

LINK_MANAGER_ROLES = set(ADMIN_ROLES) | {
    "practitioner",
    "doctor",
    "clinical_practitioner",
    "nurse",
    "receptionist",
    "facility_operator",
}


def _portal_pepper() -> str:
    if settings.PATIENT_OTP_PEPPER is not None:
        return settings.PATIENT_OTP_PEPPER.get_secret_value()
    return settings.SECRET_KEY.get_secret_value()


patient_link_service = PatientLinkService(
    pepper=_portal_pepper(),
    invitation_ttl_hours=settings.PATIENT_LINK_INVITATION_TTL_HOURS,
    invitation_max_ttl_hours=settings.PATIENT_LINK_INVITATION_MAX_TTL_HOURS,
)


def _forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=message)


async def _link_context(
    db: AsyncSession,
    account: UserAccount,
    facility_uuid: UUID | None = None,
) -> tuple[Any, Organization, Facility | None, list[UUID] | None]:
    """Resolve staff link authority.

    Returns ``allowed_facility_ids=None`` for organization admins (org-wide) or
    the explicit facility set for other roles; non-admins with no active
    facility assignment are denied.
    """
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
    if not roles & LINK_MANAGER_ROLES:
        raise _forbidden("Patient link management access is required.")
    allowed: list[UUID] | None = None
    if not roles & set(ADMIN_ROLES):
        facility_rows = (
            await db.scalars(
                select(StaffAssignment.facility_id).where(
                    StaffAssignment.staff_member_id == staff.id,
                    StaffAssignment.is_active.is_(True),
                    StaffAssignment.facility_id.is_not(None),
                )
            )
        ).all()
        allowed = sorted({row for row in facility_rows if row is not None})
        if not allowed:
            raise _forbidden("Facility access is not permitted.")
    facility: Facility | None = None
    if facility_uuid is not None:
        _, facility = await _scope(db, account, facility_uuid)
        if facility is None or facility.organization_id != organization.id:
            raise _forbidden("Facility access is not permitted.")
        if allowed is not None and facility.id not in allowed:
            raise _forbidden("Facility access is not permitted.")
    return staff, organization, facility, allowed


def _ensure_link_scoped(
    link: PatientLinkInvitation | PatientRecordLink,
    allowed: list[UUID] | None,
) -> None:
    if allowed is None:
        return
    if link.facility_id is None or link.facility_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Not found.", "details": []},
        )


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, InvalidPhoneNumber):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "details": [{"field": "phone", "message": str(exc)}],
            },
        )
    if isinstance(
        exc,
        (InvitationNotPending, LinkNotPending, LinkAlreadyExists, ClinicalPatientInvalid),
    ):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(exc, PatientLinkError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    raise exc


def _invitation_payload(
    invitation: PatientLinkInvitation,
    account: PatientPortalAccount | None = None,
) -> dict[str, Any]:
    return {
        "uuid": str(invitation.id),
        "status": invitation.status,
        "purpose": invitation.purpose,
        "organization_uuid": str(invitation.organization_id),
        "facility_uuid": str(invitation.facility_id) if invitation.facility_id else None,
        "patient_uuid": str(invitation.patient_id),
        "phone_masked": mask_phone(invitation.phone),
        "account_uuid": str(account.id) if account else None,
        "expires_at": invitation.expires_at.isoformat(),
        "accepted_at": invitation.accepted_at.isoformat() if invitation.accepted_at else None,
        "revoked_at": invitation.revoked_at.isoformat() if invitation.revoked_at else None,
        "created_at": invitation.created_at.isoformat() if invitation.created_at else None,
    }


def _link_payload(
    link: PatientRecordLink,
    account: PatientPortalAccount | None = None,
) -> dict[str, Any]:
    return {
        "uuid": str(link.id),
        "status": link.status,
        "request_kind": link.request_kind,
        "verification_source": link.verification_source,
        "organization_uuid": str(link.organization_id),
        "facility_uuid": str(link.facility_id) if link.facility_id else None,
        "patient_uuid": str(link.patient_id) if link.patient_id else None,
        "patient_account_uuid": str(link.patient_account_id),
        "account_phone_masked": mask_phone(account.phone) if account else None,
        "account_name": (
            " ".join(
                part for part in [account.first_name, account.last_name] if part
            ).strip()
            or None
            if account
            else None
        ),
        "claimed_name": link.claimed_name,
        "claimed_date_of_birth": link.claimed_date_of_birth,
        "reviewed_at": link.reviewed_at.isoformat() if link.reviewed_at else None,
        "review_reason": link.review_reason,
        "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
        "revoke_reason": link.revoke_reason,
        "created_at": link.created_at.isoformat() if link.created_at else None,
    }


async def _accounts_for(
    db: AsyncSession, ids: set[UUID]
) -> dict[UUID, PatientPortalAccount]:
    if not ids:
        return {}
    rows = await db.scalars(
        select(PatientPortalAccount).where(PatientPortalAccount.id.in_(ids))
    )
    return {account.id: account for account in rows}


@router.get("/invitations")
async def list_invitations(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    status_filter: str | None = None,
    patient_uuid: UUID | None = None,
    facility_uuid: UUID | None = None,
) -> dict[str, Any]:
    _, organization, facility, allowed = await _link_context(db, account, facility_uuid)
    invitations = await patient_link_service.list_invitations(
        db,
        organization_id=organization.id,
        status=status_filter,
        patient_id=patient_uuid,
        facility_id=facility.id if facility else None,
        facility_ids=allowed if facility is None else None,
    )
    accounts = await _accounts_for(
        db, {inv.accepted_by_account_id for inv in invitations if inv.accepted_by_account_id}
    )
    items = [
        _invitation_payload(inv, accounts.get(inv.accepted_by_account_id))
        for inv in invitations
    ]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.post("/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    payload: StaffPatientInvitationCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, facility, allowed = await _link_context(
        db, account, payload.facility_uuid
    )
    if facility is None and allowed is not None:
        if len(allowed) == 1:
            facility = await db.get(Facility, allowed[0])
        else:
            raise _forbidden(
                "A facility_uuid is required for staff assigned to multiple facilities."
            )
    patient = await db.scalar(
        select(Patient).where(
            Patient.id == payload.patient_uuid,
            Patient.organization_id == organization.id,
        )
    )
    if patient is None or not patient.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Patient not found.", "details": []},
        )
    phone = payload.phone
    if phone is None:
        person = await db.get(Person, patient.person_id)
        phone = person.phone if person else None
    if not phone:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "details": [{"field": "phone", "message": "A phone number is required for the invitation."}],
            },
        )
    facility_id = facility.id if facility else None
    try:
        invitation, token = await patient_link_service.create_invitation(
            db,
            organization=organization,
            patient=patient,
            facility_id=facility_id,
            phone=phone,
            created_by_user_id=staff.user_account_id,
            ttl_hours=payload.expires_in_hours,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    data = _invitation_payload(invitation)
    # The activation code is returned exactly once and is never stored in clear.
    data["activation_code"] = token
    return {"success": True, "data": data, "meta": {}}


@router.post("/invitations/{invitation_uuid}/revoke")
async def revoke_invitation(
    invitation_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, _, allowed = await _link_context(db, account)
    invitation = await db.scalar(
        select(PatientLinkInvitation).where(
            PatientLinkInvitation.id == invitation_uuid,
            PatientLinkInvitation.organization_id == organization.id,
        )
    )
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Invitation not found.", "details": []},
        )
    _ensure_link_scoped(invitation, allowed)
    try:
        invitation = await patient_link_service.revoke_invitation(
            db, invitation=invitation, actor_user_id=staff.user_account_id
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return {"success": True, "data": _invitation_payload(invitation), "meta": {}}


@router.get("/requests")
async def list_requests(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    status_filter: str = "pending",
    facility_uuid: UUID | None = None,
    patient_uuid: UUID | None = None,
) -> dict[str, Any]:
    _, organization, facility, allowed = await _link_context(db, account, facility_uuid)
    links = await patient_link_service.list_links_for_organization(
        db,
        organization_id=organization.id,
        status=status_filter or None,
        patient_id=patient_uuid,
        facility_id=facility.id if facility else None,
        facility_ids=allowed if facility is None else None,
    )
    accounts = await _accounts_for(db, {link.patient_account_id for link in links})
    items = [_link_payload(link, accounts.get(link.patient_account_id)) for link in links]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.post("/requests/{link_uuid}/approve")
async def approve_request(
    link_uuid: UUID,
    payload: StaffPatientLinkApprove,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, facility, allowed = await _link_context(
        db, account, payload.facility_uuid
    )
    link = await db.scalar(
        select(PatientRecordLink).where(
            PatientRecordLink.id == link_uuid,
            PatientRecordLink.organization_id == organization.id,
        )
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Link request not found.", "details": []},
        )
    _ensure_link_scoped(link, allowed)
    patient = await db.scalar(
        select(Patient).where(
            Patient.id == payload.patient_uuid,
            Patient.organization_id == organization.id,
        )
    )
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Clinical patient not found.", "details": []},
        )
    try:
        link = await patient_link_service.approve_request(
            db,
            link=link,
            patient=patient,
            actor_user_id=staff.user_account_id,
            facility_id=facility.id if facility else payload.facility_uuid,
            review_reason=payload.review_reason,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    account_row = await db.get(PatientPortalAccount, link.patient_account_id)
    return {"success": True, "data": _link_payload(link, account_row), "meta": {}}


@router.post("/requests/{link_uuid}/reject")
async def reject_request(
    link_uuid: UUID,
    payload: StaffPatientLinkReject,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, _, allowed = await _link_context(db, account)
    link = await db.scalar(
        select(PatientRecordLink).where(
            PatientRecordLink.id == link_uuid,
            PatientRecordLink.organization_id == organization.id,
        )
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Link request not found.", "details": []},
        )
    _ensure_link_scoped(link, allowed)
    try:
        link = await patient_link_service.reject_request(
            db, link=link, actor_user_id=staff.user_account_id, reason=payload.reason
        )
    except Exception as exc:
        raise _translate(exc) from exc
    account_row = await db.get(PatientPortalAccount, link.patient_account_id)
    return {"success": True, "data": _link_payload(link, account_row), "meta": {}}


@router.post("/{link_uuid}/revoke")
async def revoke_link(
    link_uuid: UUID,
    payload: StaffPatientLinkRevoke,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization, _, allowed = await _link_context(db, account)
    link = await db.scalar(
        select(PatientRecordLink).where(
            PatientRecordLink.id == link_uuid,
            PatientRecordLink.organization_id == organization.id,
        )
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "Link not found.", "details": []},
        )
    _ensure_link_scoped(link, allowed)
    try:
        link = await patient_link_service.revoke_link(
            db, link=link, actor_user_id=staff.user_account_id, reason=payload.reason
        )
    except Exception as exc:
        raise _translate(exc) from exc
    account_row = await db.get(PatientPortalAccount, link.patient_account_id)
    return {"success": True, "data": _link_payload(link, account_row), "meta": {}}


@router.get("")
async def list_links(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    status_filter: str | None = None,
    patient_uuid: UUID | None = None,
    facility_uuid: UUID | None = None,
) -> dict[str, Any]:
    _, organization, facility, allowed = await _link_context(db, account, facility_uuid)
    links = await patient_link_service.list_links_for_organization(
        db,
        organization_id=organization.id,
        status=status_filter,
        patient_id=patient_uuid,
        facility_id=facility.id if facility else None,
        facility_ids=allowed if facility is None else None,
    )
    accounts = await _accounts_for(db, {link.patient_account_id for link in links})
    items = [_link_payload(link, accounts.get(link.patient_account_id)) for link in links]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}
