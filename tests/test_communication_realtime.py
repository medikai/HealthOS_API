"""Realtime authorization tests with a fake token provider.

Covers: channel/capability scope, no client channel/sender override, missing
key fails closed, generation-based revocation, and secret redaction. No
network call and no database mutation.
"""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.app.api.dependencies import get_current_identity_account
from src.app.domains.communication.dependencies import (
    CommunicationStaffContext,
    get_communication_staff_context,
    get_realtime_service,
    resolve_staff_context,
)
from src.app.domains.communication.realtime.providers.ably import AblyRealtimeProvider
from src.app.domains.communication.realtime.service import RealtimeService
from src.app.domains.communication.shared.errors import ProviderUnavailable
from src.app.domains.communication.utils.channels import (
    build_capabilities,
    build_client_id,
    facility_presence_channel,
    own_user_channel,
    validate_namespace,
)
from src.app.main import app

NAMESPACE = "dev"


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


class InMemoryChannelRepository:
    def __init__(self) -> None:
        self.states: dict[tuple, SimpleNamespace] = {}

    async def get_or_create(self, db, *, organization_id, staff_member_id):
        return self.states.setdefault(
            (organization_id, staff_member_id), SimpleNamespace(generation=1)
        )

    async def bump_generation(self, db, *, organization_id, staff_member_id):
        state = await self.get_or_create(
            db, organization_id=organization_id, staff_member_id=staff_member_id
        )
        state.generation += 1
        return state


def _context(organization_id=None, staff_member_id=None, facility_ids=()):
    return CommunicationStaffContext(
        account_id=uuid4(),
        organization_id=organization_id or uuid4(),
        staff_member_id=staff_member_id or uuid4(),
        facility_ids=tuple(facility_ids),
        role_codes=("practitioner",),
    )


def _service(provider, repository=None):
    return RealtimeService(
        provider=provider,
        repository=repository or InMemoryChannelRepository(),
        namespace=NAMESPACE,
        ttl_seconds=1800,
        renew_after_seconds=900,
        presence_enabled=True,
        max_presence_channels=10,
    )


def test_namespace_validation_rejects_injection():
    assert validate_namespace("dev") == "dev"
    with pytest.raises(ValueError):
        validate_namespace("dev:*")


def test_capabilities_are_subscribe_only_plus_presence():
    own = own_user_channel("dev", "org", "staff", 3)
    presence = facility_presence_channel("dev", "org", "fac")
    caps = build_capabilities(own, [presence])
    assert caps[own] == ["subscribe"]
    assert caps[presence] == ["subscribe", "presence"]
    assert all("publish" not in ops for ops in caps.values())
    assert build_client_id("org", "staff") == "staff:org:staff"


def test_token_scope_derives_from_context_only():
    provider = RecordingTokenProvider()
    service = _service(provider)
    own_org, own_staff = uuid4(), uuid4()
    facility = uuid4()
    foreign_org = uuid4()

    result = run(
        service.issue_token(None, _context(own_org, own_staff, facility_ids=[facility]))
    )

    assert result["channels"]["own"] == own_user_channel(NAMESPACE, own_org, own_staff, 1)
    assert result["channels"]["presence"] == [
        facility_presence_channel(NAMESPACE, own_org, facility)
    ]
    assert str(foreign_org) not in json.dumps(result)
    caps = result["capabilities"]
    assert all("publish" not in ops for ops in caps.values())
    assert provider.calls[0]["client_id"] == build_client_id(own_org, own_staff)


def test_missing_key_fails_closed_without_token():
    service = _service(RecordingTokenProvider(configured=False))
    with pytest.raises(ProviderUnavailable):
        run(service.issue_token(None, _context()))
    config = service.feature_config()
    assert config["available"] is False
    assert config["reason"] == "ably_not_configured"


def test_generation_bump_retires_old_channel():
    provider = RecordingTokenProvider()
    repository = InMemoryChannelRepository()
    service = _service(provider, repository)
    context = _context()

    first = run(service.issue_token(None, context))
    run(
        repository.bump_generation(
            None,
            organization_id=context.organization_id,
            staff_member_id=context.staff_member_id,
        )
    )
    second = run(service.issue_token(None, context))

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert first["channels"]["own"] != second["channels"]["own"]


def test_real_ably_adapter_signs_locally_and_redacts_key():
    secret = "topsecret-should-never-appear"
    provider = AblyRealtimeProvider(
        api_key=f"fake.key:{secret}", namespace="dev", presence_enabled=True
    )
    assert secret not in repr(provider)
    own = own_user_channel("dev", "org", "staff", 1)
    token_request = run(
        provider.create_token_request(
            client_id="staff:org:staff", capabilities={own: ["subscribe"]}, ttl_seconds=60
        )
    )
    assert set(token_request) >= {"keyName", "clientId", "capability", "mac", "nonce", "timestamp"}
    assert "ApiKey" not in token_request
    assert secret not in json.dumps(token_request)


class _Result:
    def __init__(self, *, first=None, all=None):
        self._first = first
        self._all = all or []

    def first(self):
        return self._first

    def all(self):
        return self._all


class _RecordingDb:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, statement):
        return self._results.pop(0)


def test_resolver_rejects_inactive_or_foreign_membership():
    account = SimpleNamespace(id=uuid4())
    with pytest.raises(HTTPException) as excinfo:
        run(resolve_staff_context(_RecordingDb([_Result(first=None)]), account))
    assert excinfo.value.status_code == 403


def test_resolver_uses_account_membership_not_client_input():
    account = SimpleNamespace(id=uuid4())
    staff = SimpleNamespace(id=uuid4())
    organization = SimpleNamespace(id=uuid4())
    facility_id = uuid4()
    db = _RecordingDb(
        [
            _Result(first=(staff, organization)),
            _Result(all=[(facility_id,)]),
            _Result(all=[("practitioner",)]),
        ]
    )
    context = run(resolve_staff_context(db, account))
    assert context.organization_id == organization.id
    assert context.staff_member_id == staff.id
    assert context.facility_ids == (facility_id,)


def test_http_config_and_token_scope_and_missing_key(client):
    facility_id = uuid4()
    good_context = _context(facility_ids=[facility_id])
    good_service = _service(RecordingTokenProvider())

    app.dependency_overrides[get_current_identity_account] = lambda: SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_communication_staff_context] = lambda: good_context
    app.dependency_overrides[get_realtime_service] = lambda: good_service
    try:
        # A malicious client body must be ignored: no channel/tenant override.
        response = client.post(
            "/api/v1/communication/realtime/token",
            json={"organization_id": str(uuid4()), "channel": "dev:*", "client_id": "attacker"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["channels"]["own"] == own_user_channel(
            NAMESPACE, good_context.organization_id, good_context.staff_member_id, 1
        )
        assert "attacker" not in response.text
        assert good_service.provider.calls[0]["client_id"] == build_client_id(
            good_context.organization_id, good_context.staff_member_id
        )

        config = client.get("/api/v1/communication/realtime/config")
        assert config.status_code == 200
        assert config.json()["data"]["available"] is True

        app.dependency_overrides[get_realtime_service] = lambda: _service(
            RecordingTokenProvider(configured=False)
        )
        unavailable = client.post("/api/v1/communication/realtime/token", json={})
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
        assert "token_request" not in unavailable.text
    finally:
        app.dependency_overrides.clear()


def test_http_requires_membership(client):
    def _forbidden():
        raise HTTPException(status_code=403, detail="No active HealthOS organization access.")

    app.dependency_overrides[get_current_identity_account] = lambda: SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_communication_staff_context] = _forbidden
    app.dependency_overrides[get_realtime_service] = lambda: _service(RecordingTokenProvider())
    try:
        response = client.post("/api/v1/communication/realtime/token", json={})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------ Ably transport resilience


def test_ably_client_is_built_with_bounded_http_timeouts(monkeypatch):
    import ably

    captured: dict = {}

    class _FakeAblyRest:
        def __init__(self, key, **kwargs):
            captured["key"] = key
            captured.update(kwargs)

    monkeypatch.setattr(ably, "AblyRest", _FakeAblyRest)
    provider = AblyRealtimeProvider(
        api_key="test.key:secret", namespace="dev", presence_enabled=False
    )
    try:
        provider._client()
        assert captured["http_open_timeout"] == 5
        assert captured["http_request_timeout"] == 10
        assert captured["http_max_retry_count"] == 2
        assert captured["http_max_retry_duration"] == 15
    finally:
        provider.reset()


def test_ably_provider_drops_cached_client_on_cancelled_publish(monkeypatch):
    from src.app.domains.communication.realtime.providers import ably as ably_module

    class _HangingChannel:
        async def publish(self, *args, **kwargs):
            await asyncio.sleep(30)

    class _Channels:
        def get(self, name):
            return _HangingChannel()

    class _FakeClient:
        channels = _Channels()

    key = "test.key:secret"
    provider = AblyRealtimeProvider(api_key=key, namespace="dev", presence_enabled=False)
    monkeypatch.setitem(ably_module._CLIENTS, key, _FakeClient())

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                provider.publish(channel="dev:t:test", event_type="message.created", payload={}),
                timeout=0.2,
            )

    run(scenario())
    assert key not in ably_module._CLIENTS
