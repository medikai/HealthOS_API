"""ZeptoMail transport adapter.

Uses the existing async ``httpx`` client (no blocking requests inside async
handlers), JSON payloads and ``Authorization: Zoho-enczapikey <token>``.
Permanent errors (4xx validation/auth) stop; transient errors (408/429/5xx/
network) raise ``TransientDeliveryError`` and are retried by the durable worker
with backoff/Retry-After. A timeout is uncertain: the message is marked
``unknown`` because the provider may have accepted it.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import structlog

from .....core.db.database import local_session
from .....models.communication import (
    EMAIL_STATUS_FAILED,
    EMAIL_STATUS_UNKNOWN,
    EmailMessage,
)
from ...shared.errors import (
    PermanentDeliveryError,
    ProviderUnavailable,
    TransientDeliveryError,
)
from ...shared.secrets import SecretContextError, decrypt_secret_context
from ..repository import EmailDeliveryRepository
from ..schemas import EmailProviderRequest, EmailProviderResult
from ..templates import EmailTemplateError, render_template

logger = structlog.get_logger(__name__)

EMAIL_PATH = "/v1.1/email"


class ZeptoMailClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._token = token or None
        self.timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:  # never leak the token
        return f"ZeptoMailClient(configured={self.configured}, base_url={self.base_url!r})"

    @property
    def configured(self) -> bool:
        return bool(self._token and self.base_url)

    def _payload(self, request: EmailProviderRequest) -> dict[str, Any]:
        return {
            "from": {
                "address": str(request.from_email),
                "name": request.from_name or "",
            },
            "to": [
                {
                    "email_address": {
                        "address": str(request.to_email),
                        "name": request.to_name or "",
                    }
                }
            ],
            "subject": request.subject,
            "htmlbody": request.html_body,
            "textbody": request.text_body,
        }

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return None

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict):
            candidate = body.get("request_id") or body.get("requestId")
            if isinstance(candidate, str):
                return candidate
        return None

    async def send(self, request: EmailProviderRequest) -> EmailProviderResult:
        if not self.configured:
            raise ProviderUnavailable(
                "zeptomail_not_configured", "Email transport is not configured."
            )
        url = f"{self.base_url}{EMAIL_PATH}"
        headers = {
            "Authorization": f"Zoho-enczapikey {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                response = await client.post(url, json=self._payload(request), headers=headers)
        except httpx.TimeoutException as exc:
            raise TransientDeliveryError(
                "zeptomail_timeout", "ZeptoMail did not respond in time."
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientDeliveryError(
                "zeptomail_network", "ZeptoMail request failed."
            ) from exc

        status_code = response.status_code
        if 200 <= status_code < 300:
            return EmailProviderResult(
                provider="zeptomail",
                accepted=True,
                status_code=status_code,
                provider_request_id=self._request_id(response),
            )
        if status_code == 429:
            raise TransientDeliveryError(
                "zeptomail_rate_limited",
                "ZeptoMail rate limited the request.",
                retry_after_seconds=self._retry_after(response),
            )
        if status_code == 408 or status_code >= 500:
            raise TransientDeliveryError(
                "zeptomail_server_error",
                "ZeptoMail is temporarily unavailable.",
                retry_after_seconds=self._retry_after(response),
            )
        # 4xx validation/auth/rejection: do not retry; never log the body/token.
        raise PermanentDeliveryError(
            f"zeptomail_rejected_{status_code}", "ZeptoMail rejected the message."
        )


class EmailDeliveryProvider:
    """Delivery provider that renders and sends one durable email message."""

    channel = "email"

    def __init__(
        self,
        *,
        client: ZeptoMailClient,
        repository: EmailDeliveryRepository | None = None,
        session_factory: Any = None,
    ) -> None:
        self.client = client
        self.repository = repository or EmailDeliveryRepository()
        self._session_factory = session_factory or local_session

    def _build_request(self, message: EmailMessage) -> EmailProviderRequest:
        if message.expires_at is not None and message.expires_at < datetime.now(UTC):
            raise PermanentDeliveryError(
                "email_context_expired", "Email secret context has expired."
            )
        context = dict(message.context or {})
        if message.encrypted_context:
            try:
                context.update(decrypt_secret_context(message.encrypted_context))
            except SecretContextError as exc:
                raise PermanentDeliveryError(
                    "email_context_invalid", "Email secret context is invalid."
                ) from exc
        try:
            rendered = render_template(message.template_code, context)
        except EmailTemplateError as exc:
            raise PermanentDeliveryError(
                "email_template_invalid", "Email template could not be rendered."
            ) from exc
        return EmailProviderRequest(
            to_email=message.recipient_email,
            to_name=message.recipient_name,
            from_email=message.from_email,
            from_name=message.from_name,
            subject=rendered.subject,
            html_body=rendered.html_body,
            text_body=rendered.text_body,
        )

    async def _record_accepted(
        self, message_id: UUID, result: EmailProviderResult
    ) -> None:
        async with self._session_factory() as db:
            message = await self.repository.get(db, message_id)
            if message is None:
                return
            await self.repository.mark_accepted(
                db,
                message,
                provider=result.provider,
                provider_request_id=result.provider_request_id,
                provider_message_id=result.provider_message_id,
            )

    async def _record_failure(
        self, message_id: UUID, *, error: str, status: str, clear_secret: bool
    ) -> None:
        async with self._session_factory() as db:
            message = await self.repository.get(db, message_id)
            if message is None:
                return
            await self.repository.mark_failed(
                db, message, error=error, status=status, clear_secret=clear_secret
            )

    async def deliver(self, job: Any) -> dict:
        raw_id = (job.payload or {}).get("email_message_id")
        if not raw_id:
            raise PermanentDeliveryError("email_message_missing", "Job has no email message.")
        try:
            message_id = UUID(str(raw_id))
        except ValueError as exc:
            raise PermanentDeliveryError(
                "email_message_invalid", "Job references an invalid message."
            ) from exc

        async with self._session_factory() as db:
            message = await self.repository.get(db, message_id)
            if message is None:
                raise PermanentDeliveryError(
                    "email_message_missing", "Email message was not found."
                )
            request = self._build_request(message)

        try:
            result = await self.client.send(request)
        except TransientDeliveryError as exc:
            uncertain = exc.reason == "zeptomail_timeout"
            await self._record_failure(
                message_id,
                error=f"{exc.code}:{exc.reason}",
                status=EMAIL_STATUS_UNKNOWN if uncertain else EMAIL_STATUS_FAILED,
                clear_secret=False,
            )
            raise
        except PermanentDeliveryError as exc:
            await self._record_failure(
                message_id,
                error=f"{exc.code}:{exc.reason}",
                status=EMAIL_STATUS_FAILED,
                clear_secret=True,
            )
            raise

        await self._record_accepted(message_id, result)
        return {
            "provider": result.provider,
            "accepted": True,
            "status_code": result.status_code,
        }


def build_email_delivery_provider(settings: Any) -> EmailDeliveryProvider | None:
    token = settings.ZEPTOMAIL_SEND_TOKEN
    client = ZeptoMailClient(
        base_url=settings.ZEPTOMAIL_API_BASE_URL,
        token=token.get_secret_value() if token else None,
        timeout=settings.EMAIL_TIMEOUT_SECONDS,
    )
    if not client.configured:
        return None
    return EmailDeliveryProvider(client=client)
