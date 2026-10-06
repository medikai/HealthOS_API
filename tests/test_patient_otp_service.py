"""Unit checks for the patient OTP challenge lifecycle and dev provider gating."""

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from src.app.domains.patient_auth.otp import (
    DevPatientOtpProvider,
    ExpiredOtpCode,
    InvalidOtpCode,
    OtpPolicy,
    OtpProviderUnavailable,
    OtpRateLimited,
    OtpSandboxUnavailable,
    PatientOtpService,
    UnconfiguredPatientOtpProvider,
    generate_numeric_code,
)


class FakeDb:
    def __init__(self, account=None):
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.flush = AsyncMock()
        self.get = AsyncMock(return_value=account)


POLICY = OtpPolicy(
    pepper="test-pepper",
    code_ttl_seconds=600,
    resend_cooldown_seconds=60,
    max_attempts=3,
    phone_limit=100,
    ip_limit=100,
    throttle_window_seconds=3600,
    code_length=6,
)


class FakeProvider:
    name = "fake"
    configured = True

    def __init__(self):
        self.sent = {}

    def allows_sandbox(self, phone):
        return False

    async def send_code(self, *, phone, code, challenge_id, purpose, expires_at):
        self.sent[challenge_id] = (phone, code)

    def exposed_code(self, challenge_id):
        return None


class FakeRepository:
    def __init__(self, account=None):
        self.account = account
        self.challenges = {}
        self.throttles = {}
        self.created = []

    async def find_account_by_phone(self, db, phone):
        return self.account

    async def latest_challenge_for_phone(self, db, phone_hash):
        matches = [
            c for c in self.challenges.values() if c.phone_hash == phone_hash
        ]
        return max(matches, key=lambda c: c.created_at) if matches else None

    async def invalidate_active_challenges(self, db, phone_hash, now):
        for challenge in self.challenges.values():
            if challenge.phone_hash == phone_hash and challenge.consumed_at is None:
                challenge.consumed_at = now

    async def next_generation(self, db, phone_hash):
        values = [
            c.generation for c in self.challenges.values() if c.phone_hash == phone_hash
        ]
        return max(values, default=0)

    async def create_challenge(self, db, **values):
        challenge_id = values.pop("id", None)
        challenge = SimpleNamespace(
            id=challenge_id,
            consumed_at=None,
            created_at=datetime.now(UTC),
            **values,
        )
        self.challenges[challenge.id] = challenge
        self.created.append(challenge)
        return challenge

    async def get_challenge(self, db, challenge_id):
        return self.challenges.get(challenge_id)

    async def consume_challenge_atomic(self, db, challenge_id, now):
        challenge = self.challenges.get(challenge_id)
        if (
            challenge is None
            or challenge.consumed_at is not None
            or challenge.expires_at <= now
        ):
            return False
        challenge.consumed_at = now
        return True

    async def purge_consumed_challenges(self, db, phone_hash):
        for key in [
            key
            for key, c in self.challenges.items()
            if c.phone_hash == phone_hash and c.consumed_at is not None
        ]:
            del self.challenges[key]

    async def hit_throttle(self, db, *, scope, key_hash, now, window_seconds):
        key = (scope, key_hash)
        row = self.throttles.get(key)
        if row is None:
            row = SimpleNamespace(window_start=now, count=0)
            self.throttles[key] = row
        elif (now - row.window_start).total_seconds() >= window_seconds:
            row.window_start = now
            row.count = 0
        row.count += 1
        elapsed = int((now - row.window_start).total_seconds())
        return row.count, max(1, window_seconds - elapsed)


def _account(phone):
    return SimpleNamespace(id=uuid4(), phone=phone, is_active=True)


class PatientOtpServiceTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, account=None, provider=None):
        self.db = FakeDb(account)
        repo = FakeRepository(account=account)
        service = PatientOtpService(POLICY, provider=provider or FakeProvider(), repository=repo)
        return service, repo

    async def test_request_and_verify_success(self):
        account = _account("919876543210")
        provider = FakeProvider()
        service, repo = self._service(account=account, provider=provider)

        response = await service.request_code(
            self.db, phone="9876543210", client_ip="10.0.0.1"
        )
        challenge_id = response["challenge_id"]
        self.assertIn("resend_available_at", response)
        phone, code = provider.sent[next(iter(provider.sent))]
        self.assertEqual(phone, "919876543210")

        from uuid import UUID

        _, verified = await service.verify_code(
            self.db, challenge_id=UUID(challenge_id), phone="9876543210", code=code
        )
        self.assertEqual(verified.id, account.id)
        self.assertIsNotNone(repo.challenges[UUID(challenge_id)].consumed_at)

    async def test_replay_is_rejected(self):
        account = _account("919876543211")
        provider = FakeProvider()
        service, _ = self._service(account=account, provider=provider)
        response = await service.request_code(self.db, phone="9876543211", client_ip=None)
        from uuid import UUID

        challenge_id = UUID(response["challenge_id"])
        code = next(iter(provider.sent.values()))[1]
        await service.verify_code(
            self.db, challenge_id=challenge_id, phone="9876543211", code=code
        )
        with self.assertRaises(ExpiredOtpCode):
            await service.verify_code(
                self.db, challenge_id=challenge_id, phone="9876543211", code=code
            )

    async def test_wrong_code_attempt_limit_and_fail_closed(self):
        account = _account("919876543212")
        provider = FakeProvider()
        service, repo = self._service(account=account, provider=provider)
        response = await service.request_code(self.db, phone="9876543212", client_ip=None)
        from uuid import UUID

        challenge_id = UUID(response["challenge_id"])
        for expected_remaining in (2, 1, 0):
            with self.assertRaises(InvalidOtpCode) as error:
                await service.verify_code(
                    self.db, challenge_id=challenge_id, phone="9876543212", code="000000"
                )
            self.assertEqual(error.exception.attempts_remaining, expected_remaining)
        self.assertIsNotNone(repo.challenges[challenge_id].consumed_at)
        with self.assertRaises(ExpiredOtpCode):
            await service.verify_code(
                self.db, challenge_id=challenge_id, phone="9876543212", code="000000"
            )

    async def test_expired_challenge_is_rejected(self):
        account = _account("919876543213")
        provider = FakeProvider()
        service, repo = self._service(account=account, provider=provider)
        response = await service.request_code(self.db, phone="9876543213", client_ip=None)
        from uuid import UUID

        challenge_id = UUID(response["challenge_id"])
        repo.challenges[challenge_id].expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with self.assertRaises(ExpiredOtpCode):
            await service.verify_code(
                self.db, challenge_id=challenge_id, phone="9876543213", code="123456"
            )

    async def test_resend_cooldown_is_enforced(self):
        account = _account("919876543214")
        provider = FakeProvider()
        service, _ = self._service(account=account, provider=provider)
        await service.request_code(self.db, phone="9876543214", client_ip=None)
        with self.assertRaises(OtpRateLimited):
            await service.request_code(self.db, phone="9876543214", client_ip=None)

    async def test_resend_issues_new_challenge_after_cooldown(self):
        account = _account("919876543215")
        provider = FakeProvider()
        service, repo = self._service(account=account, provider=provider)
        first = await service.request_code(self.db, phone="9876543215", client_ip=None)
        from uuid import UUID

        first_id = UUID(first["challenge_id"])
        repo.challenges[first_id].created_at = datetime.now(UTC) - timedelta(
            seconds=POLICY.resend_cooldown_seconds + 1
        )
        second = await service.resend_code(self.db, challenge_id=first_id, client_ip=None)
        second_id = UUID(second["challenge_id"])
        self.assertNotEqual(first_id, second_id)
        self.assertIsNotNone(repo.challenges[first_id].consumed_at)
        code = provider.sent[second_id][1]
        _, verified = await service.verify_code(
            self.db, challenge_id=second_id, phone="9876543215", code=code
        )
        self.assertEqual(verified.id, account.id)

    async def test_new_phone_gets_a_code_and_can_register(self):
        provider = FakeProvider()
        service, repo = self._service(account=None, provider=provider)
        response = await service.request_code(self.db, phone="987650000001", client_ip=None)
        self.assertTrue(response["challenge_id"])
        self.assertEqual(len(provider.sent), 1)
        from uuid import UUID

        challenge_id = UUID(response["challenge_id"])
        _, code = provider.sent[challenge_id]
        with self.assertRaises(InvalidOtpCode):
            await service.verify_code(
                self.db, challenge_id=challenge_id, phone="987650000001", code="000000"
            )
        _, verified = await service.verify_code(
            self.db, challenge_id=challenge_id, phone="987650000001", code=code
        )
        # A verified first-time phone has no account yet; the caller creates it.
        self.assertIsNone(verified)
        self.assertIsNotNone(repo.challenges[challenge_id].consumed_at)

    async def test_unconfigured_provider_fails_closed(self):
        self.db = FakeDb(account=None)
        service = PatientOtpService(
            POLICY,
            provider=UnconfiguredPatientOtpProvider(),
            repository=FakeRepository(account=_account("919876543216")),
        )
        with self.assertRaises(OtpProviderUnavailable):
            await service.request_code(self.db, phone="9876543216", client_ip=None)

    async def test_phone_mismatch_cannot_use_someone_elses_challenge(self):
        account = _account("919876543217")
        provider = FakeProvider()
        service, _ = self._service(account=account, provider=provider)
        response = await service.request_code(self.db, phone="9876543217", client_ip=None)
        from uuid import UUID

        challenge_id = UUID(response["challenge_id"])
        code = provider.sent[challenge_id][1]
        with self.assertRaises(InvalidOtpCode):
            await service.verify_code(
                self.db, challenge_id=challenge_id, phone="987654399999", code=code
            )


class SandboxGatingTests(unittest.IsolatedAsyncioTestCase):
    def _dev_service(self, *, expose_code=True):
        self.db = FakeDb(account=_account("919999900001"))
        repo = FakeRepository(account=_account("919999900001"))
        provider = DevPatientOtpProvider(
            environment="local",
            allowlist=["+919999900001"],
            expose_code=expose_code,
        )
        service = PatientOtpService(POLICY, provider=provider, repository=repo)
        return service, repo

    async def test_sandbox_skips_throttle_and_cooldown_but_strict_still_applies(self):
        from uuid import UUID

        service, repo = self._dev_service()
        first = await service.request_code(
            self.db, phone="9999900001", client_ip="10.0.0.1", sandbox=True
        )
        second = await service.request_code(
            self.db, phone="9999900001", client_ip="10.0.0.1", sandbox=True
        )
        self.assertTrue(first["sandbox"])
        self.assertTrue(second["sandbox"])
        self.assertIn("dev_code", second)
        self.assertEqual(repo.throttles, {})

        resent = await service.resend_code(
            self.db,
            challenge_id=UUID(second["challenge_id"]),
            client_ip="10.0.0.1",
            phone="9999900001",
            sandbox=True,
        )
        self.assertTrue(resent["sandbox"])
        self.assertIn("dev_code", resent)

        with self.assertRaises(OtpRateLimited):
            await service.request_code(self.db, phone="9999900001", client_ip="10.0.0.1")

    async def test_sandbox_response_has_no_dev_code_when_expose_disabled(self):
        service, _ = self._dev_service(expose_code=False)
        response = await service.request_code(self.db, phone="9999900001", client_ip=None, sandbox=True)
        self.assertTrue(response["sandbox"])
        self.assertNotIn("dev_code", response)

    async def test_sandbox_is_denied_for_non_allowlisted_phone(self):
        service, repo = self._dev_service()
        with self.assertRaises(OtpSandboxUnavailable):
            await service.request_code(
                self.db, phone="987650000999", client_ip=None, sandbox=True
            )
        self.assertEqual(repo.throttles, {})
        self.assertEqual(repo.created, [])

    async def test_sandbox_is_denied_when_provider_has_no_sandbox_mode(self):
        account = _account("919876543299")
        self.db = FakeDb(account)
        service = PatientOtpService(
            POLICY,
            provider=FakeProvider(),
            repository=FakeRepository(account=account),
        )
        with self.assertRaises(OtpSandboxUnavailable):
            await service.request_code(
                self.db, phone="9876543299", client_ip=None, sandbox=True
            )

    async def test_sandbox_resend_is_denied_without_matching_phone(self):
        from uuid import UUID

        service, _ = self._dev_service()
        response = await service.request_code(
            self.db, phone="9999900001", client_ip=None, sandbox=True
        )
        with self.assertRaises(ExpiredOtpCode):
            await service.resend_code(
                self.db,
                challenge_id=UUID(response["challenge_id"]),
                client_ip=None,
                phone="987650000999",
                sandbox=True,
            )


class DevProviderGatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dev_provider_is_inert_in_production(self):
        provider = DevPatientOtpProvider(
            environment="production",
            allowlist=["+919999900001"],
            expose_code=True,
        )
        self.assertFalse(provider.configured)
        self.assertFalse(provider.allows_sandbox("919999900001"))
        with self.assertRaises(OtpProviderUnavailable):
            await provider.send_code(
                phone="919999900001",
                code="123456",
                challenge_id=uuid4(),
                purpose="login",
                expires_at=datetime.now(UTC),
            )

    async def test_dev_provider_sends_only_allowlisted_and_exposes_only_local(self):
        provider = DevPatientOtpProvider(
            environment="local",
            allowlist=["+919999900001"],
            expose_code=True,
        )
        self.assertTrue(provider.configured)
        self.assertTrue(provider.allows_sandbox("919999900001"))
        self.assertFalse(provider.allows_sandbox("919999900002"))
        challenge_id = uuid4()
        await provider.send_code(
            phone="919999900001",
            code="654321",
            challenge_id=challenge_id,
            purpose="login",
            expires_at=datetime.now(UTC),
        )
        self.assertEqual(provider.exposed_code(challenge_id), "654321")
        other_id = uuid4()
        await provider.send_code(
            phone="919999900002",
            code="111111",
            challenge_id=other_id,
            purpose="login",
            expires_at=datetime.now(UTC),
        )
        self.assertIsNone(provider.exposed_code(other_id))

    async def test_dev_provider_requires_allowlist(self):
        provider = DevPatientOtpProvider(
            environment="local", allowlist=[], expose_code=True
        )
        self.assertFalse(provider.configured)

    def test_generated_codes_are_numeric_with_requested_length(self):
        code = generate_numeric_code(6)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())


if __name__ == "__main__":
    unittest.main()
