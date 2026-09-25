"""Committed workflow transitions → notifications.

Call these *inside* the owning transaction, before commit, so the notification
and its outbox job persist atomically with the transition. Only the four
transitions agreed in BE03 are wired; anything else stays silent.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ....models.care import Appointment, Encounter, QueueEntry
from ....models.communication import JOB_CHANNEL_REALTIME, NotificationEvent
from ..delivery.repository import DeliveryJobRepository
from .constants import (
    EVENT_APPOINTMENT_RESCHEDULED,
    EVENT_CATALOG,
    EVENT_CONSULTATION_COMPLETED,
    EVENT_QUEUE_READY,
    EVENT_TOKEN_ASSIGNED,
    NOTIFICATION_COALESCE_SECONDS,
    NOTIFICATION_TTL_MINUTES,
)
from .recipients import (
    acting_actor,
    exclude_actor,
    facility_role_staff_ids,
    practitioner_staff_ids,
)
from .repository import NotificationRepository

REPOSITORY = NotificationRepository()
DELIVERY = DeliveryJobRepository()
RECEPTION_ROLES = ("receptionist", "facility_operator", "billing_staff")


async def _enqueue_realtime(
    db: AsyncSession, *, event: NotificationEvent, staff_member_ids: list[UUID]
) -> None:
    for staff_id in staff_member_ids:
        await DELIVERY.enqueue(
            db,
            channel=JOB_CHANNEL_REALTIME,
            event_type=event.event_type,
            dedup_key=f"notification:{event.id}:{staff_id}:{event.revision}",
            payload={
                "notification_id": str(event.id),
                "staff_member_id": str(staff_id),
            },
            organization_id=event.organization_id,
            facility_id=event.facility_id,
            recipient_staff_id=staff_id,
            expires_at=event.expires_at,
        )


async def record_notification(
    db: AsyncSession,
    *,
    event_type: str,
    organization_id: UUID,
    facility_id: UUID | None,
    actor_user_id: UUID | None,
    recipient_staff_ids: list[UUID],
    title: str,
    context: str | None = None,
    location: str | None = None,
    task_type: str | None = None,
    task_id: str | None = None,
    task_state: str = "none",
    action_kind: str | None = None,
    action_label: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    coalesce_key: str | None = None,
    dedup_key: str,
    expires_at: datetime | None = None,
) -> NotificationEvent | None:
    recipients = list(dict.fromkeys(recipient_staff_ids))
    if not recipients:
        return None  # never a broad all-staff broadcast
    catalog = EVENT_CATALOG[event_type]
    actor_name, actor_role = await acting_actor(db, actor_user_id)
    now = datetime.now(UTC)
    event, created = await REPOSITORY.create_event(
        db,
        values={
            "organization_id": organization_id,
            "facility_id": facility_id,
            "event_type": event_type,
            "title": title,
            "category": catalog["category"],
            "priority": catalog["priority"],
            "kind": catalog["kind"],
            "dedup_key": dedup_key,
            "context": context,
            "location": location,
            "actor_user_id": actor_user_id,
            "actor_name": actor_name,
            "actor_role": actor_role,
            "task_type": task_type,
            "task_id": task_id,
            "task_state": task_state,
            "action_kind": action_kind,
            "action_label": action_label,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "coalesce_key": coalesce_key,
            "occurred_at": now,
            "updated_at": now,
            "expires_at": expires_at or now + timedelta(minutes=NOTIFICATION_TTL_MINUTES),
        },
    )
    if not created:
        return event  # idempotent replay: recipients/jobs already exist
    await REPOSITORY.add_recipients(
        db, event=event, staff_member_ids=recipients, facility_id=facility_id
    )
    await _enqueue_realtime(db, event=event, staff_member_ids=recipients)
    await _enqueue_push(db, event=event, staff_member_ids=recipients)
    return event


async def _enqueue_push(
    db: AsyncSession, *, event: NotificationEvent, staff_member_ids: list[UUID]
) -> None:
    """P1/P2 only; the push service applies preference/quiet-hours gating."""
    from ....core.config import settings
    from ..push.service import build_push_service

    push = build_push_service(settings)
    await push.enqueue_for_notification(
        db, event=event, recipient_staff_ids=staff_member_ids
    )


async def emit_queue_token_assigned(
    db: AsyncSession, *, entry: QueueEntry, actor_user_id: UUID | None
) -> NotificationEvent | None:
    recipients = await exclude_actor(
        db,
        await practitioner_staff_ids(
            db, organization_id=entry.organization_id, practitioner_id=entry.practitioner_id
        ),
        actor_user_id,
    )
    return await record_notification(
        db,
        event_type=EVENT_TOKEN_ASSIGNED,
        organization_id=entry.organization_id,
        facility_id=entry.facility_id,
        actor_user_id=actor_user_id,
        recipient_staff_ids=recipients,
        title="Patient checked in",
        context=f"Token T-{entry.token_number} assigned",
        location=f"T-{entry.token_number}",
        task_type="queue_entry",
        task_id=str(entry.id),
        task_state="none",
        resource_type="queue_entry",
        resource_id=str(entry.id),
        coalesce_key=f"queue:{entry.id}",
        dedup_key=f"queue.token_assigned:{entry.id}",
    )


async def emit_queue_ready(
    db: AsyncSession, *, entry: QueueEntry, actor_user_id: UUID | None
) -> NotificationEvent | None:
    recipients = await exclude_actor(
        db,
        await practitioner_staff_ids(
            db, organization_id=entry.organization_id, practitioner_id=entry.practitioner_id
        ),
        actor_user_id,
    )
    if not recipients:
        return None
    now = datetime.now(UTC)
    context = f"Token T-{entry.token_number} is ready for consultation"

    # Coalesce arrival+ready for the same unread single recipient into one item.
    if len(recipients) == 1:
        existing = await REPOSITORY.find_coalescible(
            db,
            coalesce_key=f"queue:{entry.id}",
            staff_member_id=recipients[0],
            event_types=(EVENT_TOKEN_ASSIGNED,),
            since=now - timedelta(seconds=NOTIFICATION_COALESCE_SECONDS),
        )
        if existing is not None:
            event, _recipient = existing
            catalog = EVENT_CATALOG[EVENT_QUEUE_READY]
            await REPOSITORY.bump_revision(
                db,
                event,
                values={
                    "event_type": EVENT_QUEUE_READY,
                    "priority": catalog["priority"],
                    "kind": catalog["kind"],
                    "title": "Patient ready",
                    "context": context,
                    "task_state": "awaiting",
                    "action_kind": "open",
                    "action_label": "Open queue",
                    "actor_user_id": actor_user_id,
                    "updated_at": now,
                },
            )
            actor_name, actor_role = await acting_actor(db, actor_user_id)
            event.actor_name, event.actor_role = actor_name, actor_role
            await _enqueue_realtime(db, event=event, staff_member_ids=recipients)
            return event

    return await record_notification(
        db,
        event_type=EVENT_QUEUE_READY,
        organization_id=entry.organization_id,
        facility_id=entry.facility_id,
        actor_user_id=actor_user_id,
        recipient_staff_ids=recipients,
        title="Patient ready",
        context=context,
        location=f"T-{entry.token_number}",
        task_type="queue_entry",
        task_id=str(entry.id),
        task_state="awaiting",
        action_kind="open",
        action_label="Open queue",
        resource_type="queue_entry",
        resource_id=str(entry.id),
        coalesce_key=f"queue:{entry.id}",
        dedup_key=f"queue.ready:{entry.id}",
    )


async def emit_appointment_same_day_rescheduled(
    db: AsyncSession,
    *,
    appointment: Appointment,
    actor_user_id: UUID | None,
    new_start_label: str,
    same_day: bool,
) -> NotificationEvent | None:
    if not same_day:
        return None
    recipients = await exclude_actor(
        db,
        await practitioner_staff_ids(
            db,
            organization_id=appointment.organization_id,
            practitioner_id=appointment.practitioner_id,
        ),
        actor_user_id,
    )
    return await record_notification(
        db,
        event_type=EVENT_APPOINTMENT_RESCHEDULED,
        organization_id=appointment.organization_id,
        facility_id=appointment.facility_id,
        actor_user_id=actor_user_id,
        recipient_staff_ids=recipients,
        title="Appointment rescheduled",
        context=f"New time: {new_start_label}",
        task_type="appointment",
        task_id=str(appointment.id),
        task_state="none",
        resource_type="appointment",
        resource_id=str(appointment.id),
        dedup_key=f"appointment.rescheduled:{appointment.id}:v{appointment.version}",
    )


async def emit_consultation_completed(
    db: AsyncSession, *, encounter: Encounter, actor_user_id: UUID | None
) -> NotificationEvent | None:
    recipients = await exclude_actor(
        db,
        await facility_role_staff_ids(
            db,
            organization_id=encounter.organization_id,
            facility_id=encounter.facility_id,
            roles=RECEPTION_ROLES,
        ),
        actor_user_id,
    )
    if not recipients:
        return None
    context = "Consultation completed; proceed to checkout"
    if encounter.queue_entry_id:
        queue = await db.get(QueueEntry, encounter.queue_entry_id)
        if queue:
            context = f"Token T-{queue.token_number} completed; proceed to checkout"
    event = await record_notification(
        db,
        event_type=EVENT_CONSULTATION_COMPLETED,
        organization_id=encounter.organization_id,
        facility_id=encounter.facility_id,
        actor_user_id=actor_user_id,
        recipient_staff_ids=recipients,
        title="Consultation completed",
        context=context,
        task_type="encounter",
        task_id=str(encounter.id),
        task_state="none",
        resource_type="encounter",
        resource_id=str(encounter.id),
        dedup_key=f"consultation.completed:{encounter.id}",
    )
    if event is not None and encounter.queue_entry_id:
        for stale in await REPOSITORY.actionable_by_coalesce(
            db, coalesce_key=f"queue:{encounter.queue_entry_id}"
        ):
            await REPOSITORY.supersede(db, stale, event)
    return event
