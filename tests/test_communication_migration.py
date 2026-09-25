"""Communication migration/import checks.

- Import/metadata check always runs (no DB).
- Offline DDL generation for revision 20260925_35 always runs (no DB).
- The disposable-DB migration and lease-SQL checks run only when a distinct
  ``TEST_POSTGRES_ASYNC_URL`` is configured; no shared/live database is touched.
"""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.app.core.config import settings
from src.app.models.communication import DeliveryJob, RealtimeChannelState

SRC_DIR = Path(__file__).resolve().parents[1] / "src"

REVISION = "20260925_35"
PARENT_REVISION = "20260923_34"


def test_models_register_in_communication_schema():
    assert DeliveryJob.__table__.schema == "communication"
    assert RealtimeChannelState.__table__.schema == "communication"
    assert DeliveryJob.__table__.name == "delivery_job"
    unique = {
        constraint.name for constraint in DeliveryJob.__table__.constraints if constraint.name
    }
    assert "uq_communication_delivery_job_dedup" in unique


def test_offline_migration_sql_contains_foundation_ddl():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic.ini",
            "upgrade",
            f"{PARENT_REVISION}:{REVISION}",
            "--sql",
        ],
        cwd=SRC_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sql = result.stdout
    assert "CREATE SCHEMA IF NOT EXISTS communication;" in sql
    assert "CREATE TABLE communication.delivery_job" in sql
    assert "CREATE TABLE communication.realtime_channel_state" in sql
    assert "uq_communication_delivery_job_dedup" in sql
    assert "uq_communication_realtime_channel_state_principal" in sql


TEST_DB_URL = os.environ.get("TEST_POSTGRES_ASYNC_URL")
requires_disposable_db = pytest.mark.skipif(
    not TEST_DB_URL or TEST_DB_URL == settings.POSTGRES_ASYNC_URL,
    reason="Set a distinct TEST_POSTGRES_ASYNC_URL to run disposable-DB checks.",
)


@requires_disposable_db
def test_disposable_db_migration_and_lease_sql():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.app.domains.communication.delivery.repository import DeliveryJobRepository
    from src.app.models.communication import (
        JOB_STATUS_LEASED,
        JOB_STATUS_PENDING,
    )

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
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = DeliveryJobRepository()
        async with session_factory() as db:
            _, created_first = await repository.enqueue(
                db,
                channel="email",
                event_type="email.test",
                dedup_key="disposable-dup",
                payload={"safe": True},
            )
            await db.commit()
            _, created_second = await repository.enqueue(
                db,
                channel="email",
                event_type="email.test",
                dedup_key="disposable-dup",
                payload={"safe": True},
            )
            await db.commit()
            assert created_first is True and created_second is False

            claimed = await repository.claim_due(
                db, owner="disposable-test", limit=10, lease_seconds=60
            )
            assert len(claimed) == 1
            assert claimed[0].status == JOB_STATUS_LEASED

            future = datetime.now(UTC) + timedelta(seconds=120)
            reclaimed = await repository.reclaim_expired(db, now=future)
            assert len(reclaimed) == 1
            assert reclaimed[0].status == JOB_STATUS_PENDING
        await engine.dispose()

    asyncio.run(_exercise())
