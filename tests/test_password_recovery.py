"""Local password-recovery unit tests (no DB, real hashing/verifier).

Covers neutral known/unknown shape, generation-bound verifier, attempt
fail-closed, expiry/consumption, single-use grant and credential/session
invalidation. Real-auth DB integration lives in ``test_password_recovery_db.py``.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from uuid6 import uuid7

from src.app.core.security import get_password_hash, verify_password
from src.app.domains.auth.recovery import (
    ExpiredRecoveryCode,
    InvalidRecoveryCode,
    InvalidRecoveryGrant,
    RecoveryPolicy,
    RecoveryRateLimited,
    RecoveryService,
    generate_numeric_code,
    mask_email,
)
from src.app.domains.communication.shared.errors import ProviderUnavailable


def run(coro):
    return asyncio.run(coro)


def _policy(**overrides) -> RecoveryPolicy:
    values = {
        "pepper": "test-pepper",
        "code_ttl_seconds": 600,
        "resend_cooldown_seconds": 60,
        "max_attempts": 5,
        "account_limit": 5,
        "ip_limit": 20,
        "throttle_window_seconds": 3600,
        "grant_ttl_seconds": 600,
        "code_length": 6,
    }
    values.update(overrides)
    return RecoveryPolicy(**values)


class FakeDb:
    def __init__(self, account=None):
        self.account = account
        self.added = []

    async def get(self, model, pk):
        return self.account

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class FakeEmail:
    def __init__(self, configured=True):
        self.configured = configured
        self.calls = []

    async def enqueue_template(self, db, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(), True


class FakeRecoveryRepository:
    def __init__(
        self,
        *,
        account=None,
        challenge=None,
        grant=None,
        throttle_count=1,
        consume_challenge_ok=True,
        consume_grant_ok=True,
    ):
        self.account = account
        self.challenge = challenge
        self.grant = grant
        self.throttle_count = throttle_count
        self.consume_challenge_ok = consume_challenge_ok
        self.consume_grant_ok = consume_grant_ok
        self.created_challenges = []
        self.grants = []
        self.invalidated = 0
        self.sessions_deleted = 0

    async def find_account_by_email(self, db, email):
        return self.account

    async def latest_challenge(self, db, user_account_id):
        return None

    async def invalidate_active_challenges(self, db, user_account_id, now):
        self.invalidated += 1

    async def next_generation(self, db, user_account_id):
        return 0

    async def create_challenge(self, db, **values):
        challenge = SimpleNamespace(**values)
        self.created_challenges.append(challenge)
        self.challenge = challenge
        return challenge

    async def get_challenge(self, db, challenge_id):
        if self.challenge is not None and self.challenge.id == challenge_id:
            return self.challenge
        return None

    async def consume_challenge_atomic(self, db, challenge_id, now):
        return self.consume_challenge_ok

    async def create_grant(self, db, **values):
        grant = SimpleNamespace(**values)
        self.grants.append(grant)
        return grant

    async def consume_grant_atomic(self, db, token_hash, now):
        return self.grant if self.consume_grant_ok else None

    async def delete_account_sessions(self, db, user_account_id):
        self.sessions_deleted += 1

    async def hit_throttle(self, db, *, scope, key_hash, now, window_seconds):
        return self.throttle_count, 42


def _account(**overrides):
    base = {
        "id": uuid4(),
        "email": "user@example.com",
        "display_name": "Dr Example",
        "is_active": True,
        "credentials_version": 1,
        "password_hash": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _challenge(policy, user_account_id, code="123456", **overrides):
    challenge_id = uuid7()
    base = {
        "id": challenge_id,
        "user_account_id": user_account_id,
        "code_verifier": policy.code_verifier(challenge_id, user_account_id, code),
        "consumed_at": None,
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        "attempts": 0,
        "max_attempts": 5,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_recovery_routes_registered():
    from src.app.main import app

    paths = app.openapi()["paths"]
    assert "/api/v1/auth/local/password/forgot" in paths
    assert "/api/v1/auth/local/password/verify" in paths
    assert "/api/v1/auth/local/password/reset" in paths


def test_generate_code_and_mask_email():
    code = generate_numeric_code(6)
    assert len(code) == 6 and code.isdigit()
    assert mask_email("user@example.com") == "u***@example.com"


def test_code_verifier_is_bound_to_challenge_account_and_code():
    policy = _policy()
    account_id = uuid4()
    challenge_a, challenge_b = uuid7(), uuid7()
    assert policy.code_verifier(challenge_a, account_id, "123456") == policy.code_verifier(
        challenge_a, account_id, "123456"
    )
    assert policy.code_verifier(challenge_a, account_id, "123456") != policy.code_verifier(
        challenge_b, account_id, "123456"
    )
    assert policy.code_verifier(challenge_a, account_id, "123456") != policy.code_verifier(
        challenge_a, uuid4(), "123456"
    )
    assert "123456" not in policy.code_verifier(challenge_a, account_id, "123456")


def test_request_unknown_email_is_neutral_and_stores_nothing():
    repository = FakeRecoveryRepository(account=None)
    email = FakeEmail()
    service = RecoveryService(_policy(), repository=repository, email_service=email)
    result = run(service.request_reset(FakeDb(), email="nobody@example.com", client_ip="1.2.3.4"))
    assert result["masked_email"] == "n***@example.com"
    assert result["code_length"] == 6
    assert repository.created_challenges == []
    assert email.calls == []
    # verifying the neutral challenge never succeeds
    with pytest.raises(ExpiredRecoveryCode):
        run(service.verify_code(FakeDb(), challenge_id=uuid7(), code="123456"))


def test_request_known_account_sends_secret_code_not_plaintext():
    account = _account()
    repository = FakeRecoveryRepository(account=account)
    email = FakeEmail()
    service = RecoveryService(_policy(), repository=repository, email_service=email)
    result = run(service.request_reset(FakeDb(), email="user@example.com", client_ip="1.2.3.4"))
    assert len(repository.created_challenges) == 1
    challenge = repository.created_challenges[0]
    assert challenge.generation == 1
    assert challenge.email_hash != "user@example.com"
    assert email.calls and email.calls[0]["template_code"] == "password_reset_code"
    code = email.calls[0]["context"]["code"]
    assert len(code) == 6
    assert challenge.code_verifier != code and code not in challenge.code_verifier
    assert result["challenge_id"] == str(challenge.id)


def test_request_is_rate_limited_by_aggregate():
    repository = FakeRecoveryRepository(account=None, throttle_count=999)
    service = RecoveryService(_policy(), repository=repository, email_service=FakeEmail())
    with pytest.raises(RecoveryRateLimited) as limited:
        run(service.request_reset(FakeDb(), email="user@example.com", client_ip="1.2.3.4"))
    assert limited.value.retry_after_seconds >= 1


def test_request_fails_closed_when_email_unconfigured():
    service = RecoveryService(
        _policy(),
        repository=FakeRecoveryRepository(account=_account()),
        email_service=FakeEmail(configured=False),
    )
    with pytest.raises(ProviderUnavailable):
        run(service.request_reset(FakeDb(), email="user@example.com", client_ip=None))


def test_verify_wrong_code_increments_attempts_and_fails_closed():
    policy = _policy(max_attempts=2)
    account_id = uuid4()
    challenge = _challenge(policy, account_id, attempts=0, max_attempts=2)
    repository = FakeRecoveryRepository(challenge=challenge)
    service = RecoveryService(policy, repository=repository, email_service=FakeEmail())

    with pytest.raises(InvalidRecoveryCode) as first:
        run(service.verify_code(FakeDb(), challenge_id=challenge.id, code="000000"))
    assert challenge.attempts == 1
    assert first.value.attempts_remaining == 1
    with pytest.raises(InvalidRecoveryCode) as second:
        run(service.verify_code(FakeDb(), challenge_id=challenge.id, code="000000"))
    assert challenge.attempts == 2
    assert second.value.attempts_remaining == 0
    assert challenge.consumed_at is not None  # fail closed


def test_verify_expired_or_consumed_challenge():
    policy = _policy()
    challenge = _challenge(
        policy, uuid4(), expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    service = RecoveryService(
        policy, repository=FakeRecoveryRepository(challenge=challenge), email_service=FakeEmail()
    )
    with pytest.raises(ExpiredRecoveryCode):
        run(service.verify_code(FakeDb(), challenge_id=challenge.id, code="123456"))


def test_verify_success_consumes_challenge_and_issues_single_use_grant():
    policy = _policy()
    account_id = uuid4()
    challenge = _challenge(policy, account_id)
    repository = FakeRecoveryRepository(challenge=challenge, consume_challenge_ok=True)
    service = RecoveryService(policy, repository=repository, email_service=FakeEmail())

    result = run(service.verify_code(FakeDb(), challenge_id=challenge.id, code="123456"))
    assert result["grant"]
    assert len(repository.grants) == 1
    assert repository.grants[0].token_hash != result["grant"]

    # Challenge is now consumed; a replay is refused.
    challenge.consumed_at = datetime.now(UTC)
    repository.consume_challenge_ok = False
    with pytest.raises(ExpiredRecoveryCode):
        run(service.verify_code(FakeDb(), challenge_id=challenge.id, code="123456"))


def test_reset_updates_password_and_revokes_previous_tokens():
    policy = _policy()
    old_hash = get_password_hash("oldpassword")
    account = _account(password_hash=old_hash)
    grant = SimpleNamespace(id=uuid4(), user_account_id=account.id)
    repository = FakeRecoveryRepository(grant=grant, consume_grant_ok=True)
    email = FakeEmail()
    service = RecoveryService(policy, repository=repository, email_service=email)

    result = run(
        service.reset_password(FakeDb(account=account), grant="grant-token", password="newpassword")
    )
    assert result["sessions_revoked"] is True
    assert account.credentials_version == 2
    assert repository.sessions_deleted == 1
    assert run(verify_password("newpassword", account.password_hash)) is True
    assert run(verify_password("oldpassword", account.password_hash)) is False
    assert any(call["template_code"] == "password_changed" for call in email.calls)


def test_reset_rejects_invalid_or_reused_grant():
    service = RecoveryService(
        _policy(),
        repository=FakeRecoveryRepository(grant=None, consume_grant_ok=False),
        email_service=FakeEmail(),
    )
    with pytest.raises(InvalidRecoveryGrant):
        run(service.reset_password(FakeDb(), grant="bad", password="newpassword"))
