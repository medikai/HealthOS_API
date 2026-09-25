"""Durable delivery worker command.

Run from the repository root::

    venv/bin/python -m src.app.domains.communication.delivery.worker

This is the production-shaped worker: it claims bounded batches under lease in
short transactions, dispatches outside SQL, retries with exponential backoff
and jitter, and reclaims leases left by crashed workers. It needs PostgreSQL
only (no Redis/Celery/Kafka).

Combined in-process development mode (``COMMUNICATION_DEV_INPROCESS_DISPATCH``)
may call :func:`dispatch_inprocess` from a request/lifespan. Limitations:
it is single-process, ties dispatch latency to the caller's event loop, offers
no crash-restart cadence of its own, and must not be enabled in production.
"""

import argparse
import asyncio
import contextlib
import os
import signal
import socket
from uuid import uuid4

import structlog

from ....core.config import settings
from ....core.db.database import local_session
from ..shared.providers import DeliveryProvider
from .maintenance import run_maintenance_safely, should_run_maintenance
from .repository import DeliveryJobRepository
from .service import DeliveryDispatcher, DispatchOutcome

logger = structlog.get_logger(__name__)


def default_provider_registry() -> dict[str, DeliveryProvider]:
    """Real delivery providers are registered as each subsystem lands.

    Email is registered when a Send Mail token is configured; realtime/chat and
    push adapters follow. An unregistered channel is retried, never silently
    dropped.
    """
    from ..email.providers.zeptomail import build_email_delivery_provider
    from ..push.providers.fcm import build_push_provider
    from ..realtime.providers.notify import build_realtime_notification_provider

    registry: dict[str, DeliveryProvider] = {}
    email_provider = build_email_delivery_provider(settings)
    if email_provider is not None:
        registry[email_provider.channel] = email_provider
    realtime_provider = build_realtime_notification_provider(settings)
    if realtime_provider is not None:
        registry[realtime_provider.channel] = realtime_provider
    push_provider = build_push_provider(settings)
    if push_provider is not None:
        registry[push_provider.channel] = push_provider
    return registry


def worker_owner() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


async def dispatch_inprocess(owner: str | None = None) -> DispatchOutcome:
    """One dispatch pass for combined dev mode. See module docstring limits."""
    dispatcher = DeliveryDispatcher(
        DeliveryJobRepository(),
        default_provider_registry(),
        batch_size=settings.COMMUNICATION_WORKER_BATCH_SIZE,
        lease_seconds=settings.COMMUNICATION_JOB_LEASE_SECONDS,
        base_backoff_seconds=settings.COMMUNICATION_JOB_BASE_BACKOFF_SECONDS,
        max_backoff_seconds=settings.COMMUNICATION_JOB_MAX_BACKOFF_SECONDS,
    )
    async with local_session() as db:
        return await dispatcher.run_once(db, owner=owner or f"inprocess-{uuid4().hex[:8]}")


async def run_forever(
    *,
    owner: str,
    poll_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    dispatcher = DeliveryDispatcher(
        DeliveryJobRepository(),
        default_provider_registry(),
        batch_size=settings.COMMUNICATION_WORKER_BATCH_SIZE,
        lease_seconds=settings.COMMUNICATION_JOB_LEASE_SECONDS,
        base_backoff_seconds=settings.COMMUNICATION_JOB_BASE_BACKOFF_SECONDS,
        max_backoff_seconds=settings.COMMUNICATION_JOB_MAX_BACKOFF_SECONDS,
    )
    logger.info("communication_worker_started", owner=owner)
    pass_index = 0
    maintenance_every = max(
        1, settings.COMMUNICATION_WORKER_MAINTENANCE_EVERY_PASSES
    )
    while not stop_event.is_set():
        try:
            async with local_session() as db:
                outcome = await dispatcher.run_once(db, owner=owner)
        except Exception:
            logger.exception("communication_worker_pass_failed", owner=owner)
            await _sleep_or_stop(stop_event, poll_seconds)
            continue
        pass_index += 1
        if should_run_maintenance(pass_index, maintenance_every):
            try:
                async with local_session() as db:
                    maintenance = await run_maintenance_safely(db)
                if maintenance:
                    logger.info("communication_worker_maintenance", **maintenance)
            except Exception:
                logger.exception("communication_worker_maintenance_failed", owner=owner)
        if outcome.processed:
            logger.info("communication_worker_dispatch", **outcome.__dict__)
            await asyncio.sleep(0)
        else:
            await _sleep_or_stop(stop_event, poll_seconds)
    logger.info("communication_worker_stopped", owner=owner)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.1, seconds))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HealthOS communication delivery worker")
    parser.add_argument("--poll-seconds", type=float, default=settings.COMMUNICATION_WORKER_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="run a single dispatch pass and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    owner = worker_owner()

    async def runner() -> None:
        if args.once:
            outcome = await dispatch_inprocess(owner=owner)
            logger.info("communication_worker_once", **outcome.__dict__)
            return
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop_event.set)
        await run_forever(owner=owner, poll_seconds=args.poll_seconds, stop_event=stop_event)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
