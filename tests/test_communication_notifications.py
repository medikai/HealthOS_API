"""Notification unit tests (no DB) plus workflow coalescing.

Covers serialization/read-vs-task separation, cursor roundtrip, pagination,
read-all cutoff scope, catch-up expiry, preference policy and dispatch recheck.
DB-backed concurrency checks live in ``test_communication_notifications_db.py``
and run only against a disposable database.
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
from src.app.domains.communication.notifications import workflow
from src.app.domains.communication.notifications.constants import (
    EVENT_QUEUE_READY,
    EVENT_TOKEN_ASSIGNED,
)
from src.app.domains.communication.notifications.serialization import (
    decode_cursor,
    encode_cursor,
    is_needs_action,
    notification_item,
    preference_defaults,
)
from src.app.domains.communication.notifications.service import NotificationService


def run(coro):
    return asyncio.run(coro)


def _event(**overrides):
    now = datetime.now(UTC)
    base = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "facility_id": uuid4(),
        "event_type": "queue.ready",
        "title": "Patient ready",
        "category": "queue",
        "priority": "P1",
        "kind": "task",
        "dedup_key": "d",
        "context": "Token T-7 is ready",
        "location": "T-7",
        "actor_name": "Nurse",
        "actor_role": "nurse",
        "actor_user_id": None,
        "task_type": "queue_entry",
        "task_id": str(uuid4()),
        "task_state": "awaiting",
        "action_kind": "open",
        "action_label": "Open queue",
        "resource_type": "queue_entry",
        "resource_id": None,
        "coalesce_key": None,
        "revision": 1,
        "superseded_by_id": None,
        "occurred_at": now,
        "updated_at": now,
        "expires_at": None,
        "created_at": now,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _recipient(**overrides):
    base = {
        "id": uuid4(),
        "notification_id": None,
        "organization_id": uuid4(),
        "staff_member_id": uuid4(),
        "facility_id": None,
        "read_at": None,
        "delivered_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _context(facility_ids=()):
    return CommunicationStaffContext(
        account_id=uuid4(),
        organization_id=uuid4(),
        staff_member_id=uuid4(),
        facility_ids=tuple(facility_ids),
        role_codes=("practitioner",),
    )


# ------------------------------------------------------------------ pure


def test_item_serialization_keeps_read_and_task_state_independent():
    event = _event()
    recipient = _recipient()
    item = notification_item(event, recipient, facility_name="Main")
    assert item["priority"] == "P1"
    assert item["kind"] == "task"
    assert item["read"] is False
    assert item["taskState"] == "awaiting"
    assert item["action"] == {"kind": "open", "label": "Open queue"}
    assert item["facility"] == {"id": str(event.facility_id), "name": "Main"}
    assert is_needs_action(event) is True

    recipient.read_at = datetime.now(UTC)
    read_item = notification_item(event, recipient)
    assert read_item["read"] is True
    assert read_item["taskState"] == "awaiting"  # unchanged by read


def test_superseded_event_is_not_needs_action():
    event = _event(task_state="superseded", superseded_by_id=uuid4())
    assert is_needs_action(event) is False


def test_cursor_roundtrip_and_invalid():
    now = datetime.now(UTC)
    cursor = encode_cursor(now, uuid4())
    decoded = decode_cursor(cursor)
    assert decoded is not None
    assert decoded[0] == now
    assert decode_cursor("not-a-cursor") is None


def test_preference_defaults_p3_is_quiet():
    assert preference_defaults("queue")["browser"] is True
    assert preference_defaults("queue")["locked"] is True
    assert preference_defaults("team")["browser"] is False
    assert preference_defaults("team")["sound"] is False


# ------------------------------------------------------------- inbox service


class FakeDb:
    async def commit(self):
        return None


class FakeNotificationRepository:
    def __init__(self):
        self.list_rows = []
        self.get_result = None
        self.mark_read_result = True
        self.mark_all_calls = []
        self.changes_rows = []
        self.preferences = {}

    async def list_for_staff(self, db, **kwargs):
        return self.list_rows

    async def counts(self, db, **kwargs):
        return {"unread": 1, "needsAction": 1}

    async def get_for_staff(self, db, **kwargs):
        return self.get_result

    async def mark_read(self, db, **kwargs):
        return self.mark_read_result

    async def mark_all_read(self, db, **kwargs):
        self.mark_all_calls.append(kwargs)
        return 3

    async def changes_since(self, db, **kwargs):
        return self.changes_rows

    async def get_preferences(self, db, **kwargs):
        return list(self.preferences.values())

    async def upsert_preference(self, db, **kwargs):
        values = kwargs["values"]
        self.preferences[kwargs["category"]] = SimpleNamespace(
            category=kwargs["category"],
            in_app=values.get("in_app", True),
            browser=values.get("browser", True),
            sound=values.get("sound", False),
            desktop_alerts=values.get("desktop_alerts", False),
            quiet_hours_enabled=values.get("quiet_hours_enabled", False),
            quiet_start=values.get("quiet_start", "22:00"),
            quiet_end=values.get("quiet_end", "07:00"),
            timezone=values.get("timezone", "Asia/Kolkata"),
        )


def test_list_pagination_and_cursor():
    repository = FakeNotificationRepository()
    rows = [(_event(), _recipient(), "Main") for _ in range(3)]
    repository.list_rows = rows  # limit 2 => has_more
    service = NotificationService(repository)
    result = run(service.list_notifications(None, _context(), tab="all", limit=2))
    assert len(result["items"]) == 2
    assert result["has_more"] is True
    assert result["next_cursor"] is not None
    assert result["counts"] == {"unread": 1, "needsAction": 1}


def test_list_rejects_bad_tab_and_foreign_facility():
    service = NotificationService(FakeNotificationRepository())
    with pytest.raises(HTTPException) as bad_tab:
        run(service.list_notifications(None, _context(), tab="nope"))
    assert bad_tab.value.status_code == 422
    with pytest.raises(HTTPException) as foreign:
        run(service.list_notifications(None, _context(), facility_uuid=str(uuid4())))
    assert foreign.value.status_code == 403


def test_mark_read_is_idempotent_and_read_all_scope_uses_cutoff():
    repository = FakeNotificationRepository()
    event = _event()
    recipient = _recipient(read_at=datetime.now(UTC))
    repository.get_result = (event, recipient, "Main")
    service = NotificationService(repository)

    item = run(service.mark_read(None, _context(), event.id))
    assert item["read"] is True
    assert item["taskState"] == "awaiting"

    cutoff = datetime.now(UTC)
    scope = {"tab": "action", "priority": "P1", "facility_uuid": None}
    result = run(service.mark_all_read(None, _context(), cutoff=cutoff, scope=scope))
    assert result["updated"] == 3
    assert result["cutoff"] == cutoff.isoformat()
    call = repository.mark_all_calls[0]
    assert call["tab"] == "action"
    assert call["priority"] == "P1"
    assert call["cutoff"] == cutoff


def test_changes_expired_cursor_returns_410():
    service = NotificationService(FakeNotificationRepository())
    with pytest.raises(HTTPException) as expired:
        run(
            service.changes(
                None, _context(), since=datetime.now(UTC) - timedelta(days=30)
            )
        )
    assert expired.value.status_code == 410
    assert expired.value.detail["code"] == "CURSOR_EXPIRED"


def test_changes_returns_overlap_cursor():
    repository = FakeNotificationRepository()
    repository.changes_rows = [(_event(), _recipient(), "Main")]
    service = NotificationService(repository)
    result = run(service.changes(None, _context(), since=datetime.now(UTC) - timedelta(minutes=5)))
    assert result["resync_required"] is False
    assert result["next_cursor"] is not None
    assert result["server_time"] is not None


def test_preferences_patch_rejects_locked_category_and_persists():
    service = NotificationService(FakeNotificationRepository())
    with pytest.raises(HTTPException) as locked:
        run(
            service.patch_preferences(
                FakeDb(),
                _context(),
                {"categories": [{"code": "queue", "inApp": False}]},
            )
        )
    assert locked.value.status_code == 422

    updated = run(
        service.patch_preferences(
            FakeDb(),
            _context(),
            {
                "categories": [{"code": "team", "browser": True}],
                "desktopAlerts": True,
                "quietHours": {"enabled": True, "start": "21:00", "end": "06:00", "timezone": "Asia/Kolkata"},
            },
        )
    )
    assert updated["desktopAlerts"] is True
    assert updated["quietHours"]["enabled"] is True
    team = next(c for c in updated["categories"] if c["category"] == "team")
    assert team["browser"] is True


# ------------------------------------------------------------- dispatch recheck


class _ScalarDb:
    def __init__(self, results):
        self.results = list(results)
        self.flushed = 0

    async def scalar(self, statement):
        return self.results.pop(0)

    async def flush(self):
        self.flushed += 1


def test_evaluate_dispatch_superseded_and_ineligible():
    service = NotificationService()
    db = _ScalarDb([])
    assert run(service.evaluate_dispatch(db, _event(superseded_by_id=uuid4()), _recipient())) == "superseded"
    db = _ScalarDb([None])  # no active assignment
    assert run(service.evaluate_dispatch(db, _event(), _recipient())) == "ineligible"


def test_evaluate_dispatch_stale_queue_action_is_resolved():
    service = NotificationService()
    event = _event(event_type=EVENT_QUEUE_READY, task_state="awaiting")
    db = _ScalarDb([uuid4(), "completed"])
    decision = run(service.evaluate_dispatch(db, event, _recipient()))
    assert decision == "stale"
    assert event.task_state == "resolved"
    assert db.flushed == 1


def test_evaluate_dispatch_publishes_when_current():
    service = NotificationService()
    event = _event(event_type=EVENT_QUEUE_READY)
    db = _ScalarDb([uuid4(), "called"])
    assert run(service.evaluate_dispatch(db, event, _recipient())) == "publish"


# ------------------------------------------------------------- workflow emit


class _RecordingDelivery:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, db, **kwargs):
        self.jobs.append(kwargs)
        return SimpleNamespace(), True


class _CoalescingRepo:
    def __init__(self, existing):
        self.existing = existing
        self.superseded = []

    async def find_coalescible(self, db, **kwargs):
        return self.existing

    async def bump_revision(self, db, event, *, values):
        for key, value in values.items():
            setattr(event, key, value)
        event.revision += 1
        event.updated_at = datetime.now(UTC)
        return event


def test_ready_coalesces_unread_arrival_into_one_item(monkeypatch):
    arrivals = _event(
        event_type=EVENT_TOKEN_ASSIGNED,
        kind="update",
        task_state="none",
        action_kind=None,
        action_label=None,
        coalesce_key=f"queue:{uuid4()}",
    )
    entry = SimpleNamespace(
        id=uuid4(),
        organization_id=arrivals.organization_id,
        facility_id=arrivals.facility_id,
        practitioner_id=uuid4(),
        token_number=7,
    )
    arrivals.coalesce_key = f"queue:{entry.id}"
    recipient = _recipient(notification_id=arrivals.id)
    delivery = _RecordingDelivery()

    async def fake_practitioner(db, **kwargs):
        return [recipient.staff_member_id]

    async def fake_exclude(db, ids, actor):
        return ids

    async def fake_actor(db, actor):
        return ("Nurse", "nurse")

    monkeypatch.setattr(workflow, "REPOSITORY", _CoalescingRepo((arrivals, recipient)))
    monkeypatch.setattr(workflow, "DELIVERY", delivery)
    monkeypatch.setattr(workflow, "practitioner_staff_ids", fake_practitioner)
    monkeypatch.setattr(workflow, "exclude_actor", fake_exclude)
    monkeypatch.setattr(workflow, "acting_actor", fake_actor)

    event = run(workflow.emit_queue_ready(None, entry=entry, actor_user_id=uuid4()))
    assert event is arrivals
    assert event.event_type == EVENT_QUEUE_READY
    assert event.task_state == "awaiting"
    assert event.revision == 2
    assert delivery.jobs and delivery.jobs[0]["channel"] == "realtime"
    assert delivery.jobs[0]["payload"]["notification_id"] == str(arrivals.id)


def test_record_notification_never_broadcasts_to_empty_recipients():
    assert run(
        workflow.record_notification(
            None,
            event_type=EVENT_QUEUE_READY,
            organization_id=uuid4(),
            facility_id=uuid4(),
            actor_user_id=uuid4(),
            recipient_staff_ids=[],
            title="x",
            dedup_key="k",
        )
    ) is None


# ------------------------------------------------------------------ http


class FakeHttpService:
    async def list_notifications(self, db, context, **kwargs):
        return {"items": [], "next_cursor": None, "has_more": False, "counts": {"unread": 0, "needsAction": 0}}

    async def counts(self, db, context, **kwargs):
        return {"unread": 2, "needsAction": 1}

    async def mark_all_read(self, db, context, **kwargs):
        return {"updated": 0, "cutoff": datetime.now(UTC).isoformat(), "scope": kwargs.get("scope")}


def test_http_notification_routes_and_scope_parsing(client, monkeypatch):
    from src.app.domains.communication.notifications import router as notif_router
    from src.app.main import app

    monkeypatch.setattr(notif_router, "SERVICE", FakeHttpService())
    app.dependency_overrides[get_current_identity_account] = lambda: SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_communication_staff_context] = lambda: _context()
    try:
        listed = client.get("/api/v1/communication/notifications?tab=unread")
        assert listed.status_code == 200
        assert listed.json()["data"]["items"] == []

        counts = client.get("/api/v1/communication/notifications/counts")
        assert counts.status_code == 200
        assert counts.json()["data"]["unread"] == 2

        missing_since = client.get("/api/v1/communication/notifications/changes")
        assert missing_since.status_code == 422

        read_all = client.post(
            "/api/v1/communication/notifications/read-all",
            json={"scope": {"tab": "action", "priority": "P1"}},
        )
        assert read_all.status_code == 200
        assert read_all.json()["data"]["scope"] == {"tab": "action", "priority": "P1", "facilityUuid": None}
    finally:
        app.dependency_overrides.clear()
