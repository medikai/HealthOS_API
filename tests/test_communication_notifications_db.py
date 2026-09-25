"""Disposable-DB notification checks (rollback, dedupe, cross-tenant, replay).

Runs only when a distinct ``TEST_POSTGRES_ASYNC_URL`` is configured. Never
touches the shared/live database.
"""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.config import settings
from src.app.domains.communication.delivery.repository import DeliveryJobRepository
from src.app.domains.communication.notifications.constants import EVENT_QUEUE_READY
from src.app.domains.communication.notifications.repository import (
    NotificationRepository,
)
from src.app.domains.communication.notifications.workflow import record_notification
from src.app.models.communication import DeliveryJob, NotificationEvent
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


async def _seed(session):
    org = Organization(name=f"Org {uuid4().hex[:8]}", code=f"O{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    facility = Facility(organization_id=org.id, name="Main", code=f"F{uuid4().hex[:8]}")
    session.add(facility)
    await session.flush()
    account = UserAccount(
        logto_user_id=f"local:{uuid4().hex}",
        email=f"{uuid4().hex}@example.com",
        display_name="Nurse",
    )
    session.add(account)
    await session.flush()
    staff = StaffMember(organization_id=org.id, user_account_id=account.id)
    session.add(staff)
    await session.flush()
    session.add(
        StaffAssignment(staff_member_id=staff.id, facility_id=facility.id, role_code="receptionist")
    )
    await session.commit()
    return org, facility, staff


@requires_disposable_db
def test_notification_db_semantics():
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
        engine = create_async_engine(TEST_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = NotificationRepository()
        delivery = DeliveryJobRepository()

        async with factory() as db:
            org, facility, staff = await _seed(db)
            _, other_facility, other_staff = await _seed(db)
            since = datetime.now(UTC) - timedelta(minutes=1)

            # rollback-no-alert: nothing persists on rollback.
            await record_notification(
                db,
                event_type=EVENT_QUEUE_READY,
                organization_id=org.id,
                facility_id=facility.id,
                actor_user_id=None,
                recipient_staff_ids=[staff.id],
                title="rollback",
                dedup_key=f"rollback-{uuid4()}",
            )
            await db.rollback()
            rolled_back = await db.scalar(
                select(func.count()).select_from(NotificationEvent).where(
                    NotificationEvent.organization_id == org.id
                )
            )
            assert int(rolled_back or 0) == 0

            # committed notification is visible to its recipient only.
            event = await record_notification(
                db,
                event_type=EVENT_QUEUE_READY,
                organization_id=org.id,
                facility_id=facility.id,
                actor_user_id=None,
                recipient_staff_ids=[staff.id],
                title="ready",
                task_state="awaiting",
                dedup_key=f"ready-{uuid4()}",
            )
            await db.commit()
            assert event is not None

            rows = await repository.list_for_staff(
                db,
                organization_id=org.id,
                staff_member_id=staff.id,
                limit=10,
                cursor=None,
                tab="all",
                priority=None,
                facility_id=None,
                query=None,
            )
            assert [row[0].id for row in rows] == [event.id]

            foreign = await repository.list_for_staff(
                db,
                organization_id=org.id,
                staff_member_id=other_staff.id,
                limit=10,
                cursor=None,
                tab="all",
                priority=None,
                facility_id=None,
                query=None,
            )
            assert foreign == []

            # read is independent of task state.
            assert await repository.mark_read(
                db, staff_member_id=staff.id, notification_id=event.id
            )
            refreshed = await repository.get_for_staff(
                db, staff_member_id=staff.id, notification_id=event.id
            )
            assert refreshed is not None
            _refreshed_event, recipient, _name = refreshed
            assert recipient.read_at is not None
            assert _refreshed_event.task_state == "awaiting"

            # retry dedupe: same delivery key inserts once.
            for _ in range(2):
                await delivery.enqueue(
                    db,
                    channel="realtime",
                    event_type=EVENT_QUEUE_READY,
                    dedup_key=f"dedupe-{event.id}",
                    payload={"notification_id": str(event.id)},
                )
                await db.commit()
            jobs = await db.scalar(
                select(func.count()).select_from(DeliveryJob).where(
                    DeliveryJob.dedup_key == f"dedupe-{event.id}"
                )
            )
            assert int(jobs or 0) == 1

            # replay under concurrent commits: both rows appear after `since`.
            concurrent = await factory()
            try:
                second = await record_notification(
                    concurrent,
                    event_type=EVENT_QUEUE_READY,
                    organization_id=org.id,
                    facility_id=facility.id,
                    actor_user_id=None,
                    recipient_staff_ids=[staff.id],
                    title="second",
                    dedup_key=f"second-{uuid4()}",
                )
                await concurrent.commit()
            finally:
                await concurrent.close()
            changes = await repository.changes_since(
                db,
                organization_id=org.id,
                staff_member_id=staff.id,
                since=since,
                limit=50,
            )
            assert {row[0].id for row in changes} >= {event.id, second.id}

            counts = await repository.counts(
                db,
                staff_member_id=staff.id,
                organization_id=org.id,
            )
            assert counts["needsAction"] >= 1
            assert other_facility.id != facility.id

        await engine.dispose()

    asyncio.run(_exercise())
