import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.auth_session import auth_session_store
from ..core.config import settings


class CSRFMiddleware(BaseHTTPMiddleware):
    """Require a server-issued CSRF token for cookie-authenticated writes."""

    _safe_methods = {"GET", "HEAD", "OPTIONS", "TRACE"}

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in self._safe_methods:
            return await call_next(request)

        session_id = request.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
        if not session_id:
            return await call_next(request)

        session = await auth_session_store.get_session(session_id)
        provided_token = request.headers.get(settings.AUTH_CSRF_HEADER_NAME)
        expected_token = session.get("csrf_token") if session else None
        if not isinstance(expected_token, str) or not isinstance(provided_token, str) or not hmac.compare_digest(
            expected_token, provided_token
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": {"code": "CSRF_VALIDATION_FAILED", "message": "CSRF validation failed.", "details": []},
                    "meta": {},
                },
            )
        return await call_next(request)
