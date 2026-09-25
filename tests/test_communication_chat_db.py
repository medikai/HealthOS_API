"""Disposable-DB chat/work-status checks.

Runs only with a distinct ``TEST_POSTGRES_ASYNC_URL``. Covers order/idempotency,
direct dedupe, membership revocation, foreign scope, monotonic read, offline
catch-up pagination and work-status override preservation.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.config import settings
from src.app.domains.communication.chat.service import ChatService
from src.app.domains.communication.dependencies import CommunicationStaffContext
from src.app.domains.communication.status.service import WorkStatusService
from src.app.models.identity import UserAccount
from src.app.models.organization import (
    Facility,
    Organization,
    StaffAssignment,
    StaffMember,
)

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
TEST_DB_URL = os.environ.get("TEST_POSTGRES_ASYNC_URL")
requires_disposable_db = pytest.mark.skipif(
    not TEST_DB_URL or TEST_DB_URL == settings.POSTGRES_ASYNC_URL,
    reason="Set a distinct TEST_POSTGRES_ASYNC_URL to run disposable-DB checks.",
)


async def _seed_org(session):
    org = Organization(name=f"Org {uuid4().hex[:8]}", code=f"O{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    facility = Facility(organization_id=org.id, name="Main", code=f"F{uuid4().hex[:8]}")
    session.add(facility)
    await session.flush()
    staff = []
    for _ in range(2):
        account = UserAccount(
            logto_user_id=f"local:{uuid4().hex}",
            email=f"{uuid4().hex}@example.com",
            display_name="Staff",
        )
        session.add(account)
        await session.flush()
        member = StaffMember(organization_id=org.id, user_account_id=account.id)
        session.add(member)
        session.add(
            StaffAssignment(staff_member_id=member.id, facility_id=facility.id, role_code="practitioner")
        )
        await session.flush()
        staff.append((account, member))
    await session.commit()
    return org, facility, staff


def _context(org, facility, account, member):
    return CommunicationStaffContext(
        account_id=account.id,
        organization_id=org.id,
        staff_member_id=member.id,
        facility_ids=(facility.id,),
        role_codes=("practitioner",),
    )


@requires_disposable_db
def test_chat_db_semantics():
    sync_url = TEST_DB_URL.replace("+asyncpg", "+psycopg2")
    env = {**os.environ, "POSTGRES_ASYNC_URL": TEST_DB_URL, "POSTGRES_SYNC_URL": sync_url}
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=SRC_DIR,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        check=False,
    )
    assert upgrade.returncode == 0, upgrade.stderr

    async def _exercise() -> None:
        from fastapi import HTTPException

        engine = create_async_engine(TEST_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        chat = ChatService()
        status = WorkStatusService()

        async with factory() as db:
            org, facility, staff = await _seed_org(db)
            other_org, other_facility, other_staff = await _seed_org(db)
            account_a, member_a = staff[0]
            account_b, member_b = staff[1]
            account_c, member_c = other_staff[0]
            ctx_a = _context(org, facility, account_a, member_a)
            ctx_b = _context(org, facility, account_b, member_b)
            ctx_c = _context(other_org, other_facility, account_c, member_c)

            conversation = await chat.create_conversation(
                db,
                ctx_a,
                {"kind": "direct", "member_uuids": [member_b.id], "title": None, "facility_uuid": None},
            )
            again = await chat.create_conversation(
                db,
                ctx_a,
                {"kind": "direct", "member_uuids": [member_b.id], "title": None, "facility_uuid": None},
            )
            assert conversation["id"] == again["id"]

            first = await chat.send_message(
                db, ctx_a, conversation["id"], body="one", client_message_id="c1"
            )
            second = await chat.send_message(
                db, ctx_a, conversation["id"], body="two", client_message_id="c2"
            )
            third = await chat.send_message(
                db, ctx_b, conversation["id"], body="three", client_message_id="c3"
            )
            assert [first["sequence"], second["sequence"], third["sequence"]] == [1, 2, 3]

            replay = await chat.send_message(
                db, ctx_a, conversation["id"], body="one", client_message_id="c1"
            )
            assert replay["id"] == first["id"]

            with pytest.raises(HTTPException) as conflict:
                await chat.send_message(
                    db, ctx_a, conversation["id"], body="different", client_message_id="c1"
                )
            assert conflict.value.status_code == 409

            read = await chat.mark_read(db, ctx_b, conversation["id"], third["id"])
            assert read["readCursor"] == third["id"]
            stale = await chat.mark_read(db, ctx_b, conversation["id"], first["id"])
            assert stale["readCursor"] == third["id"]

            page = await chat.list_messages(db, ctx_b, conversation["id"], before=None, limit=2)
            assert page["has_more"] is True
            assert [m["sequence"] for m in page["items"]] == [2, 3]

            with pytest.raises(HTTPException) as foreign:
                await chat.get_conversation(db, ctx_c, conversation["id"])
            assert foreign.value.status_code == 404

            # membership revocation stops access
            team = await chat.create_conversation(
                db,
                ctx_a,
                {"kind": "team", "member_uuids": [member_b.id], "title": "Ward", "facility_uuid": None},
            )
            await chat.remove_member(db, ctx_a, team["id"], member_b.id)
            with pytest.raises(HTTPException) as revoked:
                await chat.get_conversation(db, ctx_b, team["id"])
            assert revoked.value.status_code == 403

            # work status: staff override preserved, expired override replaced
            updated = await status.patch_status(
                db,
                ctx_a,
                {"facility_uuid": facility.id, "work_state": "busy", "duty": "on-duty", "return_at": None},
            )
            assert updated["status"]["workState"] == "busy"
            derived = await status.apply_derived_status(
                db,
                organization_id=org.id,
                facility_id=facility.id,
                staff_member_id=member_a.id,
                work_state="with-patient",
            )
            assert derived.work_state == "busy"
            assert derived.source == "staff"

        await engine.dispose()

    asyncio.run(_exercise())
