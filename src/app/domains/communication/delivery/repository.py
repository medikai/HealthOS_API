"""Lease-based durable job SQL.

Rules enforced here:
- Claim/lease/reclaim run in short transactions with ``FOR UPDATE SKIP LOCKED``.
- No transaction is held across a provider network call (the dispatcher commits
  the claim before dispatching and commits each outcome afterwards).
- ``dedup_key`` is unique, so producers can enqueue idempotently.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ....models.communication import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_DEAD,
    JOB_STATUS_DELIVERED,
    JOB_STATUS_FAILED,
    JOB_STATUS_LEASED,
    JOB_STATUS_PENDING,
    DeliveryJob,
)

CLAIMABLE_STATUSES = (JOB_STATUS_PENDING, JOB_STATUS_FAILED)


class DeliveryJobRepository:
    async def enqueue(
        self,
        db: AsyncSession,
        *,
        channel: str,
        event_type: str,
        dedup_key: str,
        payload: dict,
        organization_id: UUID | None = None,
        facility_id: UUID | None = None,
        recipient_staff_id: UUID | None = None,
        due_at: datetime | None = None,
        max_attempts: int | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[DeliveryJob, bool]:
        """Insert idempotently. Returns ``(job, created)``.

        Does not commit: the caller commits the business change and the outbox
        row atomically.
        """
        values = {
            "id": uuid7(),
            "channel": channel,
            "event_type": event_type,
            "dedup_key": dedup_key,
            "payload": payload or {},
            "organization_id": organization_id,
            "facility_id": facility_id,
            "recipient_staff_id": recipient_staff_id,
            "status": JOB_STATUS_PENDING,
            "due_at": due_at or datetime.now(UTC),
            "expires_at": expires_at,
        }
        if max_attempts is not None:
            values["max_attempts"] = max_attempts
        stmt = (
            pg_insert(DeliveryJob)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(DeliveryJob.id)
        )
        new_id = (await db.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            existing = await db.scalar(
                select(DeliveryJob).where(DeliveryJob.dedup_key == dedup_key)
            )
            assert existing is not None
            return existing, False
        job = await db.get(DeliveryJob, new_id)
        assert job is not None
        return job, True

    async def claim_due(
        self,
        db: AsyncSession,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> list[DeliveryJob]:
        now = now or datetime.now(UTC)
        stmt = (
            select(DeliveryJob)
            .where(
                DeliveryJob.status.in_(CLAIMABLE_STATUSES),
                DeliveryJob.due_at <= now,
            )
            .order_by(DeliveryJob.due_at, DeliveryJob.created_at)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
        jobs = list((await db.scalars(stmt)).all())
        for job in jobs:
            job.status = JOB_STATUS_LEASED
            job.lease_owner = owner
            job.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
            job.attempts = (job.attempts or 0) + 1
            job.updated_at = now
        await db.commit()
        return jobs

    async def reclaim_expired(
        self, db: AsyncSession, *, limit: int = 100, now: datetime | None = None
    ) -> list[DeliveryJob]:
        now = now or datetime.now(UTC)
        stmt = (
            select(DeliveryJob)
            .where(
                DeliveryJob.status == JOB_STATUS_LEASED,
                DeliveryJob.lease_expires_at < now,
            )
            .order_by(DeliveryJob.lease_expires_at)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
        jobs = list((await db.scalars(stmt)).all())
        for job in jobs:
            job.status = (
                JOB_STATUS_PENDING
                if (job.attempts or 0) < (job.max_attempts or 0)
                else JOB_STATUS_DEAD
            )
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
        await db.commit()
        return jobs

    async def cancel_expired(
        self, db: AsyncSession, *, limit: int = 200, now: datetime | None = None
    ) -> int:
        """Stop attempting jobs whose data expiry passed (bounded)."""
        now = now or datetime.now(UTC)
        result = await db.execute(
            update(DeliveryJob)
            .where(
                DeliveryJob.id.in_(
                    select(DeliveryJob.id)
                    .where(
                        DeliveryJob.expires_at.is_not(None),
                        DeliveryJob.expires_at < now,
                        DeliveryJob.status.in_(
                            (JOB_STATUS_PENDING, JOB_STATUS_FAILED, JOB_STATUS_LEASED)
                        ),
                    )
                    .order_by(DeliveryJob.expires_at)
                    .limit(max(1, limit))
                )
            )
            .values(status=JOB_STATUS_CANCELLED, updated_at=now, lease_owner=None, lease_expires_at=None)
        )
        return int(result.rowcount or 0)

    async def purge_terminal(
        self,
        db: AsyncSession,
        *,
        older_than: datetime,
        limit: int = 200,
    ) -> int:
        """Delete delivered/dead/cancelled jobs past retention (bounded)."""
        result = await db.execute(
            delete(DeliveryJob).where(
                DeliveryJob.id.in_(
                    select(DeliveryJob.id)
                    .where(
                        DeliveryJob.status.in_(
                            (JOB_STATUS_DELIVERED, JOB_STATUS_DEAD, JOB_STATUS_CANCELLED)
                        ),
                        DeliveryJob.updated_at.is_not(None),
                        DeliveryJob.updated_at < older_than,
                    )
                    .order_by(DeliveryJob.updated_at)
                    .limit(max(1, limit))
                )
            )
        )
        return int(result.rowcount or 0)

    async def mark_delivered(
        self,
        db: AsyncSession,
        job: DeliveryJob,
        *,
        result: dict | None = None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        job.status = JOB_STATUS_DELIVERED
        job.result = result
        job.last_error = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.delivered_at = now
        job.updated_at = now
        await db.commit()

    async def mark_failed(
        self,
        db: AsyncSession,
        job: DeliveryJob,
        *,
        error: str,
        retry_delay_seconds: int | None = None,
        permanent: bool = False,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        exhausted = permanent or (job.attempts or 0) >= (job.max_attempts or 0)
        job.status = JOB_STATUS_DEAD if exhausted else JOB_STATUS_FAILED
        job.last_error = error[:1000]
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now
        if not exhausted and retry_delay_seconds is not None:
            job.due_at = now + timedelta(seconds=max(1, retry_delay_seconds))
        await db.commit()
