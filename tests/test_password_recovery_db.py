"""Disposable-DB local password-recovery checks (auth-owned).

Runs only with a distinct ``TEST_POSTGRES_ASYNC_URL``. Uses a fake email
transport so no real mail is sent, but exercises the real recovery tables,
verifier, throttling, atomic grant consumption and credential/session
invalidation.
"""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.config import settings
from src.app.core.security import get_password_hash, verify_password
from src.app.domains.auth.recovery import (
    ExpiredRecoveryCode,
    InvalidRecoveryCode,
    InvalidRecoveryGrant,
    RecoveryPolicy,
    RecoveryRateLimited,
    RecoveryService,
)
from src.app.models.identity import (
    AuthSession,
    PasswordRecoveryChallenge,
    UserAccount,
)

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
TEST_DB_URL = os.environ.get("TEST_POSTGRES_ASYNC_URL")
requires_disposable_db = pytest.mark.skipif(
    not TEST_DB_URL or TEST_DB_URL == settings.POSTGRES_ASYNC_URL,
    reason="Set a distinct TEST_POSTGRES_ASYNC_URL to run disposable-DB checks.",
)


class FakeEmail:
    configured = True

    def __init__(self):
        self.codes: list[str] = []

    async def enqueue_template(self, db, **kwargs):
        if kwargs["template_code"] == "password_reset_code":
            self.codes.append(kwargs["context"]["code"])
        return SimpleNamespace(), True


@requires_disposable_db
def test_password_recovery_db_semantics():
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
        email = FakeEmail()
        service = RecoveryService(
            RecoveryPolicy.from_settings(settings), email_service=email
        )

        async with factory() as db:
            account = UserAccount(
                logto_user_id=f"local:{uuid4().hex}",
                email=f"{uuid4().hex}@example.com",
                display_name="Recovery User",
                password_hash=get_password_hash("oldpassword"),
                credentials_version=1,
            )
            db.add(account)
            await db.flush()
            db.add(
                AuthSession(
                    id=f"sess-{uuid4().hex}",
                    user_account_id=account.id,
                    id_token="token",
                    csrf_token="csrf",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await db.commit()
            account_id = account.id
            account_email = account.email

        async with factory() as db:
            unknown = await service.request_reset(
                db, email="nobody@example.com", client_ip="10.0.0.1"
            )
            assert unknown["challenge_id"]
            assert email.codes == []  # unknown email never sends

        async with factory() as db:
            known = await service.request_reset(
                db, email=account_email, client_ip="10.0.0.1"
            )
            assert len(email.codes) == 1
            code = email.codes[0]

        async with factory() as db:
            with pytest.raises(RecoveryRateLimited):
                await service.request_reset(db, email=account_email, client_ip="10.0.0.2")

        challenge_id = UUID(known["challenge_id"])
        async with factory() as db:
            with pytest.raises(InvalidRecoveryCode):
                await service.verify_code(db, challenge_id=challenge_id, code="000000")
        async with factory() as db:
            grant = await service.verify_code(db, challenge_id=challenge_id, code=code)
        assert grant["grant"]

        async def attempt(token: str):
            async with factory() as db:
                try:
                    return await service.reset_password(db, grant=token, password="newpassword")
                except InvalidRecoveryGrant:
                    return None

        results = await asyncio.gather(attempt(grant["grant"]), attempt(grant["grant"]))
        assert sum(1 for result in results if result is not None) == 1

        async with factory() as db:
            refreshed = await db.get(UserAccount, account_id)
            assert refreshed is not None
            assert refreshed.credentials_version >= 2
            assert await verify_password("newpassword", refreshed.password_hash) is True
            assert await verify_password("oldpassword", refreshed.password_hash) is False
            remaining = await db.scalar(
                select(AuthSession).where(AuthSession.user_account_id == account_id)
            )
            assert remaining is None

        async with factory() as db:
            with pytest.raises(InvalidRecoveryGrant):
                await service.reset_password(
                    db, grant=grant["grant"], password="anotherpassword"
                )

        async with factory() as db:
            expired = await service.request_reset(
                db, email=account_email, client_ip="10.0.0.3"
            )
            challenge = await db.get(
                PasswordRecoveryChallenge, UUID(expired["challenge_id"])
            )
            challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
            with pytest.raises(ExpiredRecoveryCode):
                await service.verify_code(
                    db,
                    challenge_id=UUID(expired["challenge_id"]),
                    code=email.codes[-1],
                )

        await engine.dispose()

    asyncio.run(_exercise())
