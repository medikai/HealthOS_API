"""Delivery dispatcher skeleton.

Claims a bounded batch under lease in a short transaction, then dispatches each
job to its channel provider outside any SQL transaction. Reclaims crashed
leases first. Provider adapters implement ``DeliveryProvider``.
"""

import random
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from ....models.communication import JOB_STATUS_DEAD, DeliveryJob
from ..shared.errors import DeliveryError, PermanentDeliveryError, ProviderUnavailable
from ..shared.providers import DeliveryProvider
from .repository import DeliveryJobRepository


@dataclass(frozen=True)
class DispatchOutcome:
    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    dead: int = 0
    reclaimed: int = 0

    @property
    def processed(self) -> int:
        return self.claimed


class RngLike(Protocol):
    def random(self) -> float: ...


class DeliveryDispatcher:
    def __init__(
        self,
        repository: DeliveryJobRepository,
        providers: dict[str, DeliveryProvider],
        *,
        batch_size: int = 25,
        lease_seconds: int = 60,
        base_backoff_seconds: int = 5,
        max_backoff_seconds: int = 300,
        rng: RngLike | None = None,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.batch_size = max(1, batch_size)
        self.lease_seconds = max(1, lease_seconds)
        self.base_backoff_seconds = max(1, base_backoff_seconds)
        self.max_backoff_seconds = max(self.base_backoff_seconds, max_backoff_seconds)
        self._rng = rng or random.Random()

    def _backoff_seconds(self, attempts: int, retry_after_seconds: int | None = None) -> int:
        if retry_after_seconds is not None and retry_after_seconds > 0:
            return int(min(retry_after_seconds, self.max_backoff_seconds))
        exponent = max(0, int(attempts) - 1)
        base = self.base_backoff_seconds * (2**exponent)
        capped = min(base, self.max_backoff_seconds)
        jitter = self._rng.random() * capped * 0.25
        return int(capped + jitter)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, (ProviderUnavailable, DeliveryError)):
            reason = getattr(exc, "reason", "")
            return f"{exc.code}:{reason}" if reason else exc.code
        return type(exc).__name__

    async def run_once(self, db: AsyncSession, *, owner: str) -> DispatchOutcome:
        reclaimed = await self.repository.reclaim_expired(db, limit=self.batch_size)
        jobs = await self.repository.claim_due(
            db,
            owner=owner,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        delivered = failed = dead = 0
        for job in jobs:
            provider = self.providers.get(job.channel)
            if provider is None:
                await self._fail(db, job, ProviderUnavailable("provider_not_registered"))
            else:
                try:
                    result = await provider.deliver(job)
                except Exception as exc:  # noqa: BLE001 - any provider failure retries
                    await self._fail(db, job, exc)
                else:
                    await self.repository.mark_delivered(db, job, result=result)
                    delivered += 1
                    continue
            if job.status == JOB_STATUS_DEAD:
                dead += 1
            else:
                failed += 1
        return DispatchOutcome(
            claimed=len(jobs),
            delivered=delivered,
            failed=failed,
            dead=dead,
            reclaimed=len(reclaimed),
        )

    async def _fail(self, db: AsyncSession, job: DeliveryJob, exc: Exception) -> None:
        permanent = isinstance(exc, PermanentDeliveryError) or not getattr(
            exc, "retryable", True
        )
        retry_after = getattr(exc, "retry_after_seconds", None)
        await self.repository.mark_failed(
            db,
            job,
            error=self._safe_error(exc),
            retry_delay_seconds=(
                None
                if permanent
                else self._backoff_seconds(job.attempts or 1, retry_after)
            ),
            permanent=permanent,
        )
