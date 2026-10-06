"""Patient portal API: phone OTP auth, session, profile, clinic links.

Every route under ``/api/v1/patient`` resolves only the patient session cookie.
Staff cookies/JWTs are ignored, and no clinical record is exposed until an
audited, staff-authorized link exists.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.patient_dependencies import PatientPrincipal, get_current_patient
from ...core.config import settings
from ...core.db.database import async_get_db
from ...domains.communication.realtime.repository import (
    PatientRealtimeChannelRepository,
)
from ...domains.patient_auth.links import (
    ClinicalPatientInvalid,
    ClinicNotAvailable,
    InvitationExpired,
    InvitationInvalid,
    InvitationNotPending,
    LinkAlreadyExists,
    LinkAlreadyVerified,
    LinkNotFound,
    LinkNotPending,
    PatientLinkError,
    PatientLinkService,
)
from ...domains.patient_auth.otp import (
    ExpiredOtpCode,
    InvalidOtpCode,
    OtpPolicy,
    OtpProviderUnavailable,
    OtpRateLimited,
    OtpSandboxUnavailable,
    PatientOtpError,
    PatientOtpService,
    build_patient_otp_provider,
)
from ...domains.patient_auth.phone import InvalidPhoneNumber
from ...domains.patient_auth.service import (
    PatientAuthError,
    PatientAuthService,
    PhoneInUse,
    PhoneUnchanged,
)
from ...domains.patient_auth.sessions import PatientSessionService
from ...models.identity import Patient, PatientRecordLink
from ...models.organization import Facility, Organization
from ...schemas.patient_portal import (
    PatientLinkActivationBody,
    PatientLinkRequestBody,
    PatientOtpRequestBody,
    PatientOtpResendBody,
    PatientOtpVerifyBody,
    PatientPhoneChangeBody,
    PatientPhoneChangeVerifyBody,
    PatientProfileUpdate,
)

router = APIRouter(prefix="/patient", tags=["patient"])

patient_otp_service = PatientOtpService(
    OtpPolicy.from_settings(settings),
    provider=build_patient_otp_provider(settings),
)
patient_session_service = PatientSessionService()
patient_realtime_repository = PatientRealtimeChannelRepository()
patient_auth_service = PatientAuthService(
    otp_service=patient_otp_service,
    session_service=patient_session_service,
    session_ttl_seconds=settings.PATIENT_SESSION_TTL_SECONDS,
)


def _portal_pepper() -> str:
    if settings.PATIENT_OTP_PEPPER is not None:
        return settings.PATIENT_OTP_PEPPER.get_secret_value()
    return settings.SECRET_KEY.get_secret_value()


patient_link_service = PatientLinkService(
    pepper=_portal_pepper(),
    invitation_ttl_hours=settings.PATIENT_LINK_INVITATION_TTL_HOURS,
    invitation_max_ttl_hours=settings.PATIENT_LINK_INVITATION_MAX_TTL_HOURS,
)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _otp_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Move the internal sandbox marker into the response envelope meta."""
    sandbox = bool(data.pop("sandbox", False))
    return {
        "success": True,
        "data": data,
        "meta": {"sandbox": True} if sandbox else {},
    }


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, OtpRateLimited):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "OTP_RATE_LIMITED",
                "message": "Too many code requests. Try again later.",
                "details": [{"retry_after_seconds": exc.retry_after_seconds}],
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    if isinstance(exc, OtpProviderUnavailable):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "OTP_PROVIDER_UNAVAILABLE",
                "message": "Phone sign-in is temporarily unavailable.",
                "details": [{"reason": exc.reason}],
            },
        )
    if isinstance(exc, OtpSandboxUnavailable):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "OTP_SANDBOX_UNAVAILABLE",
                "message": "Sandbox OTP is available only for allowlisted numbers in local development.",
                "details": [{"reason": exc.reason}],
            },
        )
    if isinstance(exc, ExpiredOtpCode):
        return HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "OTP_EXPIRED", "message": "The code is invalid or has expired."},
        )
    if isinstance(exc, InvalidOtpCode):
        details = []
        if exc.attempts_remaining is not None:
            details.append({"attempts_remaining": exc.attempts_remaining})
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "OTP_INVALID",
                "message": "The code is invalid or has expired.",
                "details": details,
            },
        )
    if isinstance(exc, PhoneInUse):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "PHONE_IN_USE", "message": str(exc), "details": []},
        )
    if isinstance(exc, PhoneUnchanged):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "PHONE_UNCHANGED", "message": str(exc), "details": []},
        )
    if isinstance(exc, InvitationExpired):
        return HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": "LINK_CODE_EXPIRED", "message": "The link code has expired."},
        )
    if isinstance(exc, InvitationInvalid):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "LINK_CODE_INVALID", "message": "The link code is invalid."},
        )
    if isinstance(exc, ClinicNotAvailable):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CLINIC_NOT_AVAILABLE", "message": "This clinic is not available in the patient portal."},
        )
    if isinstance(exc, (LinkAlreadyVerified, LinkAlreadyExists)):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(exc, (InvitationNotPending, LinkNotPending, LinkNotFound, ClinicalPatientInvalid)):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    if isinstance(exc, InvalidPhoneNumber):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "details": [{"field": "phone", "message": str(exc)}],
            },
        )
    if isinstance(exc, (PatientOtpError, PatientAuthError, PatientLinkError)):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": str(exc), "details": []},
        )
    raise exc


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.PATIENT_SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.PATIENT_SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.PATIENT_COOKIE_SECURE,
        samesite=settings.PATIENT_COOKIE_SAMESITE,
        path=settings.PATIENT_COOKIE_PATH,
    )


def _delete_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.PATIENT_SESSION_COOKIE_NAME,
        path=settings.PATIENT_COOKIE_PATH,
        secure=settings.PATIENT_COOKIE_SECURE,
        samesite=settings.PATIENT_COOKIE_SAMESITE,
    )


def _session_payload(token: str | None, session) -> dict[str, Any]:
    return {
        "expires_at": session.expires_at.isoformat(),
        "csrf_token": session.csrf_token,
        "cookie_name": settings.PATIENT_SESSION_COOKIE_NAME,
        "cookie_path": settings.PATIENT_COOKIE_PATH,
        "rotated": token is not None,
    }


def _link_payload(
    link: PatientRecordLink,
    organization: Organization | None = None,
    patient: Patient | None = None,
) -> dict[str, Any]:
    verified = link.status == "verified"
    return {
        "uuid": str(link.id),
        "status": link.status,
        "request_kind": link.request_kind,
        "verification_source": link.verification_source,
        "organization_uuid": str(link.organization_id),
        "organization_name": organization.name if organization else None,
        "facility_uuid": str(link.facility_id) if link.facility_id else None,
        "patient_uuid": str(link.patient_id) if verified and link.patient_id else None,
        "mrn": patient.mrn if verified and patient is not None else None,
        "verified_at": link.reviewed_at.isoformat() if link.reviewed_at else None,
        "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
        "created_at": link.created_at.isoformat() if link.created_at else None,
    }


def _facility_payload(facility: Facility) -> dict[str, Any]:
    return {
        "uuid": str(facility.id),
        "name": facility.name,
        "code": facility.code,
        "street_address": facility.street_address,
        "phone": facility.phone,
    }


@router.post("/auth/otp/request")
async def request_otp(
    payload: PatientOtpRequestBody,
    request: Request,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    try:
        data = await patient_auth_service.request_login_otp(
            db,
            phone=payload.phone,
            client_ip=_client_ip(request),
            sandbox=payload.sandbox,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return _otp_envelope(data)


@router.post("/auth/otp/resend")
async def resend_otp(
    payload: PatientOtpResendBody,
    request: Request,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    try:
        data = await patient_auth_service.resend_login_otp(
            db,
            challenge_id=payload.challenge_id,
            client_ip=_client_ip(request),
            phone=payload.phone,
            sandbox=payload.sandbox,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return _otp_envelope(data)


@router.post("/auth/otp/verify")
async def verify_otp(
    payload: PatientOtpVerifyBody,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    rotate_token = request.cookies.get(settings.PATIENT_SESSION_COOKIE_NAME)
    try:
        account, token = await patient_auth_service.verify_login(
            db,
            phone=payload.phone,
            challenge_id=payload.challenge_id,
            code=payload.code,
            rotate_token=rotate_token,
            user_agent=request.headers.get("User-Agent"),
            client_ip=_client_ip(request),
        )
    except Exception as exc:
        raise _translate(exc) from exc
    session = await patient_session_service.resolve(db, token)
    assert session is not None
    _set_session_cookie(response, token)
    link_status = await patient_link_service.link_status(db, account_id=account.id)
    return {
        "success": True,
        "data": {
            "patient": patient_auth_service.profile_payload(account),
            "link_status": link_status,
            "session": _session_payload(token, session),
        },
        "meta": {},
    }


@router.get("/auth/session")
async def current_session(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    link_status = await patient_link_service.link_status(
        db, account_id=principal.account.id
    )
    return {
        "success": True,
        "data": {
            "patient": patient_auth_service.profile_payload(principal.account),
            "link_status": link_status,
            "session": _session_payload(principal.token, principal.session),
        },
        "meta": {},
    }


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    await patient_session_service.revoke(db, principal.token, reason="logout")
    # Retire every previously issued realtime channel/token for this account.
    await patient_realtime_repository.bump_patient_generation(
        db, patient_account_id=principal.account.id
    )
    await db.commit()
    _delete_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me")
async def me(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    link_status = await patient_link_service.link_status(
        db, account_id=principal.account.id
    )
    return {
        "success": True,
        "data": {
            "patient": patient_auth_service.profile_payload(principal.account),
            "link_status": link_status,
        },
        "meta": {},
    }


@router.patch("/profile")
async def update_profile(
    payload: PatientProfileUpdate,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    account = principal.account
    fields = payload.model_fields_set
    if "first_name" in fields:
        account.first_name = payload.first_name
    if "last_name" in fields:
        account.last_name = payload.last_name
    if "date_of_birth" in fields:
        account.date_of_birth = payload.date_of_birth
    if "email" in fields:
        account.email = payload.email
    if fields:
        from datetime import UTC, datetime

        account.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(account)
    return {
        "success": True,
        "data": {"patient": patient_auth_service.profile_payload(account)},
        "meta": {},
    }


@router.post("/auth/phone/change/request")
async def request_phone_change(
    payload: PatientPhoneChangeBody,
    request: Request,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    try:
        data = await patient_auth_service.request_phone_change(
            db,
            account=principal.account,
            new_phone=payload.new_phone,
            client_ip=_client_ip(request),
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return {"success": True, "data": data, "meta": {}}


@router.post("/auth/phone/change/verify")
async def verify_phone_change(
    payload: PatientPhoneChangeVerifyBody,
    request: Request,
    response: Response,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    try:
        account, token = await patient_auth_service.verify_phone_change(
            db,
            account=principal.account,
            new_phone=payload.new_phone,
            challenge_id=payload.challenge_id,
            code=payload.code,
            current_token=principal.token,
            user_agent=request.headers.get("User-Agent"),
            client_ip=_client_ip(request),
        )
    except Exception as exc:
        raise _translate(exc) from exc
    session = await patient_session_service.resolve(db, token)
    assert session is not None
    _set_session_cookie(response, token)
    link_status = await patient_link_service.link_status(db, account_id=account.id)
    return {
        "success": True,
        "data": {
            "patient": patient_auth_service.profile_payload(account),
            "link_status": link_status,
            "session": _session_payload(token, session),
        },
        "meta": {},
    }


@router.get("/linked-clinics")
async def linked_clinics(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    links = await patient_link_service.list_links_for_account(
        db, account_id=principal.account.id
    )
    organizations: dict[UUID, Organization] = {}
    if links:
        rows = await db.scalars(
            select(Organization).where(
                Organization.id.in_({link.organization_id for link in links})
            )
        )
        organizations = {org.id: org for org in rows}
    patient_ids = {link.patient_id for link in links if link.patient_id}
    patients: dict[UUID, Patient] = {}
    if patient_ids:
        rows = await db.scalars(select(Patient).where(Patient.id.in_(patient_ids)))
        patients = {patient.id: patient for patient in rows}
    items = [
        _link_payload(
            link,
            organizations.get(link.organization_id),
            patients.get(link.patient_id) if link.patient_id else None,
        )
        for link in links
    ]
    status_value = await patient_link_service.link_status(
        db, account_id=principal.account.id
    )
    return {
        "success": True,
        "data": {"items": items, "link_status": status_value},
        "meta": {"count": len(items)},
    }


@router.post("/link/activation")
async def activate_link(
    payload: PatientLinkActivationBody,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    try:
        link = await patient_link_service.activate(
            db, account=principal.account, token=payload.code
        )
    except Exception as exc:
        raise _translate(exc) from exc
    organization = await db.get(Organization, link.organization_id)
    patient = await db.get(Patient, link.patient_id) if link.patient_id else None
    return {
        "success": True,
        "data": {
            "link": _link_payload(link, organization, patient),
            "link_status": await patient_link_service.link_status(
                db, account_id=principal.account.id
            ),
        },
        "meta": {},
    }


@router.post("/link/request")
async def request_link(
    payload: PatientLinkRequestBody,
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization = await db.get(Organization, payload.organization_uuid)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CLINIC_NOT_AVAILABLE", "message": "This clinic is not available in the patient portal."},
        )
    facility_id = payload.facility_uuid
    if facility_id is not None:
        facility = await db.get(Facility, facility_id)
        if (
            facility is None
            or facility.organization_id != organization.id
            or not facility.is_active
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": [{"field": "facility_uuid", "message": "Unknown facility for this clinic."}],
                },
            )
    claimed_name = None
    if payload.first_name:
        claimed_name = " ".join(
            part for part in [payload.first_name, payload.last_name] if part
        )
    try:
        link = await patient_link_service.request_link(
            db,
            account=principal.account,
            organization=organization,
            facility_id=facility_id,
            claimed_name=claimed_name,
            claimed_date_of_birth=payload.date_of_birth,
        )
    except Exception as exc:
        raise _translate(exc) from exc
    return {
        "success": True,
        "data": {
            "link": _link_payload(link, organization, None),
            "link_status": await patient_link_service.link_status(
                db, account_id=principal.account.id
            ),
        },
        "meta": {},
    }


@router.get("/clinics")
async def portal_clinics(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organizations = (
        await db.scalars(
            select(Organization)
            .where(
                Organization.is_active.is_(True),
                Organization.portal_enabled.is_(True),
            )
            .order_by(Organization.name)
        )
    ).all()
    items = []
    if organizations:
        facilities = (
            await db.scalars(
                select(Facility)
                .where(
                    Facility.organization_id.in_([org.id for org in organizations]),
                    Facility.is_active.is_(True),
                )
                .order_by(Facility.name)
            )
        ).all()
        by_org: dict[UUID, list[Facility]] = {}
        for facility in facilities:
            by_org.setdefault(facility.organization_id, []).append(facility)
        items = [
            {
                "uuid": str(org.id),
                "name": org.name,
                "code": org.code,
                "facilities": [
                    _facility_payload(facility)
                    for facility in by_org.get(org.id, [])
                ],
            }
            for org in organizations
        ]
    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items)},
    }
