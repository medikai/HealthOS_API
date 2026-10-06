"""Patient in-app notifications, preferences and push device binding.

In-app rows are inserted in the caller's transaction (no lost events around a
commit). Push is truthful: credentials missing means push is reported
unavailable and in-app still works. No staff_member_id is ever fabricated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ...core.config import settings
from ...models.communication import (
    JOB_CHANNEL_REALTIME,
    PatientNotification,
    PatientNotificationPreference,
    PatientPushDevice,
)
from ..communication.delivery.repository import DeliveryJobRepository

DELIVERY = DeliveryJobRepository()
REALTIME_EVENT_PATIENT_NOTIFICATION = "patient_notification"


async def _enqueue_realtime(
    db: AsyncSession,
    *,
    notification_id: UUID,
    account_id: UUID,
    kind: str,
    organization_id: UUID | None,
    facility_id: UUID | None,
) -> None:
    """Enqueue the realtime invalidation in the caller's transaction.

    No commit here: the outbox row commits with the originating workflow.
    The payload is opaque (UUIDs/kind); detail is refetched over HTTP.
    """
    await DELIVERY.enqueue(
        db,
        channel=JOB_CHANNEL_REALTIME,
        event_type=REALTIME_EVENT_PATIENT_NOTIFICATION,
        dedup_key=f"patient-notification:{notification_id}:realtime",
        payload={
            "notification_id": str(notification_id),
            "patient_account_id": str(account_id),
            "kind": kind,
            "organization_id": str(organization_id) if organization_id else None,
        },
        organization_id=organization_id,
        facility_id=facility_id,
        recipient_patient_id=account_id,
    )


async def create_patient_notification(
    db: AsyncSession,
    *,
    account_id: UUID,
    kind: str,
    title: str,
    body: str,
    organization_id: UUID | None = None,
    facility_id: UUID | None = None,
    category: str = "general",
    deep_link: str | None = None,
    dedup_key: str | None = None,
) -> bool:
    """Create one in-app notification and its realtime outbox job.

    Returns True when newly created. Idempotent when ``dedup_key`` is supplied:
    a replay (retry, duplicate decision) inserts nothing and enqueues nothing.
    Callers must NOT commit; the insert and outbox row commit with the
    originating workflow transaction.
    """
    if dedup_key is None:
        notification = PatientNotification(
            patient_account_id=account_id,
            kind=kind,
            title=title[:160],
            body=body,
            organization_id=organization_id,
            facility_id=facility_id,
            category=category,
            deep_link=deep_link,
        )
        db.add(notification)
        await db.flush()
        created_id = notification.id
    else:
        statement = (
            pg_insert(PatientNotification)
            .values(
                id=uuid7(),
                patient_account_id=account_id,
                kind=kind,
                title=title[:160],
                body=body,
                organization_id=organization_id,
                facility_id=facility_id,
                category=category,
                deep_link=deep_link,
                dedup_key=dedup_key,
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(PatientNotification.id)
        )
        row = (await db.execute(statement)).first()
        if row is None:
            return False
        created_id = row[0]
    await _enqueue_realtime(
        db,
        notification_id=created_id,
        account_id=account_id,
        kind=kind,
        organization_id=organization_id,
        facility_id=facility_id,
    )
    return True


async def list_notifications(
    db: AsyncSession,
    *,
    account_id: UUID,
    unread_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PatientNotification], int, int]:
    query = select(PatientNotification).where(
        PatientNotification.patient_account_id == account_id,
        (PatientNotification.expires_at.is_(None))
        | (PatientNotification.expires_at > datetime.now(UTC)),
    )
    if unread_only:
        query = query.where(PatientNotification.read_at.is_(None))
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    unread = await db.scalar(
        select(func.count(PatientNotification.id)).where(
            PatientNotification.patient_account_id == account_id,
            PatientNotification.read_at.is_(None),
        )
    ) or 0
    rows = (
        await db.scalars(
            query.order_by(PatientNotification.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return list(rows), int(total), int(unread)


async def mark_read(
    db: AsyncSession, *, account_id: UUID, notification_id: UUID
) -> PatientNotification | None:
    notification = await db.scalar(
        select(PatientNotification).where(
            PatientNotification.id == notification_id,
            PatientNotification.patient_account_id == account_id,
        )
    )
    if notification is None:
        return None
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(notification)
    return notification


async def get_preferences(
    db: AsyncSession, *, account_id: UUID
) -> PatientNotificationPreference:
    preference = await db.scalar(
        select(PatientNotificationPreference).where(
            PatientNotificationPreference.patient_account_id == account_id
        )
    )
    if preference is None:
        preference = PatientNotificationPreference(patient_account_id=account_id)
        db.add(preference)
        await db.commit()
        await db.refresh(preference)
    return preference


async def update_preferences(
    db: AsyncSession, *, account_id: UUID, in_app: bool | None, push: bool | None
) -> PatientNotificationPreference:
    preference = await get_preferences(db, account_id=account_id)
    if in_app is not None:
        preference.in_app = in_app
    if push is not None:
        preference.push = push
    preference.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(preference)
    return preference


def push_status() -> dict[str, Any]:
    """Truthful push state: availability means delivery-ready, not merely configured.

    In-app notifications are always unaffected. A project id alone is not
    delivery-ready; server credentials and the SDK must both be present, and
    live delivery still requires end-to-end verification.
    """
    from pathlib import Path

    from ..communication.push.service import (
        push_configuration_reason,
        push_sdk_available,
    )

    sdk_available = push_sdk_available()
    credentials_present = bool(settings.FIREBASE_CREDENTIALS_JSON) or bool(
        settings.FIREBASE_SERVICE_ACCOUNT_FILE
        and Path(settings.FIREBASE_SERVICE_ACCOUNT_FILE).exists()
    )
    configured = bool(settings.FCM_ENABLED) and credentials_present
    delivery_ready = configured and sdk_available
    if not settings.FCM_ENABLED:
        reason = "push_disabled"
    elif not sdk_available:
        reason = "fcm_sdk_missing"
    elif not credentials_present:
        reason = push_configuration_reason(settings) or "fcm_credentials_missing"
    else:
        reason = None
    return {
        "available": delivery_ready,
        "configured": configured,
        "delivery_ready": delivery_ready,
        "provider": "fcm",
        "reason": reason,
        "in_app_unaffected": True,
    }


async def register_device(
    db: AsyncSession,
    *,
    account_id: UUID,
    installation_id: str,
    token: str,
    platform: str,
    user_agent: str | None,
) -> PatientPushDevice:
    now = datetime.now(UTC)
    device = await db.scalar(
        select(PatientPushDevice).where(
            PatientPushDevice.patient_account_id == account_id,
            PatientPushDevice.installation_id == installation_id,
        )
    )
    if device is None:
        device = PatientPushDevice(
            patient_account_id=account_id,
            installation_id=installation_id,
            token=token,
            platform=platform,
            user_agent=user_agent,
            is_active=True,
            last_seen_at=now,
        )
        db.add(device)
    else:
        device.token = token
        device.platform = platform
        device.user_agent = user_agent
        device.is_active = True
        device.revoked_at = None
        device.revoked_reason = None
        device.last_seen_at = now
        device.updated_at = now
    await db.commit()
    await db.refresh(device)
    return device


async def revoke_device(
    db: AsyncSession, *, account_id: UUID, device_id: UUID, reason: str = "unregistered"
) -> bool:
    result = await db.execute(
        update(PatientPushDevice)
        .where(
            PatientPushDevice.id == device_id,
            PatientPushDevice.patient_account_id == account_id,
            PatientPushDevice.is_active.is_(True),
        )
        .values(is_active=False, revoked_at=datetime.now(UTC), revoked_reason=reason[:64])
    )
    await db.commit()
    return bool(result.rowcount)


async def revoke_all_devices(
    db: AsyncSession, *, account_id: UUID, reason: str = "account_change"
) -> int:
    result = await db.execute(
        update(PatientPushDevice)
        .where(
            PatientPushDevice.patient_account_id == account_id,
            PatientPushDevice.is_active.is_(True),
        )
        .values(is_active=False, revoked_at=datetime.now(UTC), revoked_reason=reason[:64])
    )
    return int(result.rowcount or 0)
