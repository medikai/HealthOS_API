"""Patient realtime authorization tests with a fake token provider.

Covers: account-level channel/client-id derivation, subscribe-only
capabilities, no client-supplied identity/channel override, CSRF on the
patient token POST, missing key fails closed, generation-based revocation and
opaque patient delivery payloads. No network call and no database mutation.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException

from src.app.api.patient_dependencies import PatientPrincipal, get_current_patient
from src.app.api.v1.patient_realtime import get_patient_realtime_service
from src.app.core.config import settings
from src.app.domains.communication.realtime.patient_service import (
    PatientRealtimeService,
)
from src.app.domains.communication.realtime.providers.notify import (
    REALTIME_EVENT_PATIENT_NOTIFICATION,
    RealtimeNotificationProvider,
)
from src.app.domains.communication.shared.errors import ProviderUnavailable
from src.app.domains.communication.utils.channels import (
    build_patient_capabilities,
    build_patient_client_id,
    patient_user_channel,
)
from src.app.main import app

NAMESPACE = "dev"
PATIENT_COOKIE = settings.PATIENT_SESSION_COOKIE_NAME
CSRF_HEADER = settings.PATIENT_CSRF_HEADER_NAME


def run(coro):
    return asyncio.run(coro)


class RecordingTokenProvider:
    def __init__(self, *, configured: bool = True) -> None:
        self.configured = configured
        self.calls: list[dict] = []

    async def create_token_request(self, *, client_id, capabilities, ttl_seconds) -> dict:
        self.calls.append(
            {"client_id": client_id, "capabilities": capabilities, "ttl_seconds": ttl_seconds}
        )
        return {
            "keyName": "test.key",
            "clientId": client_id,
            "ttl": ttl_seconds * 1000,
            "nonce": "synthetic",
            "capability": json.dumps(capabilities),
            "timestamp": 0,
            "mac": "synthetic-mac",
        }


class InMemoryPatientChannelRepository:
    def __init__(self) -> None:
        self.states: dict = {}

    async def get_or_create_patient(self, db, *, patient_account_id):
        return self.states.setdefault(patient_account_id, SimpleNamespace(generation=1))

    async def bump_patient_generation(self, db, *, patient_account_id):
        state = await self.get_or_create_patient(db, patient_account_id=patient_account_id)
        state.generation += 1
        return state.generation


def _service(provider, repository=None):
    return PatientRealtimeService(
        provider=provider,
        repository=repository or InMemoryPatientChannelRepository(),
        namespace=NAMESPACE,
        ttl_seconds=1800,
        renew_after_seconds=900,
    )


def _principal(account_id=None):
    account = SimpleNamespace(id=account_id or uuid4(), is_active=True)
    return PatientPrincipal(
        account=account, session=SimpleNamespace(csrf_token="patient-csrf"), token="opaque"
    )


def test_patient_channel_and_client_id_are_account_level():
    account_id = uuid4()
    channel = patient_user_channel(NAMESPACE, account_id, 4)
    assert channel == f"{NAMESPACE}:p:{account_id}:g:4"
    assert build_patient_client_id(account_id) == f"patient:{account_id}"
    caps = build_patient_capabilities(channel)
    assert caps == {channel: ["subscribe"]}


def test_patient_token_scope_derives_from_account_only():
    provider = RecordingTokenProvider()
    service = _service(provider)
    account_id = uuid4()
    foreign_account = uuid4()

    result = run(service.issue_token(None, SimpleNamespace(id=account_id)))

    assert result["channel"] == patient_user_channel(NAMESPACE, account_id, 1)
    assert result["client_id"] == build_patient_client_id(account_id)
    assert result["capabilities"] == {result["channel"]: ["subscribe"]}
    assert all("publish" not in ops for ops in result["capabilities"].values())
    assert str(foreign_account) not in json.dumps(result)
    assert provider.calls[0]["client_id"] == build_patient_client_id(account_id)


def test_patient_missing_key_fails_closed_without_token():
    service = _service(RecordingTokenProvider(configured=False))
    with_raises = False
    try:
        run(service.issue_token(None, SimpleNamespace(id=uuid4())))
    except ProviderUnavailable:
        with_raises = True
    assert with_raises is True
    config = service.feature_config()
    assert config["available"] is False
    assert config["reason"] == "ably_not_configured"
    assert config["browser_capabilities"] == ["subscribe"]
    assert config["browser_publish"] is False


def test_patient_generation_bump_retires_old_channel():
    provider = RecordingTokenProvider()
    repository = InMemoryPatientChannelRepository()
    service = _service(provider, repository)
    account = SimpleNamespace(id=uuid4())

    first = run(service.issue_token(None, account))
    run(repository.bump_patient_generation(None, patient_account_id=account.id))
    second = run(service.issue_token(None, account))

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert first["channel"] != second["channel"]


def test_patient_http_config_and_token_scope(client):
    account_id = uuid4()
    service = _service(RecordingTokenProvider())
    app.dependency_overrides[get_current_patient] = lambda: _principal(account_id)
    app.dependency_overrides[get_patient_realtime_service] = lambda: service
    try:
        # A malicious client body must be ignored: no channel/account override.
        response = client.post(
            "/api/v1/patient/realtime/token",
            json={"patient_account_id": str(uuid4()), "channel": "dev:*", "client_id": "attacker"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["channel"] == patient_user_channel(NAMESPACE, account_id, 1)
        assert "attacker" not in response.text
        assert service.provider.calls[0]["client_id"] == build_patient_client_id(account_id)

        config = client.get("/api/v1/patient/realtime/config")
        assert config.status_code == 200
        body = config.json()["data"]
        assert body["available"] is True
        assert body["browser_publish"] is False
        assert "token_request" not in body
        assert "keyName" not in config.text
        assert "mac" not in config.text

        app.dependency_overrides[get_patient_realtime_service] = lambda: _service(
            RecordingTokenProvider(configured=False)
        )
        unavailable = client.post("/api/v1/patient/realtime/token", json={})
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
        assert "token_request" not in unavailable.text
    finally:
        app.dependency_overrides.clear()


def test_patient_http_requires_authentication(client):
    def _unauthorized():
        raise HTTPException(status_code=401, detail="Authentication required.")

    app.dependency_overrides[get_current_patient] = _unauthorized
    app.dependency_overrides[get_patient_realtime_service] = lambda: _service(
        RecordingTokenProvider()
    )
    try:
        response = client.post("/api/v1/patient/realtime/token", json={})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_patient_token_post_requires_csrf_when_cookie_present(client, monkeypatch):
    monkeypatch.setattr(
        "src.app.middleware.csrf_middleware._patient_session_service.resolve",
        AsyncMock(return_value=SimpleNamespace(csrf_token="patient-csrf")),
    )
    app.dependency_overrides[get_current_patient] = lambda: _principal()
    app.dependency_overrides[get_patient_realtime_service] = lambda: _service(
        RecordingTokenProvider()
    )
    try:
        denied = client.post(
            "/api/v1/patient/realtime/token", json={}, cookies={PATIENT_COOKIE: "opaque"}
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"

        allowed = client.post(
            "/api/v1/patient/realtime/token",
            json={},
            cookies={PATIENT_COOKIE: "opaque"},
            headers={CSRF_HEADER: "patient-csrf"},
        )
        assert allowed.status_code == 200
    finally:
        app.dependency_overrides.clear()


class _RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def configured(self) -> bool:
        return True

    async def publish(self, *, channel, event_type, payload):
        self.calls.append(
            {"channel": channel, "event_type": event_type, "payload": payload}
        )
        return {"provider": "ably"}


def test_patient_delivery_routes_to_patient_branch(monkeypatch):
    publisher = _RecordingPublisher()
    provider = RealtimeNotificationProvider(publisher=publisher)

    async def _fake_patient(payload):
        return {"provider": "ably", "published": True, "patient": True}

    monkeypatch.setattr(provider, "_deliver_patient_notification", _fake_patient)
    job = SimpleNamespace(
        payload={"notification_id": str(uuid4()), "patient_account_id": str(uuid4())},
        recipient_patient_id=uuid4(),
    )
    result = run(provider.deliver(job))
    assert result.get("patient") is True


def test_patient_envelope_is_opaque_and_has_no_key_material():
    from src.app.domains.communication.shared.envelope import build_event_envelope

    patient_id = uuid4()
    envelope = build_event_envelope(
        REALTIME_EVENT_PATIENT_NOTIFICATION,
        entity_id=uuid4(),
        entity_version=None,
        organization_id=uuid4(),
        facility_id=None,
        recipient_patient_id=patient_id,
        data={
            "notification_id": str(uuid4()),
            "kind": "appointment_confirmed",
            "organization_uuid": str(uuid4()),
        },
    )
    assert envelope["event_type"] == "patient_notification"
    assert envelope["recipient_patient_id"] == str(patient_id)
    assert set(envelope["data"]) == {"notification_id", "kind", "organization_uuid"}
    serialized = json.dumps(envelope).lower()
    for forbidden in ("apikey", "api_key", "body", "title", "deep_link", "mac"):
        assert forbidden not in serialized
