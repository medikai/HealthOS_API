"""Delivery dispatcher tests.

The dispatcher/backoff/idempotency logic is exercised with an in-memory
repository and fake providers (no DB). Real lease SQL (FOR UPDATE SKIP LOCKED)
is covered by ``test_communication_migration.py`` when a disposable
``TEST_POSTGRES_ASYNC_URL`` database is configured.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.app.domains.communication.delivery.maintenance import should_run_maintenance
from src.app.domains.communication.delivery.providers.fake import (
    FakeEmailProvider,
    FakePushProvider,
    FakeRealtimePublisher,
)
from src.app.domains.communication.delivery.service import (
    DeliveryDispatcher,
    DispatchOutcome,
)
from src.app.domains.communication.shared.errors import (
    PermanentDeliveryError,
    TransientDeliveryError,
)
from src.app.models.communication import (
    JOB_STATUS_DEAD,
    JOB_STATUS_DELIVERED,
    JOB_STATUS_FAILED,
    JOB_STATUS_LEASED,
    JOB_STATUS_PENDING,
)


def run(coro):
    return asyncio.run(coro)


class FakeJobRepository:
    """In-memory mirror of the lease repository contract."""

    def __init__(self) -> None:
        self.jobs: dict[str, SimpleNamespace] = {}

    async def enqueue(
        self,
        db,
        *,
        channel,
        event_type,
        dedup_key,
        payload,
        due_at=None,
        max_attempts=5,
        **kwargs,
    ):
        if dedup_key in self.jobs:
            return self.jobs[dedup_key], False
        job = SimpleNamespace(
            id=uuid4(),
            channel=channel,
            event_type=event_type,
            dedup_key=dedup_key,
            payload=payload,
            status=JOB_STATUS_PENDING,
            attempts=0,
            max_attempts=max_attempts,
            due_at=due_at or datetime.now(UTC),
            lease_owner=None,
            lease_expires_at=None,
            delivered_at=None,
            result=None,
            last_error=None,
        )
        self.jobs[dedup_key] = job
        return job, True

    async def claim_due(self, db, *, owner, limit, lease_seconds, now=None):
        now = now or datetime.now(UTC)
        claimable = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status in (JOB_STATUS_PENDING, JOB_STATUS_FAILED) and job.due_at <= now
            ),
            key=lambda job: job.due_at,
        )[:limit]
        for job in claimable:
            job.status = JOB_STATUS_LEASED
            job.lease_owner = owner
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.attempts += 1
        return claimable

    async def reclaim_expired(self, db, *, limit=100, now=None):
        now = now or datetime.now(UTC)
        expired = [
            job
            for job in self.jobs.values()
            if job.status == JOB_STATUS_LEASED and job.lease_expires_at < now
        ][:limit]
        for job in expired:
            job.status = (
                JOB_STATUS_PENDING if job.attempts < job.max_attempts else JOB_STATUS_DEAD
            )
            job.lease_owner = None
            job.lease_expires_at = None
        return expired

    async def mark_delivered(self, db, job, *, result=None, now=None):
        job.status = JOB_STATUS_DELIVERED
        job.result = result
        job.delivered_at = now or datetime.now(UTC)
        job.lease_owner = None
        job.lease_expires_at = None

    async def mark_failed(
        self, db, job, *, error, retry_delay_seconds=None, permanent=False, now=None
    ):
        now = now or datetime.now(UTC)
        exhausted = permanent or job.attempts >= job.max_attempts
        job.status = JOB_STATUS_DEAD if exhausted else JOB_STATUS_FAILED
        job.last_error = error
        job.lease_owner = None
        job.lease_expires_at = None
        if not exhausted and retry_delay_seconds is not None:
            job.due_at = now + timedelta(seconds=retry_delay_seconds)


def _dispatcher(repository, providers, **kwargs):
    defaults = {"batch_size": 10, "lease_seconds": 60, "base_backoff_seconds": 5}
    defaults.update(kwargs)
    return DeliveryDispatcher(repository, providers, **defaults)


def test_enqueue_is_idempotent_on_dedup_key():
    repository = FakeJobRepository()
    _, created_first = run(
        repository.enqueue(
            None, channel="email", event_type="email.test", dedup_key="k-1", payload={"a": 1}
        )
    )
    _, created_second = run(
        repository.enqueue(
            None, channel="email", event_type="email.test", dedup_key="k-1", payload={"a": 2}
        )
    )
    assert created_first is True
    assert created_second is False
    assert len(repository.jobs) == 1
    assert repository.jobs["k-1"].payload == {"a": 1}


def test_dispatcher_delivers_once_per_job():
    repository = FakeJobRepository()
    provider = FakeEmailProvider()
    run(
        repository.enqueue(
            None, channel="email", event_type="email.test", dedup_key="k-1", payload={"a": 1}
        )
    )
    outcome = run(_dispatcher(repository, {"email": provider}).run_once(None, owner="w1"))
    assert outcome == DispatchOutcome(claimed=1, delivered=1, failed=0, dead=0, reclaimed=0)
    assert provider.attempts == 1
    assert repository.jobs["k-1"].status == JOB_STATUS_DELIVERED


def test_dispatcher_retries_transient_failure_then_delivers():
    repository = FakeJobRepository()
    provider = FakeEmailProvider(fail_first=1)
    job, _ = run(
        repository.enqueue(
            None,
            channel="email",
            event_type="email.test",
            dedup_key="k-2",
            payload={},
            max_attempts=3,
        )
    )
    dispatcher = _dispatcher(repository, {"email": provider})
    first = run(dispatcher.run_once(None, owner="w1"))
    assert first.failed == 1 and first.delivered == 0
    assert job.status == JOB_STATUS_FAILED and job.attempts == 1

    job.due_at = datetime.now(UTC) - timedelta(seconds=1)  # simulate backoff elapsed
    second = run(dispatcher.run_once(None, owner="w1"))
    assert second.delivered == 1
    assert repository.jobs["k-2"].status == JOB_STATUS_DELIVERED


def test_dispatcher_dead_letters_after_max_attempts():
    repository = FakeJobRepository()
    provider = FakePushProvider(fail_first=99)
    job, _ = run(
        repository.enqueue(
            None,
            channel="push",
            event_type="push.test",
            dedup_key="k-3",
            payload={},
            max_attempts=1,
        )
    )
    run(_dispatcher(repository, {"push": provider}).run_once(None, owner="w1"))
    assert job.status == JOB_STATUS_DEAD
    assert job.attempts == 1


def test_dispatcher_retries_unregistered_channel_without_dropping():
    repository = FakeJobRepository()
    job, _ = run(
        repository.enqueue(
            None,
            channel="email",
            event_type="email.test",
            dedup_key="k-4",
            payload={},
            max_attempts=3,
        )
    )
    outcome = run(_dispatcher(repository, {}).run_once(None, owner="w1"))
    assert outcome.failed == 1
    assert job.status == JOB_STATUS_FAILED
    assert job.last_error == "PROVIDER_UNAVAILABLE:provider_not_registered"


def test_reclaim_expired_lease_returns_job_to_claimable():
    repository = FakeJobRepository()
    job, _ = run(
        repository.enqueue(
            None, channel="email", event_type="email.test", dedup_key="k-5", payload={}
        )
    )
    job.status = JOB_STATUS_LEASED
    job.attempts = 1
    job.lease_owner = "crashed-worker"
    job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=5)

    reclaimed = run(repository.reclaim_expired(None))
    assert len(reclaimed) == 1
    assert job.status == JOB_STATUS_PENDING
    assert job.lease_owner is None


def test_maintenance_runs_on_bounded_cadence():
    assert should_run_maintenance(0, 60) is True
    assert should_run_maintenance(60, 60) is True
    assert should_run_maintenance(59, 60) is False
    assert should_run_maintenance(5, 0) is False


def test_fake_realtime_publisher_records_publish():
    provider = FakeRealtimePublisher()
    result = run(provider.publish(channel="dev:t:o:u:s:g:1", event_type="message.created", payload={}))
    assert result["provider"] == "fake-realtime"
    assert provider.published[0]["event_type"] == "message.created"


class _RaisingProvider:
    channel = "email"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def deliver(self, job):
        raise self.exc


def _enqueue_channel(repository, dedup_key, *, max_attempts=5):
    return run(
        repository.enqueue(
            None,
            channel="email",
            event_type="email.test",
            dedup_key=dedup_key,
            payload={},
            max_attempts=max_attempts,
        )
    )


def test_dispatcher_dead_letters_permanent_error_immediately():
    repository = FakeJobRepository()
    job, _ = _enqueue_channel(repository, "k-permanent", max_attempts=5)
    provider = _RaisingProvider(PermanentDeliveryError("zeptomail_rejected_400"))
    outcome = run(_dispatcher(repository, {"email": provider}).run_once(None, owner="w1"))
    assert outcome.dead == 1
    assert job.status == JOB_STATUS_DEAD
    assert job.attempts == 1
    assert job.last_error == "DELIVERY_PERMANENT:zeptomail_rejected_400"


def test_dispatcher_honours_retry_after_seconds():
    repository = FakeJobRepository()
    job, _ = _enqueue_channel(repository, "k-rate-limited", max_attempts=5)
    provider = _RaisingProvider(
        TransientDeliveryError("zeptomail_rate_limited", retry_after_seconds=42)
    )
    before = datetime.now(UTC)
    run(_dispatcher(repository, {"email": provider}).run_once(None, owner="w1"))
    assert job.status == JOB_STATUS_FAILED
    assert (job.due_at - before).total_seconds() >= 40


# ------------------------------------------------- worker resilience (BE09)


class _FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


class _HangingDispatcher:
    def __init__(self):
        self.cancelled = False

    async def run_once(self, db, *, owner):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_bounded_pass_times_out_and_cancels_a_stalled_provider(monkeypatch):
    from src.app.domains.communication.delivery import worker as worker_module

    monkeypatch.setattr(worker_module, "local_session", lambda: _FakeSessionContext())
    dispatcher = _HangingDispatcher()

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await worker_module.run_pass_bounded(
                dispatcher, owner="test", timeout_seconds=1
            )

    run(scenario())
    assert dispatcher.cancelled is True


def test_reset_providers_swallows_provider_reset_errors():
    from src.app.domains.communication.delivery import worker as worker_module

    calls = []

    class _Resettable:
        def reset(self):
            calls.append("reset")

    class _Broken:
        def reset(self):
            raise RuntimeError("boom")

    worker_module._reset_providers({"ok": _Resettable(), "broken": _Broken()})
    assert calls == ["reset"]
