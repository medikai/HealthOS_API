"""Disposable-DB push device binding checks.

Runs only with a distinct ``TEST_POSTGRES_ASYNC_URL``. Covers registration
ownership, account-switch rebinding, stale/revoked devices and duplicate job
dedupe. No real FCM call is made.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.config import settings
from src.app.domains.communication.delivery.repository import DeliveryJobRepository
from src.app.domains.communication.dependencies import CommunicationStaffContext
from src.app.domains.communication.push.service import PushService
from src.app.models.communication import DeliveryJob
from src.app.models.identity import UserAccount
from src.app.models.organization import Facility, Organization, StaffMember

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
    members = []
    for _ in range(2):
        account = UserAccount(
            logto_user_id=f"local:{uuid4().hex}",
            email=f"{uuid4().hex}@example.com",
            display_name="Push User",
        )
        session.add(account)
        await session.flush()
        member = StaffMember(organization_id=org.id, user_account_id=account.id)
        session.add(member)
        await session.flush()
        members.append((account, member))
    await session.commit()
    return org, facility, members


def _context(org, account, member):
    return CommunicationStaffContext(
        account_id=account.id,
        organization_id=org.id,
        staff_member_id=member.id,
        facility_ids=(),
        role_codes=("practitioner",),
    )


@requires_disposable_db
def test_push_device_db_semantics():
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
        service = PushService(enabled=True, provider_configured=True)

        async with factory() as db:
            org, _facility, members = await _seed(db)
            (account_a, member_a), (account_b, member_b) = members
            ctx_a = _context(org, account_a, member_a)
            ctx_b = _context(org, account_b, member_b)

            first = await service.register_device(
                db, ctx_a, {"token": "device-token-1", "installation_id": "install-a"}
            )
            assert first["active"] is True

            # account switch on the same physical device token
            second = await service.register_device(
                db, ctx_b, {"token": "device-token-1", "installation_id": "install-b"}
            )
            assert second["active"] is True
            devices_a = await service.list_devices(db, ctx_a)
            assert len(devices_a) == 1 and devices_a[0]["active"] is False
            devices_b = await service.list_devices(db, ctx_b)
            assert len(devices_b) == 1 and devices_b[0]["active"] is True

            # foreign revoke denied
            from uuid import UUID

            with pytest.raises(HTTPException) as denied:
                await service.revoke_device(db, ctx_a, UUID(second["id"]))
            assert denied.value.status_code == 404

            # duplicate push jobs dedupe
            delivery = DeliveryJobRepository()
            for _ in range(2):
                await delivery.enqueue(
                    db,
                    channel="push",
                    event_type="queue.ready",
                    dedup_key="push:dup",
                    payload={"notification_id": str(uuid4())},
                )
                await db.commit()
            count = await db.scalar(
                select(func.count()).select_from(DeliveryJob).where(
                    DeliveryJob.dedup_key == "push:dup"
                )
            )
            assert int(count or 0) == 1

            # logout revokes remaining bindings
            revoked = await service.revoke_account_devices(db, account_b.id, reason="logout")
            assert revoked >= 1
            devices_b = await service.list_devices(db, ctx_b)
            assert all(device["active"] is False for device in devices_b)

        await engine.dispose()

    asyncio.run(_exercise())
