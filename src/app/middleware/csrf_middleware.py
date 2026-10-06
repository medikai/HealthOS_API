import hmac
from typing import ClassVar

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.config import settings
from ..core.db.database import local_session
from ..crud.crud_auth_session import crud_auth_sessions
from ..domains.patient_auth.sessions import PatientSessionService

PATIENT_NAMESPACE = "/api/v1/patient"
# Public OTP bootstrap endpoints do not act on an existing cookie principal and
# must remain reachable even when a stale patient cookie is present.
PATIENT_CSRF_EXEMPT_PATHS = {
    f"{PATIENT_NAMESPACE}/auth/otp/request",
    f"{PATIENT_NAMESPACE}/auth/otp/resend",
    f"{PATIENT_NAMESPACE}/auth/otp/verify",
}
_patient_session_service = PatientSessionService()


class CSRFMiddleware(BaseHTTPMiddleware):
    """Require a server-issued CSRF token for cookie-authenticated writes.

    Staff cookie auth keeps its existing behavior (Bearer bypass). Patient
    writes are validated against the patient session cookie, and an arbitrary
    ``Authorization: Bearer`` header can never bypass CSRF for the patient
    namespace.
    """

    _safe_methods: ClassVar[set[str]] = {"GET", "HEAD", "OPTIONS", "TRACE"}

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in self._safe_methods:
            return await call_next(request)

        path = request.url.path
        if path == PATIENT_NAMESPACE or path.startswith(PATIENT_NAMESPACE + "/"):
            if path in PATIENT_CSRF_EXEMPT_PATHS:
                return await call_next(request)
            patient_token = request.cookies.get(settings.PATIENT_SESSION_COOKIE_NAME)
            if not patient_token:
                return await call_next(request)
            async with local_session() as db:
                patient_session = await _patient_session_service.resolve(
                    db, patient_token
                )
            provided_token = request.headers.get(settings.PATIENT_CSRF_HEADER_NAME)
            expected_token = patient_session.csrf_token if patient_session else None
            if (
                not isinstance(expected_token, str)
                or not isinstance(provided_token, str)
                or not hmac.compare_digest(expected_token, provided_token)
            ):
                return _csrf_failure()
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return await call_next(request)

        session_id = request.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
        if not session_id:
            return await call_next(request)

        async with local_session() as db:
            session = await crud_auth_sessions.get_session(db, session_id)
        provided_token = request.headers.get(settings.AUTH_CSRF_HEADER_NAME)
        expected_token = session.csrf_token if session else None
        if not isinstance(expected_token, str) or not isinstance(provided_token, str) or not hmac.compare_digest(
            expected_token, provided_token
        ):
            return _csrf_failure()
        return await call_next(request)


def _csrf_failure() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "success": False,
            "error": {"code": "CSRF_VALIDATION_FAILED", "message": "CSRF validation failed.", "details": []},
            "meta": {},
        },
    )
