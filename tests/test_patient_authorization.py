"""Patient authorization dependency: staff cookies/JWTs never satisfy it."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.app.api.patient_dependencies import PatientPrincipal, get_current_patient
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.models.identity import PatientPortalAccount

PATIENT_COOKIE = settings.PATIENT_SESSION_COOKIE_NAME


class FakeDb:
    def __init__(self, account):
        self.account = account

    async def get(self, model, primary_key):
        if model is PatientPortalAccount:
            return self.account
        return None


def _session(account):
    return SimpleNamespace(
        id=uuid4(),
        patient_account_id=account.id,
        token_hash="x" * 64,
        csrf_token="csrf",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _client(account):
    app = FastAPI()

    @app.get("/patient-test")
    async def whoami(
        principal: PatientPrincipal = Depends(get_current_patient),  # noqa: B008
    ) -> dict:
        return {"uuid": str(principal.account.id)}

    app.dependency_overrides[async_get_db] = lambda: FakeDb(account)
    return TestClient(app)


def test_missing_patient_cookie_is_rejected(monkeypatch):
    account = SimpleNamespace(id=uuid4(), is_active=True)
    monkeypatch.setattr(
        "src.app.api.patient_dependencies.patient_session_service.resolve",
        AsyncMock(return_value=_session(account)),
    )
    with _client(account) as client:
        response = client.get("/patient-test")
    assert response.status_code == 401


def test_staff_cookie_cannot_authenticate_patient_route(monkeypatch):
    account = SimpleNamespace(id=uuid4(), is_active=True)
    monkeypatch.setattr(
        "src.app.api.patient_dependencies.patient_session_service.resolve",
        AsyncMock(return_value=_session(account)),
    )
    with _client(account) as client:
        response = client.get(
            "/patient-test", cookies={settings.AUTH_SESSION_COOKIE_NAME: "staff"}
        )
    assert response.status_code == 401


def test_invalid_patient_session_is_rejected(monkeypatch):
    account = SimpleNamespace(id=uuid4(), is_active=True)
    monkeypatch.setattr(
        "src.app.api.patient_dependencies.patient_session_service.resolve",
        AsyncMock(return_value=None),
    )
    with _client(account) as client:
        response = client.get("/patient-test", cookies={PATIENT_COOKIE: "nope"})
    assert response.status_code == 401


def test_inactive_account_is_rejected(monkeypatch):
    account = SimpleNamespace(id=uuid4(), is_active=False)
    monkeypatch.setattr(
        "src.app.api.patient_dependencies.patient_session_service.resolve",
        AsyncMock(return_value=_session(account)),
    )
    with _client(account) as client:
        response = client.get("/patient-test", cookies={PATIENT_COOKIE: "opaque"})
    assert response.status_code == 401


def test_valid_patient_cookie_resolves(monkeypatch):
    account = SimpleNamespace(id=uuid4(), is_active=True)
    monkeypatch.setattr(
        "src.app.api.patient_dependencies.patient_session_service.resolve",
        AsyncMock(return_value=_session(account)),
    )
    with _client(account) as client:
        response = client.get("/patient-test", cookies={PATIENT_COOKIE: "opaque"})
    assert response.status_code == 200
    assert response.json()["uuid"] == str(account.id)


def test_arbitrary_bearer_is_never_consulted(monkeypatch):
    account = SimpleNamespace(id=uuid4(), is_active=True)
    monkeypatch.setattr(
        "src.app.api.patient_dependencies.patient_session_service.resolve",
        AsyncMock(return_value=_session(account)),
    )
    with _client(account) as client:
        response = client.get(
            "/patient-test",
            cookies={PATIENT_COOKIE: "opaque"},
            headers={"Authorization": "Bearer totally-bogus"},
        )
    assert response.status_code == 200
