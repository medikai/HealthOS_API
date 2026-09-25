"""SQL for durable email messages and their delivery metadata."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ....models.communication import (
    EMAIL_STATUS_ACCEPTED,
    EMAIL_STATUS_FAILED,
    EMAIL_STATUS_QUEUED,
    EMAIL_STATUS_UNKNOWN,
    EmailMessage,
)


class EmailDeliveryRepository:
    async def create_message(
        self,
        db: AsyncSession,
        *,
        dedup_key: str,
        template_code: str,
        recipient_email: str,
        recipient_name: str | None,
        from_email: str,
        from_name: str | None,
        subject: str | None,
        context: dict,
        encrypted_context: str | None,
        is_secret: bool,
        organization_id: UUID | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[EmailMessage, bool]:
        """Insert idempotently. Returns ``(message, created)``; does not commit."""
        stmt = (
            pg_insert(EmailMessage)
            .values(
                id=uuid7(),
                dedup_key=dedup_key,
                template_code=template_code,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                from_email=from_email,
                from_name=from_name,
                subject=subject,
                status=EMAIL_STATUS_QUEUED,
                context=context,
                encrypted_context=encrypted_context,
                is_secret=is_secret,
                organization_id=organization_id,
                created_at=datetime.now(UTC),
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(EmailMessage.id)
        )
        new_id = (await db.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            existing = await db.scalar(
                select(EmailMessage).where(EmailMessage.dedup_key == dedup_key)
            )
            assert existing is not None
            return existing, False
        message = await db.get(EmailMessage, new_id)
        assert message is not None
        return message, True

    async def get(self, db: AsyncSession, message_id: UUID) -> EmailMessage | None:
        return await db.get(EmailMessage, message_id)

    async def erase_expired_secrets(
        self, db: AsyncSession, *, limit: int = 200, now: datetime | None = None
    ) -> int:
        """Erase short-lived secret context whose expiry passed (bounded)."""
        now = now or datetime.now(UTC)
        result = await db.execute(
            update(EmailMessage)
            .where(
                EmailMessage.id.in_(
                    select(EmailMessage.id)
                    .where(
                        EmailMessage.encrypted_context.is_not(None),
                        EmailMessage.expires_at.is_not(None),
                        EmailMessage.expires_at < now,
                    )
                    .order_by(EmailMessage.expires_at)
                    .limit(max(1, limit))
                )
            )
            .values(encrypted_context=None, updated_at=now)
        )
        return int(result.rowcount or 0)

    async def mark_accepted(
        self,
        db: AsyncSession,
        message: EmailMessage,
        *,
        provider: str,
        provider_request_id: str | None,
        provider_message_id: str | None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        message.status = EMAIL_STATUS_ACCEPTED
        message.provider = provider
        message.provider_request_id = provider_request_id
        message.provider_message_id = provider_message_id
        message.accepted_at = now
        message.updated_at = now
        message.last_error = None
        message.encrypted_context = None  # erase secret after terminal outcome
        await db.commit()

    async def mark_failed(
        self,
        db: AsyncSession,
        message: EmailMessage,
        *,
        error: str,
        status: str = EMAIL_STATUS_FAILED,
        clear_secret: bool = True,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        message.status = status
        message.last_error = error[:500]
        message.updated_at = now
        if clear_secret:
            message.encrypted_context = None
        await db.commit()

    async def mark_unknown(
        self,
        db: AsyncSession,
        message: EmailMessage,
        *,
        error: str,
        now: datetime | None = None,
    ) -> None:
        """Timeout/uncertain outcome: keep context for bounded retry, mark unknown."""
        await self.mark_failed(
            db,
            message,
            error=error,
            status=EMAIL_STATUS_UNKNOWN,
            clear_secret=False,
            now=now,
        )
