"""Bounded retention/maintenance for the durable delivery foundation.

Runs periodically from the worker (never on a request path). Cancels jobs whose
data expiry passed, deletes terminal jobs past retention, and erases expired
short-lived email secret context. All operations are batched to avoid long
locks/backpressure.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ....core.config import settings
from ..email.repository import EmailDeliveryRepository
from .repository import DeliveryJobRepository


def should_run_maintenance(pass_index: int, every: int) -> bool:
    return every > 0 and pass_index % every == 0


async def run_maintenance(
    db: AsyncSession,
    *,
    retention_days: int | None = None,
    batch_size: int | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    retention_days = (
        settings.COMMUNICATION_JOB_RETENTION_DAYS if retention_days is None else retention_days
    )
    batch_size = settings.COMMUNICATION_WORKER_BATCH_SIZE if batch_size is None else batch_size
    jobs = DeliveryJobRepository()
    emails = EmailDeliveryRepository()
    result = {
        "cancelled": await jobs.cancel_expired(db, limit=batch_size, now=now),
        "purged": await jobs.purge_terminal(
            db, older_than=now - timedelta(days=max(1, retention_days)), limit=batch_size
        ),
        "secrets_erased": await emails.erase_expired_secrets(
            db, limit=batch_size, now=now
        ),
    }
    await db.commit()
    return result


async def run_maintenance_safely(db: AsyncSession, **kwargs) -> dict | None:
    try:
        return await run_maintenance(db, **kwargs)
    except SQLAlchemyError:
        await db.rollback()
        return None
