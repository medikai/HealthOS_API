"""Chat and work-status unit tests with in-memory fakes (no DB).

Covers sender-from-auth, idempotency conflict, direct dedupe, read monotonicity,
XSS-as-text, membership revocation scope and work-status override preservation.
DB concurrency/race checks live in ``test_communication_chat_db.py``.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

import src.app.domains.communication.chat.service as chat_service
from src.app.api.dependencies import get_current_identity_account
from src.app.domains.communication.chat.constants import (
    CONVERSATION_DIRECT,
    CONVERSATION_TEAM,
)
from src.app.domains.communication.chat.serialization import (
    conversation_item,
    message_item,
)
from src.app.domains.communication.chat.service import ChatService, _direct_key
from src.app.domains.communication.dependencies import (
    CommunicationStaffContext,
    get_communication_staff_context,
)
from src.app.domains.communication.status.repository import WorkStatusRepository
from src.app.domains.communication.status.service import WorkStatusService
from src.app.models.communication import (
    WORK_STATUS_SOURCE_CONSULTATION,
    WORK_STATUS_SOURCE_STAFF,
    Conversation,
    ConversationMember,
    Message,
    WorkStatus,
)


def run(coro):
    return asyncio.run(coro)


def _context(organization_id=None, staff_member_id=None, facility_ids=()):
    return CommunicationStaffContext(
        account_id=uuid4(),
        organization_id=organization_id or uuid4(),
        staff_member_id=staff_member_id or uuid4(),
        facility_ids=tuple(facility_ids),
        role_codes=("practitioner",),
    )


# ------------------------------------------------------------- pure helpers


def test_direct_key_is_order_independent():
    first, second = uuid4(), uuid4()
    assert _direct_key(first, second) == _direct_key(second, first)


def test_message_item_is_plain_text_and_marks_persisted_ack():
    conversation_id = uuid4()
    sender_id = uuid4()
    message = Message(
        conversation_id=conversation_id,
        organization_id=uuid4(),
        sender_staff_id=sender_id,
        body="<script>alert(1)</script>",
        sequence=1,
    )
    item = message_item(message, mine=True, sender_name="Nurse")
    assert item["body"] == "<script>alert(1)</script>"  # text, never HTML
    assert item["delivery"] == "sent"  # persisted, not a recipient receipt
    assert item["receipt"] is None
    assert item["mine"] is True
    assert item["senderId"] == str(sender_id)


def test_conversation_preview_is_collapsed_and_truncated():
    conversation = Conversation(organization_id=uuid4(), kind=CONVERSATION_DIRECT)
    membership = ConversationMember(
        conversation_id=conversation.id,
        organization_id=conversation.organization_id,
        staff_member_id=uuid4(),
    )
    item = conversation_item(
        conversation,
        membership,
        member_ids=[membership.staff_member_id],
        last_body="line one\n" + "x" * 300,
    )
    assert "\n" not in item["lastMessagePreview"]
    assert len(item["lastMessagePreview"]) <= 120


# ------------------------------------------------------------- fake plumbing


class FakeChatRepository:
    def __init__(self):
        self.conversations: dict = {}
        self.members: list[ConversationMember] = []
        self.messages: dict = {}

    async def get_conversation(self, db, conversation_id):
        return self.conversations.get(conversation_id)

    async def lock_conversation(self, db, conversation_id):
        return self.conversations.get(conversation_id)

    async def get_membership(self, db, *, conversation_id, staff_member_id):
        for member in self.members:
            if (
                member.conversation_id == conversation_id
                and member.staff_member_id == staff_member_id
                and member.left_at is None
            ):
                return member
        return None

    async def list_members(self, db, conversation_id):
        return [
            m
            for m in self.members
            if m.conversation_id == conversation_id and m.left_at is None
        ]

    async def members_for_conversations(self, db, conversation_ids):
        grouped: dict = {}
        for member in self.members:
            if member.conversation_id in conversation_ids and member.left_at is None:
                grouped.setdefault(member.conversation_id, []).append(member)
        return grouped

    async def create_conversation(
        self, db, *, organization_id, kind, direct_key, facility_id, title, created_by_staff_id
    ):
        if direct_key is not None:
            for conversation in self.conversations.values():
                if (
                    conversation.organization_id == organization_id
                    and conversation.direct_key == direct_key
                ):
                    return conversation, False
        conversation = Conversation(
            organization_id=organization_id,
            kind=kind,
            direct_key=direct_key,
            facility_id=facility_id,
            title=title,
            created_by_staff_id=created_by_staff_id,
        )
        self.conversations[conversation.id] = conversation
        return conversation, True

    async def add_member(
        self, db, *, conversation_id, organization_id, staff_member_id, role="member", join_sequence=0
    ):
        for member in self.members:
            if member.conversation_id == conversation_id and member.staff_member_id == staff_member_id:
                member.left_at = None
                return member
        member = ConversationMember(
            conversation_id=conversation_id,
            organization_id=organization_id,
            staff_member_id=staff_member_id,
            role=role,
            join_sequence=join_sequence,
            last_read_sequence=join_sequence,
        )
        self.members.append(member)
        return member

    async def remove_member(self, db, membership):
        membership.left_at = datetime.now(UTC)

    async def find_message_by_client_id(
        self, db, *, conversation_id, sender_staff_id, client_message_id
    ):
        for message in self.messages.values():
            if (
                message.conversation_id == conversation_id
                and message.sender_staff_id == sender_staff_id
                and message.client_message_id == client_message_id
            ):
                return message
        return None

    async def get_message(self, db, message_id):
        return self.messages.get(message_id)

    async def list_messages(self, db, *, conversation_id, before_sequence, limit):
        rows = [
            m for m in self.messages.values() if m.conversation_id == conversation_id
        ]
        if before_sequence is not None:
            rows = [m for m in rows if m.sequence < before_sequence]
        rows.sort(key=lambda m: m.sequence, reverse=True)
        return rows[: limit + 1]

    async def unread_count(self, db, *, conversation_id, staff_member_id, after_sequence):
        return len(
            [
                m
                for m in self.messages.values()
                if m.conversation_id == conversation_id
                and m.sequence > after_sequence
                and m.sender_staff_id != staff_member_id
            ]
        )

    async def list_conversations_for_staff(self, db, *, organization_id, staff_member_id):
        rows = []
        for conversation in self.conversations.values():
            if conversation.organization_id != organization_id:
                continue
            membership = await self.get_membership(
                db, conversation_id=conversation.id, staff_member_id=staff_member_id
            )
            if membership is not None:
                rows.append((conversation, membership, None))
        return rows

    async def last_messages(self, db, conversation_ids):
        result = {}
        for message in self.messages.values():
            if message.conversation_id in conversation_ids:
                current = result.get(message.conversation_id)
                if current is None or message.sequence > current[1]:
                    result[message.conversation_id] = (
                        message.body,
                        message.created_at,
                        message.sequence,
                    )
        return {cid: (body, created_at) for cid, (body, created_at, _s) in result.items()}


class FakeDb:
    def __init__(self, repository):
        self.repository = repository

    async def commit(self):
        return None

    async def flush(self):
        return None

    def add(self, obj):
        if isinstance(obj, Message):
            self.repository.messages[obj.id] = obj


class FakeDelivery:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, db, **kwargs):
        self.jobs.append(kwargs)
        return SimpleNamespace(), True


class FakeNotifications:
    def __init__(self, recent=None):
        self.recent = recent

    async def find_coalescible(self, db, **kwargs):
        return self.recent


def _chat_service(monkeypatch, *, recent=None, max_group_members=20):
    repository = FakeChatRepository()
    delivery = FakeDelivery()

    async def fake_names(db, staff_ids):
        return {staff_id: f"Staff {str(staff_id)[:4]}" for staff_id in staff_ids}

    async def fake_record(db, **kwargs):
        return SimpleNamespace(id=uuid4())

    async def fake_valid(self, db, *, organization_id, staff_ids):
        return set(staff_ids)

    monkeypatch.setattr(chat_service, "staff_display_names", fake_names)
    monkeypatch.setattr(chat_service, "record_notification", fake_record)
    monkeypatch.setattr(ChatService, "_valid_staff_ids", fake_valid)
    service = ChatService(
        repository=repository,
        notifications=FakeNotifications(recent),
        delivery=delivery,
        max_group_members=max_group_members,
    )
    return service, repository, delivery


def _seed_direct(service, repository, context, other_staff_id):
    conversation, _created = run(
        service.repository.create_conversation(
            None,
            organization_id=context.organization_id,
            kind=CONVERSATION_DIRECT,
            direct_key=_direct_key(context.staff_member_id, other_staff_id),
            facility_id=None,
            title=None,
            created_by_staff_id=context.staff_member_id,
        )
    )
    run(
        service.repository.add_member(
            None,
            conversation_id=conversation.id,
            organization_id=context.organization_id,
            staff_member_id=context.staff_member_id,
        )
    )
    run(
        service.repository.add_member(
            None,
            conversation_id=conversation.id,
            organization_id=context.organization_id,
            staff_member_id=other_staff_id,
        )
    )
    return conversation


# ------------------------------------------------------------- chat service


def test_send_message_is_idempotent_and_conflicts_on_reuse(monkeypatch):
    service, repository, delivery = _chat_service(monkeypatch)
    context = _context()
    other = uuid4()
    conversation = _seed_direct(service, repository, context, other)
    db = FakeDb(repository)

    first = run(
        service.send_message(
            db, context, conversation.id, body="hello", client_message_id="c1"
        )
    )
    assert first["sequence"] == 1
    assert first["delivery"] == "sent"
    assert first["mine"] is True
    assert delivery.jobs and delivery.jobs[0]["payload"]["kind"] == "conversation"

    # Same request replays the same message (no duplicate).
    second = run(
        service.send_message(
            db, context, conversation.id, body="hello", client_message_id="c1"
        )
    )
    assert second["id"] == first["id"]
    assert len(repository.messages) == 1

    # Key reuse with different content is a conflict.
    with pytest.raises(HTTPException) as conflict:
        run(
            service.send_message(
                db, context, conversation.id, body="different", client_message_id="c1"
            )
        )
    assert conflict.value.status_code == 409


def test_send_persists_sender_from_auth_and_xss_as_text(monkeypatch):
    service, repository, _delivery = _chat_service(monkeypatch)
    context = _context()
    conversation = _seed_direct(service, repository, context, uuid4())
    item = run(
        service.send_message(
            FakeDb(repository),
            context,
            conversation.id,
            body="<img src=x onerror=alert(1)>",
            client_message_id=None,
        )
    )
    assert item["senderId"] == str(context.staff_member_id)
    assert item["body"] == "<img src=x onerror=alert(1)>"


def test_direct_conversation_dedupes(monkeypatch):
    service, repository, _delivery = _chat_service(monkeypatch)
    context = _context()
    other = uuid4()
    first = run(
        service.create_conversation(
            FakeDb(repository),
            context,
            {"kind": "direct", "member_uuids": [other], "title": None, "facility_uuid": None},
        )
    )
    second = run(
        service.create_conversation(
            FakeDb(repository),
            context,
            {"kind": "direct", "member_uuids": [other], "title": None, "facility_uuid": None},
        )
    )
    assert first["id"] == second["id"]
    assert len(repository.conversations) == 1


def test_team_group_size_is_bounded(monkeypatch):
    service, repository, _delivery = _chat_service(monkeypatch, max_group_members=2)
    context = _context()
    with pytest.raises(HTTPException) as too_big:
        run(
            service.create_conversation(
                FakeDb(repository),
                context,
                {
                    "kind": "team",
                    "member_uuids": [uuid4(), uuid4()],
                    "title": "Ward",
                    "facility_uuid": None,
                },
            )
        )
    assert too_big.value.status_code == 422


def test_read_cursor_is_monotonic(monkeypatch):
    service, repository, _delivery = _chat_service(monkeypatch)
    context = _context()
    conversation = _seed_direct(service, repository, context, uuid4())
    db = FakeDb(repository)
    first = run(
        service.send_message(db, context, conversation.id, body="one", client_message_id=None)
    )
    second = run(
        service.send_message(db, context, conversation.id, body="two", client_message_id=None)
    )
    read = run(service.mark_read(db, context, conversation.id, UUID(second["id"])))
    assert read["readCursor"] == second["id"]

    # A stale/earlier message must not regress the cursor.
    regressed = run(service.mark_read(db, context, conversation.id, UUID(first["id"])))
    assert regressed["readCursor"] == second["id"]


def test_revoked_or_foreign_conversation_is_denied(monkeypatch):
    service, repository, _delivery = _chat_service(monkeypatch)
    context = _context()
    other = uuid4()
    conversation = _seed_direct(service, repository, context, other)

    # A non-member (foreign tenant/staff) is denied.
    foreign = _context(organization_id=context.organization_id)
    with pytest.raises(HTTPException) as denied:
        run(service.get_conversation(FakeDb(repository), foreign, conversation.id))
    assert denied.value.status_code == 403

    # After removal the previous member loses access.
    membership = run(
        repository.get_membership(
            None, conversation_id=conversation.id, staff_member_id=other
        )
    )
    run(repository.remove_member(None, membership))
    removed_context = CommunicationStaffContext(
        account_id=uuid4(),
        organization_id=context.organization_id,
        staff_member_id=other,
        facility_ids=(),
        role_codes=("practitioner",),
    )
    with pytest.raises(HTTPException) as revoked:
        run(service.get_conversation(FakeDb(repository), removed_context, conversation.id))
    assert revoked.value.status_code == 403


def test_offline_catchup_history_paginates(monkeypatch):
    service, repository, _delivery = _chat_service(monkeypatch)
    context = _context()
    conversation = _seed_direct(service, repository, context, uuid4())
    db = FakeDb(repository)
    for index in range(3):
        run(
            service.send_message(
                db, context, conversation.id, body=f"m{index}", client_message_id=None
            )
        )
    page = run(service.list_messages(db, context, conversation.id, before=None, limit=2))
    assert page["has_more"] is True
    assert [m["body"] for m in page["items"]] == ["m1", "m2"]
    older = run(
        service.list_messages(
            db, context, conversation.id, before=int(page["next_cursor"]), limit=2
        )
    )
    assert [m["body"] for m in older["items"]] == ["m0"]


# ------------------------------------------------------------- work status


class FakeWorkStatusRepository(WorkStatusRepository):
    def __init__(self):
        self.rows: dict = {}

    async def get(self, db, *, organization_id, facility_id, staff_member_id):
        return self.rows.get((organization_id, facility_id, staff_member_id))

    async def upsert(
        self, db, *, organization_id, facility_id, staff_member_id, work_state, duty, source, return_at
    ):
        row = self.rows.get((organization_id, facility_id, staff_member_id))
        if row is None:
            row = WorkStatus(
                organization_id=organization_id,
                facility_id=facility_id,
                staff_member_id=staff_member_id,
            )
            self.rows[(organization_id, facility_id, staff_member_id)] = row
        row.work_state = work_state
        row.duty = duty
        row.source = source
        row.return_at = return_at
        row.updated_at = datetime.now(UTC)
        return row


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeWorkDb:
    def __init__(self, facility_id):
        self.facility_id = facility_id

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def scalar(self, statement):
        return "Main Hospital"

    async def execute(self, statement):
        return _Rows([(self.facility_id, "Main Hospital")])


def test_staff_override_is_preserved_over_derived_status():
    facility_id = uuid4()
    repository = FakeWorkStatusRepository()
    service = WorkStatusService(repository)
    context = _context(facility_ids=[facility_id])
    db = FakeWorkDb(facility_id)

    updated = run(
        service.patch_status(
            db,
            context,
            {
                "facility_uuid": facility_id,
                "work_state": "busy",
                "duty": "on-duty",
                "return_at": None,
            },
        )
    )
    assert updated["status"]["workState"] == "busy"
    assert updated["status"]["source"] == WORK_STATUS_SOURCE_STAFF

    # A consultation-derived update must not clobber the live staff override.
    derived = run(
        service.apply_derived_status(
            db,
            organization_id=context.organization_id,
            facility_id=facility_id,
            staff_member_id=context.staff_member_id,
            work_state="with-patient",
        )
    )
    assert derived.work_state == "busy"
    assert derived.source == WORK_STATUS_SOURCE_STAFF

    # An expired override may be replaced by derived state.
    row = repository.rows[(context.organization_id, facility_id, context.staff_member_id)]
    row.return_at = datetime.now(UTC) - timedelta(seconds=1)
    derived = run(
        service.apply_derived_status(
            db,
            organization_id=context.organization_id,
            facility_id=facility_id,
            staff_member_id=context.staff_member_id,
            work_state="available",
        )
    )
    assert derived.source == WORK_STATUS_SOURCE_CONSULTATION
    assert derived.work_state == "available"


def test_patch_status_rejects_foreign_facility():
    repository = FakeWorkStatusRepository()
    service = WorkStatusService(repository)
    context = _context(facility_ids=[uuid4()])
    with pytest.raises(HTTPException) as denied:
        run(
            service.patch_status(
                FakeWorkDb(uuid4()),
                context,
                {"facility_uuid": uuid4(), "work_state": "busy", "duty": None, "return_at": None},
            )
        )
    assert denied.value.status_code == 403


def test_http_chat_routes_registered(client, monkeypatch):
    from src.app.domains.communication.chat import router as chat_router
    from src.app.main import app

    class FakeChat:
        async def list_conversations(self, db, context):
            return {"items": []}

    monkeypatch.setattr(chat_router, "CHAT", FakeChat())
    app.dependency_overrides[get_current_identity_account] = lambda: SimpleNamespace(id=uuid4())
    app.dependency_overrides[get_communication_staff_context] = lambda: _context()
    try:
        listed = client.get("/api/v1/communication/chat/conversations")
        assert listed.status_code == 200
        assert listed.json()["data"]["items"] == []
        paths = app.openapi()["paths"]
        assert "/api/v1/communication/chat/conversations" in paths
        assert "/api/v1/communication/chat/conversations/{conversation_id}/messages" in paths
        assert "/api/v1/communication/status" in paths
    finally:
        app.dependency_overrides.clear()


def test_team_kind_roundtrip_uses_owner_role(monkeypatch):
    service, repository, _delivery = _chat_service(monkeypatch)
    context = _context()
    conversation = run(
        service.create_conversation(
            FakeDb(repository),
            context,
            {"kind": CONVERSATION_TEAM, "member_uuids": [uuid4()], "title": "Ward", "facility_uuid": None},
        )
    )
    assert conversation["kind"] == CONVERSATION_TEAM
    owner_roles = [m["role"] for m in conversation["members"] if m["id"] == str(context.staff_member_id)]
    assert owner_roles == ["owner"]
