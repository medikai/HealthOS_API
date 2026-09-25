"""FCM push adapter.

The SDK is imported lazily so missing configuration/SDK never breaks unrelated
APIs. Messages are data-only and generic (no names/MRNs/chat bodies); the
service worker owns display with a stable tag so FCM auto-display cannot
duplicate. Recipient/task/device/preference state is rechecked before send and
invalid provider tokens are revoked.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .....core.db.database import local_session
from .....models.communication import NotificationRecipient
from ...notifications.repository import NotificationRepository
from ...notifications.service import NotificationService
from ...shared.errors import (
    PermanentDeliveryError,
    ProviderUnavailable,
    TransientDeliveryError,
)
from ..constants import (
    DEEP_LINK,
    GENERIC_BODY,
    GENERIC_TITLE,
    PUSH_PROVIDER,
)
from ..repository import PushRepository
from ..service import PushService, push_configured

logger = structlog.get_logger(__name__)

_APPS: dict[str, Any] = {}


class FcmPushProvider:
    channel = "push"

    def __init__(
        self,
        *,
        settings: Any,
        repository: PushRepository | None = None,
        notifications: NotificationRepository | None = None,
        service: NotificationService | None = None,
        push_service: PushService | None = None,
        session_factory: Any = None,
    ) -> None:
        self.settings = settings
        self.repository = repository or PushRepository()
        self.notifications = notifications or NotificationRepository()
        self.service = service or NotificationService()
        self.push_service = push_service or PushService(
            enabled=settings.FCM_ENABLED,
            provider_configured=push_configured(settings),
        )
        self._session_factory = session_factory or local_session

    def __repr__(self) -> str:  # never leak credentials
        return f"FcmPushProvider(configured={self.configured})"

    @property
    def configured(self) -> bool:
        return push_configured(self.settings)

    def _app(self):
        if not self.configured:
            raise ProviderUnavailable("fcm_not_configured", "Push is not configured.")
        key = self.settings.FIREBASE_PROJECT_ID or "default"
        if key in _APPS:
            return _APPS[key]
        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError as exc:  # pragma: no cover - dependency declared
            raise ProviderUnavailable("fcm_sdk_missing", "Firebase SDK is not installed.") from exc
        options: dict[str, Any] = {}
        try:
            if self.settings.FIREBASE_SERVICE_ACCOUNT_FILE:
                # Explicit file credentials; contents are never printed/stored.
                cred = credentials.Certificate(self.settings.FIREBASE_SERVICE_ACCOUNT_FILE)
                if self.settings.FIREBASE_PROJECT_ID:
                    options["projectId"] = self.settings.FIREBASE_PROJECT_ID
                elif getattr(cred, "project_id", None):
                    options["projectId"] = cred.project_id
            elif self.settings.FIREBASE_CREDENTIALS_JSON:
                info = json.loads(self.settings.FIREBASE_CREDENTIALS_JSON.get_secret_value())
                cred = credentials.Certificate(info)
                project = info.get("project_id")
                if project:
                    options["projectId"] = project
            else:
                # Preserve existing authorized ADC (e.g. GCS/Google) credentials.
                cred = credentials.ApplicationDefault()
                if self.settings.FIREBASE_PROJECT_ID:
                    options["projectId"] = self.settings.FIREBASE_PROJECT_ID
            app = firebase_admin.initialize_app(cred, options or None, name=key)
        except Exception as exc:
            raise ProviderUnavailable("fcm_credentials_invalid", "Push credentials are invalid.") from exc
        _APPS[key] = app
        return app

    def _message(self, *, token: str, event: Any):
        from firebase_admin import messaging

        data = {
            "notification_id": str(event.id),
            "event_type": event.event_type,
            "priority": event.priority,
            "category": event.category,
            "title": GENERIC_TITLE,
            "body": GENERIC_BODY,
            "tag": f"healthos-notification-{event.id}",
            "deep_link": DEEP_LINK,
        }
        urgency = "high" if event.priority == "P1" else "normal"
        return messaging.Message(
            token=token,
            data=data,
            webpush=messaging.WebpushConfig(
                headers={"Urgency": urgency, "TTL": str(self.push_service.ttl_seconds)}
            ),
        )

    async def _revoke_invalid_token(self, device_id: UUID) -> None:
        try:
            async with self._session_factory() as db:
                device = await self.repository.get(db, device_id)
                if device is not None and device.is_active:
                    await self.repository.revoke(db, device, reason="invalid_token")
                    await db.commit()
        except SQLAlchemyError:
            logger.warning("push_invalid_token_cleanup_failed", device_id=str(device_id))

    async def deliver(self, job: Any) -> dict:
        payload = job.payload or {}
        device_id = _uuid(payload.get("push_device_id"))
        notification_id = _uuid(payload.get("notification_id"))
        staff_member_id = _uuid(payload.get("staff_member_id"))
        if device_id is None or notification_id is None or staff_member_id is None:
            raise PermanentDeliveryError("push_payload_invalid", "Job is missing push identity.")

        async with self._session_factory() as db:
            device = await self.repository.get(db, device_id)
            if device is None or not device.is_active or device.revoked_at is not None:
                raise PermanentDeliveryError("push_device_inactive", "Device is not active.")
            if device.staff_member_id != staff_member_id:
                raise PermanentDeliveryError("push_device_mismatch", "Device ownership mismatch.")
            event = await self.notifications.get_event(db, notification_id)
            recipient = await db.scalar(
                select(NotificationRecipient).where(
                    NotificationRecipient.notification_id == notification_id,
                    NotificationRecipient.staff_member_id == staff_member_id,
                )
            )
            if event is None or recipient is None:
                raise PermanentDeliveryError("push_notification_missing")
            decision = await self.service.evaluate_dispatch(db, event, recipient)
            if decision != "publish":
                await db.commit()
                return {"provider": PUSH_PROVIDER, "accepted": False, "decision": decision}
            if not await self.push_service.device_eligible(db, event=event, device=device):
                await db.commit()
                return {"provider": PUSH_PROVIDER, "accepted": False, "decision": "ineligible"}
            token = device.token
            await db.commit()

        from firebase_admin import exceptions as fb_exceptions
        from firebase_admin import messaging

        message = self._message(token=token, event=event)
        try:
            message_id = await asyncio.to_thread(messaging.send, message, app=self._app())
        except (
            messaging.UnregisteredError,
            messaging.SenderIdMismatchError,
            messaging.InvalidArgumentError,
        ) as exc:
            await self._revoke_invalid_token(device_id)
            raise PermanentDeliveryError(
                "fcm_invalid_token", "Push token was rejected."
            ) from exc
        except messaging.QuotaExceededError as exc:
            raise TransientDeliveryError("fcm_quota_exceeded") from exc
        except (
            messaging.InternalError,
            messaging.UnavailableError,
            fb_exceptions.FirebaseError,
        ) as exc:
            raise TransientDeliveryError("fcm_transient") from exc

        logger.info(
            "push_accepted",
            notification_id=str(notification_id),
            device_id=str(device_id),
        )
        return {
            "provider": PUSH_PROVIDER,
            "accepted": True,
            "message_id": message_id,
        }


def _uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def build_push_provider(settings: Any) -> FcmPushProvider | None:
    if not push_configured(settings):
        return None
    return FcmPushProvider(settings=settings)
