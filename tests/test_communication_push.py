"""Push unit tests with fakes (no DB, no real FCM).

Covers device ownership/rebind/revoke, missing configuration, P3/quiet/TTL
eligibility, duplicate-job dedupe and the generic data-only payload.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.app.api.dependencies import get_current_identity_account
from src.app.domains.communication.dependencies import (
    CommunicationStaffContext,
    get_communication_staff_context,
)
from src.app.domains.communication.push.constants import GENERIC_BODY, GENERIC_TITLE
from src.app.domains.communication.push.providers.fcm import (
    FcmPushProvider,
    build_push_provider,
)
from src.app.domains.communication.push.service import (
    PushService,
    device_item,
    is_quiet_now,
    push_configuration_reason,
    push_configured,
    push_sdk_available,
)


def run(coro):
    return asyncio.run(coro)


def _context(organization_id=None, staff_member_id=None):
    return CommunicationStaffContext(
        account_id=uuid4(),
        organization_id=organization_id or uuid4(),
        staff_member_id=staff_member_id or uuid4(),
        facility_ids=(),
        role_codes=("practitioner",),
    )


def _device(**overrides):
    now = datetime.now(UTC)
    base = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "staff_member_id": uuid4(),
        "user_account_id": uuid4(),
        "installation_id": "install-1",
        "token": "token-1",
        "platform": "web",
        "environment": "dev",
        "user_agent": None,
        "is_active": True,
        "last_seen_at": now,
        "revoked_at": None,
        "revoked_reason": None,
        "created_at": now,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeDb:
    async def commit(self):
        return None

    async def flush(self):
        return None

    async def rollback(self):
        return None


class FakePushRepository:
    def __init__(self, devices=None):
        self.devices = list(devices or [])
        self.revoked: list[tuple] = []

    async def get(self, db, device_id):
        return next((d for d in self.devices if d.id == device_id), None)

    async def get_by_token(self, db, token, for_update=False):
        return next((d for d in self.devices if d.token == token), None)

    async def get_by_installation(
        self, db, *, organization_id, staff_member_id, installation_id, for_update=False
    ):
        return next(
            (
                d
                for d in self.devices
                if d.organization_id == organization_id
                and d.staff_member_id == staff_member_id
                and d.installation_id == installation_id
            ),
            None,
        )

    async def create(self, db, **values):
        device = _device(**{k: v for k, v in values.items() if k != "id"})
        self.devices.append(device)
        return device

    async def revoke(self, db, device, *, reason, now=None):
        device.is_active = False
        device.revoked_at = now or datetime.now(UTC)
        device.revoked_reason = reason
        self.revoked.append((device.id, reason))

    async def list_for_staff(self, db, staff_member_id, active_only=True):
        return [
            d
            for d in self.devices
            if d.staff_member_id == staff_member_id and (d.is_active or not active_only)
        ]

    async def active_devices_for_staff_ids(self, db, staff_member_ids):
        return [
            d
            for d in self.devices
            if d.staff_member_id in staff_member_ids and d.is_active and d.revoked_at is None
        ]

    async def revoke_for_account(self, db, user_account_id, *, reason, now=None):
        count = 0
        for device in self.devices:
            if device.user_account_id == user_account_id and device.is_active:
                await self.revoke(db, device, reason=reason, now=now)
                count += 1
        return count


class FakeNotificationRepository:
    def __init__(self, preferences=None):
        self.preferences = preferences or []

    async def get_preferences(self, db, *, organization_id, staff_member_id):
        return self.preferences


class FakeDelivery:
    def __init__(self):
        self.jobs: dict[str, dict] = {}

    async def enqueue(self, db, **kwargs):
        self.jobs.setdefault(kwargs["dedup_key"], kwargs)
        return SimpleNamespace(), True


def _settings(**overrides):
    base = {
        "FCM_ENABLED": True,
        "FIREBASE_PROJECT_ID": None,
        "FIREBASE_SERVICE_ACCOUNT_FILE": None,
        "FIREBASE_CREDENTIALS_JSON": None,
        "FCM_DEFAULT_TTL_SECONDS": 3600,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _event(**overrides):
    base = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "facility_id": None,
        "priority": "P2",
        "category": "queue",
        "event_type": "queue.ready",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _prefs(*, browser=True, desktop=False, quiet=False, start="22:00", end="07:00"):
    rows = []
    if browser is not None:
        rows.append(SimpleNamespace(category="queue", browser=browser))
    rows.append(
        SimpleNamespace(
            category="all",
            desktop_alerts=desktop,
            quiet_hours_enabled=quiet,
            quiet_start=start,
            quiet_end=end,
            timezone="Asia/Kolkata",
        )
    )
    return rows


# ---------------------------------------------------------------- pure/config


def test_quiet_hours_day_and_overnight():
    noon = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    early_afternoon = datetime(2026, 9, 25, 13, 30, tzinfo=UTC)
    night = datetime(2026, 9, 25, 23, 30, tzinfo=UTC)
    assert is_quiet_now(enabled=True, start="22:00", end="07:00", timezone_name="UTC", now=night)
    assert not is_quiet_now(enabled=True, start="22:00", end="07:00", timezone_name="UTC", now=noon)
    assert is_quiet_now(
        enabled=True, start="13:00", end="14:00", timezone_name="UTC", now=early_afternoon
    )
    assert not is_quiet_now(enabled=False, start="13:00", end="14:00", timezone_name="UTC", now=noon)


def test_missing_config_reports_unavailable():
    settings = _settings()
    assert push_sdk_available() is True  # declared dependency
    assert push_configured(settings) is False
    service = PushService(enabled=True, provider_configured=False)
    status = service.config_status()
    assert status["available"] is False
    assert status["reason"] == "fcm_not_configured"
    assert status["display_strategy"] == "service_worker_data_only"
    assert build_push_provider(settings) is None


def test_service_account_file_configuration(tmp_path):
    missing = _settings(
        FIREBASE_PROJECT_ID="project-1",
        FIREBASE_SERVICE_ACCOUNT_FILE=str(tmp_path / "absent.json"),
    )
    assert push_configured(missing) is False
    assert push_configuration_reason(missing) == "fcm_credentials_missing"

    present = tmp_path / "service-account.json"
    present.write_text('{"project_id":"project-1"}')
    configured = _settings(FIREBASE_SERVICE_ACCOUNT_FILE=str(present))
    assert push_configured(configured) is True
    assert push_configuration_reason(configured) is None


def test_device_item_hides_token():
    item = device_item(_device())
    assert "token" not in item
    assert item["active"] is True


# ---------------------------------------------------------------- ownership


def test_register_rebinds_token_from_previous_account():
    org_id, staff_id = uuid4(), uuid4()
    previous = _device(token="shared-token", organization_id=org_id)
    repository = FakePushRepository([previous])
    service = PushService(
        enabled=True, provider_configured=True, repository=repository
    )
    context = _context(org_id, staff_id)
    result = run(
        service.register_device(
            FakeDb(),
            context,
            {"token": "shared-token", "installation_id": "install-new"},
        )
    )
    assert result["active"] is True
    assert (previous.id, "rebound") in repository.revoked
    new_device = next(d for d in repository.devices if d.id != previous.id)
    assert new_device.staff_member_id == staff_id
    assert new_device.user_account_id == context.account_id


def test_register_same_installation_updates_in_place():
    context = _context()
    existing = _device(
        organization_id=context.organization_id,
        staff_member_id=context.staff_member_id,
        user_account_id=context.account_id,
        installation_id="install-1",
        token="old-token",
        is_active=False,
        revoked_at=datetime.now(UTC),
    )
    repository = FakePushRepository([existing])
    service = PushService(enabled=True, provider_configured=True, repository=repository)
    run(
        service.register_device(
            FakeDb(),
            context,
            {"token": "new-token", "installation_id": "install-1"},
        )
    )
    assert existing.token == "new-token"
    assert existing.is_active is True
    assert existing.revoked_at is None


def test_revoke_foreign_device_is_denied():
    device = _device()
    service = PushService(
        enabled=True, provider_configured=True, repository=FakePushRepository([device])
    )
    with pytest.raises(HTTPException) as denied:
        run(service.revoke_device(FakeDb(), _context(), device.id))
    assert denied.value.status_code == 404


def test_revoke_all_devices_only_own_active():
    account_id, staff_id = uuid4(), uuid4()
    own = _device(
        user_account_id=account_id, staff_member_id=staff_id, is_active=True
    )
    foreign = _device(is_active=True)
    repository = FakePushRepository([own, foreign])
    service = PushService(enabled=True, provider_configured=True, repository=repository)
    context = CommunicationStaffContext(
        account_id=account_id,
        organization_id=own.organization_id,
        staff_member_id=staff_id,
        facility_ids=(),
        role_codes=(),
    )
    result = run(service.revoke_all_devices(FakeDb(), context))
    assert result["revoked"] == 1
    assert own.is_active is False
    assert foreign.is_active is True


def test_revoke_account_devices_on_logout():
    account_id = uuid4()
    devices = [_device(user_account_id=account_id), _device(user_account_id=account_id)]
    repository = FakePushRepository(devices)
    service = PushService(enabled=True, provider_configured=True, repository=repository)
    count = run(service.revoke_account_devices(FakeDb(), account_id, reason="logout"))
    assert count == 2
    assert all(d.is_active is False for d in devices)


# ---------------------------------------------------------------- eligibility


def _enqueue_service(devices, preferences):
    return PushService(
        enabled=True,
        provider_configured=True,
        repository=FakePushRepository(devices),
        notifications=FakeNotificationRepository(preferences),
        delivery=FakeDelivery(),
    )


def test_eligible_p2_queues_one_generic_job_and_dedupes():
    staff_id = uuid4()
    device = _device(staff_member_id=staff_id)
    service = _enqueue_service([device], _prefs(desktop=True))
    event = _event()
    first = run(service.enqueue_for_notification(None, event=event, recipient_staff_ids=[staff_id]))
    second = run(service.enqueue_for_notification(None, event=event, recipient_staff_ids=[staff_id]))
    assert first == 1 and second == 1
    assert len(service.delivery.jobs) == 1  # duplicate dedup
    job = next(iter(service.delivery.jobs.values()))
    assert job["channel"] == "push"
    assert job["payload"]["notification_id"] == str(event.id)
    assert "body" not in job["payload"]


def test_p3_and_missing_desktop_alerts_and_quiet_are_skipped():
    staff_id = uuid4()
    device = _device(staff_member_id=staff_id)
    service = _enqueue_service([device], _prefs(desktop=True))
    assert run(service.enqueue_for_notification(
        None, event=_event(priority="P3"), recipient_staff_ids=[staff_id]
    )) == 0

    service = _enqueue_service([device], _prefs(desktop=False))
    assert run(service.enqueue_for_notification(
        None, event=_event(), recipient_staff_ids=[staff_id]
    )) == 0

    now = datetime.now(UTC)
    night = now.replace(hour=23, minute=30)
    service = _enqueue_service([device], _prefs(desktop=True, quiet=True))
    assert run(service.device_eligible(
        None, event=_event(), device=device, now=night
    )) is False


def test_expired_event_is_skipped():
    staff_id = uuid4()
    device = _device(staff_member_id=staff_id)
    service = _enqueue_service([device], _prefs(desktop=True))
    assert run(service.enqueue_for_notification(
        None,
        event=_event(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
        recipient_staff_ids=[staff_id],
    )) == 0


# ---------------------------------------------------------------- FCM payload


def test_fcm_message_is_data_only_and_generic():
    provider = FcmPushProvider(settings=_settings(FIREBASE_PROJECT_ID="project-1"))
    event = _event()
    message = provider._message(token="device-token", event=event)
    assert message.token == "device-token"
    assert message.notification is None  # data-only: service worker displays once
    data = message.data
    assert data["title"] == GENERIC_TITLE
    assert data["body"] == GENERIC_BODY
    assert data["tag"] == f"healthos-notification-{event.id}"
    assert data["deep_link"] == "/notifications"
    assert "patient" not in str(data).lower()
    assert "token" not in str(data).lower()


# ---------------------------------------------------------------- http


def test_http_push_routes_registered(client, monkeypatch):
    from src.app.domains.communication.push import router as push_router
    from src.app.main import app

    class FakePush:
        def config_status(self):
            return {"available": False, "provider": "fcm", "reason": "fcm_not_configured"}

        async def list_devices(self, db, context):
            return []

    monkeypatch.setattr(push_router, "PUSH", FakePush())
    app.dependency_overrides[get_current_identity_account] = lambda: SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_communication_staff_context] = lambda: _context()
    try:
        config = client.get("/api/v1/communication/push/config")
        assert config.status_code == 200
        assert config.json()["data"]["available"] is False
        listed = client.get("/api/v1/communication/push/devices")
        assert listed.status_code == 200
        assert listed.json()["data"]["items"] == []
    finally:
        app.dependency_overrides.clear()
