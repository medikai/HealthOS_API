"""Internal email service and the auth-to-email call contract.

Auth (and other domains) call ``enqueue_template`` inside their own transaction.
This service validates the approved template, splits secret fields into
encrypted context, persists a durable ``EmailMessage`` and enqueues a delivery
job whose payload carries only the message id. The worker renders and sends.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ....core.config import Settings
from ....models.communication import JOB_CHANNEL_EMAIL, EmailMessage
from ..delivery.repository import DeliveryJobRepository
from ..shared.errors import ProviderUnavailable
from ..shared.secrets import encrypt_secret_context
from .repository import EmailDeliveryRepository
from .templates import secret_fields_for, validate_template_context

DEFAULT_SECRET_TTL_SECONDS = 900


class EmailService:
    def __init__(
        self,
        *,
        repository: EmailDeliveryRepository,
        delivery_repository: DeliveryJobRepository,
        configured: bool,
        from_email: str,
        from_name: str | None,
        max_attempts: int,
        secret_ttl_seconds: int = DEFAULT_SECRET_TTL_SECONDS,
    ) -> None:
        self.repository = repository
        self.delivery_repository = delivery_repository
        self.configured = configured
        self.from_email = from_email
        self.from_name = from_name
        self.max_attempts = max(1, int(max_attempts))
        self.secret_ttl_seconds = max(60, int(secret_ttl_seconds))

    async def enqueue_template(
        self,
        db: AsyncSession,
        *,
        template_code: str,
        to_email: str,
        dedup_key: str,
        context: dict[str, Any],
        to_name: str | None = None,
        organization_id: UUID | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[EmailMessage, bool]:
        """Create message + delivery job atomically (caller commits).

        Raises ``ProviderUnavailable`` when the transport is not configured and
        ``InvalidTemplateContext``/``UnsupportedTemplate`` for bad input.
        Returns ``(message, created)``; ``created=False`` is an idempotent replay.
        """
        if not self.configured:
            raise ProviderUnavailable(
                "zeptomail_not_configured", "Email transport is not configured."
            )
        now = now or datetime.now(UTC)
        merged = dict(context)
        validate_template_context(template_code, merged)
        secret_names = secret_fields_for(template_code)
        secret_context = {key: value for key, value in merged.items() if key in secret_names}
        public_context = {key: value for key, value in merged.items() if key not in secret_names}
        encrypted_context = (
            encrypt_secret_context(secret_context) if secret_context else None
        )
        if expires_at is None and secret_context:
            expires_at = now + timedelta(seconds=self.secret_ttl_seconds)

        message, created = await self.repository.create_message(
            db,
            dedup_key=dedup_key,
            template_code=template_code,
            recipient_email=str(to_email),
            recipient_name=to_name,
            from_email=self.from_email,
            from_name=self.from_name,
            subject=None,  # rendered at dispatch; secret subjects never stored
            context=public_context,
            encrypted_context=encrypted_context,
            is_secret=bool(secret_context),
            organization_id=organization_id,
            expires_at=expires_at,
        )
        if created:
            await self.delivery_repository.enqueue(
                db,
                channel=JOB_CHANNEL_EMAIL,
                event_type=f"email.{template_code}",
                dedup_key=f"email:{dedup_key}",
                payload={"email_message_id": str(message.id)},
                organization_id=organization_id,
                max_attempts=self.max_attempts,
                expires_at=expires_at,
            )
        return message, created


def build_email_service(settings: Settings) -> EmailService:
    return EmailService(
        repository=EmailDeliveryRepository(),
        delivery_repository=DeliveryJobRepository(),
        configured=bool(settings.ZEPTOMAIL_SEND_TOKEN),
        from_email=settings.EMAIL_FROM_ADDRESS,
        from_name=settings.EMAIL_FROM_NAME,
        max_attempts=settings.EMAIL_MAX_ATTEMPTS,
    )
