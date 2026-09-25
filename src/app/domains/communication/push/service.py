"""Push device lifecycle and eligibility.

Registration/revocation derive ownership from auth. Push jobs are queued only
for P1/P2 notifications whose recipient has an active device, desktop alerts
enabled and is not in quiet hours. P3 stays off by default. Provider
acceptance, client receipt, read and acknowledgement remain separate states.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ....core.config import Settings
from ....models.communication import NotificationEvent
from ..delivery.repository import DeliveryJobRepository
from ..notifications.repository import NotificationRepository
from ..notifications.serialization import preference_defaults
from .constants import (
    DISPLAY_STRATEGY,
    MAX_INSTALLATION_ID_LENGTH,
    MAX_TOKEN_LENGTH,
    PUSH_CHANNEL,
    PUSH_PRIORITIES,
    PUSH_PROVIDER,
)
from .repository import PushRepository

DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_PUSH_TTL_SECONDS = 3600
DEFAULT_PUSH_MAX_ATTEMPTS = 5


def _parse_hhmm(value: str | None) -> int | None:
    if not value:
        return None
    try:
        hours, minutes = value.split(":", 1)
        return (int(hours) % 24) * 60 + int(minutes) % 60
    except (ValueError, AttributeError):
        return None


def is_quiet_now(
    *, enabled: bool, start: str | None, end: str | None, timezone_name: str | None, now: datetime
) -> bool:
    if not enabled:
        return False
    start_minutes = _parse_hhmm(start)
    end_minutes = _parse_hhmm(end)
    if start_minutes is None or end_minutes is None or start_minutes == end_minutes:
        return False
    try:
        tz = ZoneInfo(timezone_name or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - unknown timezone falls back to UTC
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    minutes = local.hour * 60 + local.minute
    if start_minutes < end_minutes:
        return start_minutes <= minutes < end_minutes
    return minutes >= start_minutes or minutes < end_minutes  # overnight window


def device_item(device: Any) -> dict:
    return {
        "id": str(device.id),
        "platform": device.platform,
        "environment": device.environment,
        "installationId": device.installation_id,
        "active": bool(device.is_active),
        "lastSeenAt": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "createdAt": device.created_at.isoformat() if device.created_at else None,
    }


def push_sdk_available() -> bool:
    return importlib.util.find_spec("firebase_admin") is not None


def push_configuration_reason(settings: Settings) -> str | None:
    """Why push is unavailable, or ``None`` when a credential source is present.

    Credential sources: explicit service-account file, inline credentials JSON,
    or ADC with an explicit project id. A configured-but-missing file is
    reported without ever reading or logging its contents.
    """
    if not settings.FCM_ENABLED:
        return "push_disabled"
    if not push_sdk_available():
        return "fcm_sdk_missing"
    if settings.FIREBASE_CREDENTIALS_JSON:
        return None
    if settings.FIREBASE_SERVICE_ACCOUNT_FILE:
        if Path(settings.FIREBASE_SERVICE_ACCOUNT_FILE).expanduser().is_file():
            return None
        return "fcm_credentials_missing"
    if settings.FIREBASE_PROJECT_ID:
        return None  # authorized ADC path
    return "fcm_not_configured"


def push_configured(settings: Settings) -> bool:
    return push_configuration_reason(settings) is None


class PushService:
    def __init__(
        self,
        *,
        enabled: bool,
        provider_configured: bool,
        repository: PushRepository | None = None,
        notifications: NotificationRepository | None = None,
        delivery: DeliveryJobRepository | None = None,
        ttl_seconds: int = DEFAULT_PUSH_TTL_SECONDS,
        max_attempts: int = DEFAULT_PUSH_MAX_ATTEMPTS,
        sdk_available: bool = True,
        configuration_reason: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.provider_configured = provider_configured
        self.sdk_available = sdk_available
        self.configuration_reason = configuration_reason
        self.repository = repository or PushRepository()
        self.notifications = notifications or NotificationRepository()
        self.delivery = delivery or DeliveryJobRepository()
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_attempts = max(1, int(max_attempts))

    def config_status(self) -> dict:
        reason = None
        if not self.enabled:
            reason = "push_disabled"
        elif not self.sdk_available:
            reason = "fcm_sdk_missing"
        elif not self.provider_configured:
            reason = self.configuration_reason or "fcm_not_configured"
        return {
            "available": reason is None,
            "provider": PUSH_PROVIDER,
            "reason": reason,
            # Public Firebase web config + VAPID are frontend settings, never here.
            "display_strategy": DISPLAY_STRATEGY,
            "ttl_seconds": self.ttl_seconds,
            "public_config_source": "frontend_env",
        }

    async def register_device(
        self, db: AsyncSession, context: Any, payload: dict
    ) -> dict:
        token = (payload.get("token") or "").strip()
        installation_id = (payload.get("installation_id") or "").strip()
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise HTTPException(status_code=422, detail="Invalid push token.")
        if not installation_id or len(installation_id) > MAX_INSTALLATION_ID_LENGTH:
            raise HTTPException(status_code=422, detail="Invalid installation id.")
        now = datetime.now(UTC)
        device = await self.repository.get_by_installation(
            db,
            organization_id=context.organization_id,
            staff_member_id=context.staff_member_id,
            installation_id=installation_id,
            for_update=True,
        )
        other = await self.repository.get_by_token(db, token, for_update=True)
        try:
            if device is not None:
                if other is not None and other.id != device.id:
                    await self.repository.revoke(db, other, reason="rebound", now=now)
                device.token = token
                device.platform = payload.get("platform") or device.platform
                device.environment = payload.get("environment") or device.environment
                device.user_agent = payload.get("user_agent") or device.user_agent
                device.is_active = True
                device.revoked_at = None
                device.revoked_reason = None
                device.last_seen_at = now
                device.updated_at = now
                await db.flush()
            else:
                if other is not None:
                    # Token was bound to a previous account/installation.
                    await self.repository.revoke(db, other, reason="rebound", now=now)
                device = await self.repository.create(
                    db,
                    organization_id=context.organization_id,
                    staff_member_id=context.staff_member_id,
                    user_account_id=context.account_id,
                    installation_id=installation_id,
                    token=token,
                    platform=payload.get("platform") or "web",
                    environment=payload.get("environment") or "dev",
                    user_agent=payload.get("user_agent"),
                    is_active=True,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409, detail="Device registration conflicted; retry."
            ) from exc
        await db.commit()
        return device_item(device)

    async def list_devices(self, db: AsyncSession, context: Any) -> list[dict]:
        devices = await self.repository.list_for_staff(
            db, context.staff_member_id, active_only=False
        )
        return [device_item(device) for device in devices]

    async def revoke_device(
        self, db: AsyncSession, context: Any, device_id: UUID, *, reason: str = "user_revoked"
    ) -> dict:
        device = await self.repository.get(db, device_id)
        if (
            device is None
            or device.user_account_id != context.account_id
            or device.staff_member_id != context.staff_member_id
        ):
            raise HTTPException(status_code=404, detail="Device not found.")
        await self.repository.revoke(db, device, reason=reason)
        await db.commit()
        return {"id": str(device.id), "revoked": True}

    async def revoke_account_devices(
        self, db: AsyncSession, user_account_id: UUID, *, reason: str
    ) -> int:
        count = await self.repository.revoke_for_account(db, user_account_id, reason=reason)
        await db.commit()
        return count

    async def revoke_all_devices(
        self, db: AsyncSession, context: Any, *, reason: str = "identity_change"
    ) -> dict:
        """Authenticated revoke-all for identity change/logout without the BFF cookie."""
        devices = await self.repository.list_for_staff(
            db, context.staff_member_id, active_only=True
        )
        revoked = 0
        for device in devices:
            if device.user_account_id != context.account_id:
                continue
            await self.repository.revoke(db, device, reason=reason)
            revoked += 1
        await db.commit()
        return {"revoked": revoked}

    async def device_eligible(
        self,
        db: AsyncSession,
        *,
        event: NotificationEvent,
        device: Any,
        now: datetime | None = None,
    ) -> bool:
        """Recheck priority/TTL/preferences/quiet hours for one device."""
        now = now or datetime.now(UTC)
        if not (self.enabled and self.provider_configured):
            return False
        if event.priority not in PUSH_PRIORITIES:
            return False
        if event.expires_at is not None and event.expires_at <= now:
            return False
        rows = await self.notifications.get_preferences(
            db,
            organization_id=event.organization_id,
            staff_member_id=device.staff_member_id,
        )
        pref_rows = {row.category: row for row in rows}
        category_row = pref_rows.get(event.category)
        browser = (
            category_row.browser
            if category_row is not None
            else preference_defaults(event.category)["browser"]
        )
        staff_row = pref_rows.get("all")
        desktop_alerts = bool(staff_row.desktop_alerts) if staff_row else False
        quiet = is_quiet_now(
            enabled=staff_row.quiet_hours_enabled if staff_row else False,
            start=staff_row.quiet_start if staff_row else None,
            end=staff_row.quiet_end if staff_row else None,
            timezone_name=staff_row.timezone if staff_row else None,
            now=now,
        )
        return bool(browser and desktop_alerts and not quiet)

    async def enqueue_for_notification(
        self,
        db: AsyncSession,
        *,
        event: NotificationEvent,
        recipient_staff_ids: list[UUID],
    ) -> int:
        if not (self.enabled and self.provider_configured):
            return 0
        if event.priority not in PUSH_PRIORITIES:
            return 0  # P3 off by default
        now = datetime.now(UTC)
        if event.expires_at is not None and event.expires_at <= now:
            return 0
        devices = await self.repository.active_devices_for_staff_ids(
            db, recipient_staff_ids
        )
        if not devices:
            return 0
        expires_at = event.expires_at or (now + timedelta(seconds=self.ttl_seconds))
        queued = 0
        for device in devices:
            if not await self.device_eligible(db, event=event, device=device, now=now):
                continue
            await self.delivery.enqueue(
                db,
                channel=PUSH_CHANNEL,
                event_type=event.event_type,
                dedup_key=f"push:{event.id}:{device.id}",
                payload={
                    "push_device_id": str(device.id),
                    "notification_id": str(event.id),
                    "staff_member_id": str(device.staff_member_id),
                },
                organization_id=event.organization_id,
                facility_id=event.facility_id,
                recipient_staff_id=device.staff_member_id,
                max_attempts=self.max_attempts,
                expires_at=expires_at,
            )
            queued += 1
        return queued


def build_push_service(settings: Settings) -> PushService:
    return PushService(
        enabled=settings.FCM_ENABLED,
        provider_configured=push_configured(settings),
        sdk_available=push_sdk_available(),
        configuration_reason=push_configuration_reason(settings),
        ttl_seconds=settings.FCM_DEFAULT_TTL_SECONDS,
    )
