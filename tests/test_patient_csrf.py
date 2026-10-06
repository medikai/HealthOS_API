"""CSRF and cookie-coexistence checks for the patient namespace.

The patient session service and the staff session lookup are mocked so these
checks exercise middleware behavior only; no database is required.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.core.config import settings
from src.app.middleware.csrf_middleware import CSRFMiddleware

PATIENT_COOKIE = settings.PATIENT_SESSION_COOKIE_NAME
STAFF_COOKIE = settings.AUTH_SESSION_COOKIE_NAME
CSRF_HEADER = settings.PATIENT_CSRF_HEADER_NAME


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/api/v1/patient/write")
    async def patient_write() -> dict:
        return {"success": True}

    @app.post("/api/v1/patient/auth/otp/request")
    async def otp_request() -> dict:
        return {"success": True}

    @app.post("/api/v1/me")
    async def staff_write() -> dict:
        return {"success": True}

    return app


@pytest.fixture
def csrf_client(monkeypatch):
    patient_session = SimpleNamespace(csrf_token="patient-csrf")
    staff_session = SimpleNamespace(csrf_token="staff-csrf")
    monkeypatch.setattr(
        "src.app.middleware.csrf_middleware._patient_session_service.resolve",
        AsyncMock(return_value=patient_session),
    )
    monkeypatch.setattr(
        "src.app.crud.crud_auth_session.crud_auth_sessions.get_session",
        AsyncMock(return_value=staff_session),
    )
    with TestClient(_app()) as client:
        yield client


def test_patient_cookie_write_requires_csrf(csrf_client):
    response = csrf_client.post(
        "/api/v1/patient/write", cookies={PATIENT_COOKIE: "opaque"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"


def test_patient_cookie_write_accepts_matching_csrf(csrf_client):
    response = csrf_client.post(
        "/api/v1/patient/write",
        cookies={PATIENT_COOKIE: "opaque"},
        headers={CSRF_HEADER: "patient-csrf"},
    )
    assert response.status_code == 200


def test_bogus_bearer_cannot_bypass_patient_csrf(csrf_client):
    response = csrf_client.post(
        "/api/v1/patient/write",
        cookies={PATIENT_COOKIE: "opaque"},
        headers={"Authorization": "Bearer arbitrary-not-a-real-token"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"


def test_bogus_bearer_without_patient_cookie_grants_nothing(csrf_client):
    response = csrf_client.post(
        "/api/v1/patient/write",
        headers={"Authorization": "Bearer arbitrary-not-a-real-token"},
    )
    # Middleware does not authenticate the bearer; the route dependency would
    # reject it with 401 in the real application.
    assert response.status_code == 200


def test_otp_bootstrap_endpoints_are_csrf_exempt(csrf_client):
    response = csrf_client.post(
        "/api/v1/patient/auth/otp/request", cookies={PATIENT_COOKIE: "stale"}
    )
    assert response.status_code == 200


def test_patient_cookie_is_ignored_on_staff_paths(csrf_client):
    # Staff routes resolve the staff cookie only; the patient CSRF is irrelevant.
    response = csrf_client.post(
        "/api/v1/me",
        cookies={PATIENT_COOKIE: "opaque"},
        headers={CSRF_HEADER: "patient-csrf"},
    )
    assert response.status_code == 200


def test_staff_cookie_write_requires_staff_csrf(csrf_client):
    response = csrf_client.post("/api/v1/me", cookies={STAFF_COOKIE: "opaque"})
    assert response.status_code == 403
    response = csrf_client.post(
        "/api/v1/me",
        cookies={STAFF_COOKIE: "opaque"},
        headers={CSRF_HEADER: "staff-csrf"},
    )
    assert response.status_code == 200


def test_both_cookies_present_use_the_namespace_session(csrf_client):
    both = {PATIENT_COOKIE: "patient-opaque", STAFF_COOKIE: "staff-opaque"}
    patient_ok = csrf_client.post(
        "/api/v1/patient/write",
        cookies=both,
        headers={CSRF_HEADER: "patient-csrf"},
    )
    assert patient_ok.status_code == 200
    staff_ok = csrf_client.post(
        "/api/v1/me", cookies=both, headers={CSRF_HEADER: "staff-csrf"}
    )
    assert staff_ok.status_code == 200
