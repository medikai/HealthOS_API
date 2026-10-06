"""Disposable/local DB checks for patient realtime outbox and delivery.

Gated like the other patient DB tests: runs only with a distinct
``TEST_POSTGRES_ASYNC_URL`` or when ``HEALTHOS_ALLOW_LOCAL_DB_TESTS=1`` allows
the configured non-production local database. All rows are synthetic and
removed in a ``finally`` cleanup.
"""

import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.domains.communication.notifications.constants import (
    EVENT_APPOINTMENT_REQUEST_CREATED,
)
from src.app.domains.communication.realtime.providers.notify import (
    RealtimeNotificationProvider,
)
from src.app.domains.communication.realtime.repository import (
    PatientRealtimeChannelRepository,
)
from src.app.domains.communication.utils.channels import patient_user_channel
from src.app.domains.patient_portal import appointments as appt_service
from src.app.domains.patient_portal import notifications as notify_service
from src.app.models.communication import (
    DeliveryJob,
    NotificationEvent,
    NotificationRecipient,
    PatientNotification,
    PatientRealtimeChannelState,
)
from src.app.models.identity import UserAccount
from src.app.models.organization import StaffAssignment, StaffMember
from tests.test_patient_domain_db import _future_monday, _hard_cleanup, _seed
from tests.test_patient_portal_db import ACTIVE_DB_URL, _upgrade_schema, requires_db


class _RecordingPublisher:
    namespace = "dev"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def configured(self) -> bool:
        return True

    async def publish(self, *, channel, event_type, payload):
        self.calls.append(
            {"channel": channel, "event_type": event_type, "payload": payload}
        )
        return {"provider": "ably"}


@requires_db
def test_patient_realtime_db_semantics():
    assert ACTIVE_DB_URL is not None
    _upgrade_schema(ACTIVE_DB_URL)

    async def _exercise() -> None:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        org_ids = []
        account_ids = []
        patient_ids = []
        person_ids = []
        staff_user_ids = []
        staff_ids = []
        try:
            async with factory() as db:
                ctx = await _seed(db, prefix="SYNRTP")
                org = ctx["org"]
                facility = ctx["facility"]
                account = ctx["account"]
                org_ids.append(org.id)
                account_ids.append(account.id)
                patient_ids.append(ctx["patient"].id)
                person_ids.append(ctx["person"].id)
                staff_user_ids.append(ctx["staff_account"].id)

                desk_account = UserAccount(
                    logto_user_id=f"local:rt-{uuid4().hex}",
                    email=f"rt-{uuid4().hex[:8]}@example.com",
                    display_name="RT Desk",
                )
                db.add(desk_account)
                await db.flush()
                desk = StaffMember(organization_id=org.id, user_account_id=desk_account.id)
                db.add(desk)
                await db.flush()
                db.add(
                    StaffAssignment(
                        staff_member_id=desk.id,
                        facility_id=facility.id,
                        role_code="receptionist",
                    )
                )
                await db.commit()
                staff_ids.append(desk.id)
                staff_user_ids.append(desk_account.id)

                # 1) In-app notification and its realtime outbox job commit
                # together; a dedup replay inserts neither twice.
                assert (
                    await notify_service.create_patient_notification(
                        db,
                        account_id=account.id,
                        kind="appointment_confirmed",
                        title="Confirmed",
                        body="Your appointment is confirmed.",
                        organization_id=org.id,
                        facility_id=facility.id,
                        dedup_key="rt-db-1",
                    )
                    is True
                )
                await db.commit()
                notification = await db.scalar(
                    select(PatientNotification).where(
                        PatientNotification.dedup_key == "rt-db-1"
                    )
                )
                assert notification is not None
                job_dedup = f"patient-notification:{notification.id}:realtime"
                job = await db.scalar(
                    select(DeliveryJob).where(DeliveryJob.dedup_key == job_dedup)
                )
                assert job is not None
                assert job.channel == "realtime"
                assert job.recipient_patient_id == account.id
                assert job.payload["kind"] == "appointment_confirmed"

                assert (
                    await notify_service.create_patient_notification(
                        db,
                        account_id=account.id,
                        kind="appointment_confirmed",
                        title="Confirmed",
                        body="Your appointment is confirmed.",
                        organization_id=org.id,
                        facility_id=facility.id,
                        dedup_key="rt-db-1",
                    )
                    is False
                )
                await db.commit()
                job_count = await db.scalar(
                    select(func.count())
                    .select_from(DeliveryJob)
                    .where(DeliveryJob.dedup_key == job_dedup)
                )
                assert job_count == 1

                # 2) Account-level generation lifecycle.
                repository = PatientRealtimeChannelRepository()
                state = await repository.get_or_create_patient(
                    db, patient_account_id=account.id
                )
                assert state.generation == 1
                assert (
                    await repository.bump_patient_generation(
                        db, patient_account_id=account.id
                    )
                    == 2
                )
                await db.commit()
                assert (
                    await repository.bump_patient_generation(
                        db, patient_account_id=account.id
                    )
                    == 3
                )
                await db.commit()

                # 3) Worker provider publishes to the current generation with an
                # opaque payload (UUIDs/kind only, no clinical detail).
                publisher = _RecordingPublisher()
                provider = RealtimeNotificationProvider(
                    publisher=publisher, session_factory=factory
                )
                result = await provider.deliver(
                    SimpleNamespace(
                        payload={
                            "notification_id": str(notification.id),
                            "patient_account_id": str(account.id),
                        },
                        recipient_patient_id=account.id,
                    )
                )
                assert result["published"] is True
                call = publisher.calls[-1]
                assert call["channel"] == patient_user_channel(
                    publisher.namespace, account.id, 3
                )
                assert call["event_type"] == "patient_notification"
                assert call["payload"]["recipient_patient_id"] == str(account.id)
                assert set(call["payload"]["data"]) == {
                    "notification_id",
                    "kind",
                    "organization_uuid",
                }
                assert call["payload"]["data"]["organization_uuid"] == str(org.id)

                # 4) A pending appointment request notifies facility reception
                # in the same transaction and enqueues the staff realtime job.
                day = _future_monday()
                ist = ZoneInfo("Asia/Kolkata")
                start = datetime.combine(day, time(10, 0), tzinfo=ist)
                end = start + timedelta(minutes=30)
                request, replayed = await appt_service.create_new_request(
                    db,
                    account=account,
                    link=ctx["link"],
                    facility=facility,
                    practitioner=ctx["practitioner"],
                    start=start,
                    end=end,
                    reason_code=None,
                    reason_text="Realtime check",
                    idempotency_key="rt-req-1",
                )
                assert replayed is False and request.status == "requested"
                event = await db.scalar(
                    select(NotificationEvent).where(
                        NotificationEvent.event_type
                        == EVENT_APPOINTMENT_REQUEST_CREATED,
                        NotificationEvent.resource_id == str(request.id),
                    )
                )
                assert event is not None
                assert event.kind == "task" and event.task_state == "awaiting"
                recipient = await db.scalar(
                    select(NotificationRecipient).where(
                        NotificationRecipient.notification_id == event.id,
                        NotificationRecipient.staff_member_id == desk.id,
                    )
                )
                assert recipient is not None
                staff_job = await db.scalar(
                    select(DeliveryJob).where(
                        DeliveryJob.dedup_key
                        == f"notification:{event.id}:{desk.id}:{event.revision}"
                    )
                )
                assert staff_job is not None
                assert staff_job.recipient_staff_id == desk.id

                # Idempotent request replay does not notify twice.
                _, replayed_again = await appt_service.create_new_request(
                    db,
                    account=account,
                    link=ctx["link"],
                    facility=facility,
                    practitioner=ctx["practitioner"],
                    start=start,
                    end=end,
                    reason_code=None,
                    reason_text="Realtime check",
                    idempotency_key="rt-req-1",
                )
                assert replayed_again is True
                event_count = await db.scalar(
                    select(func.count())
                    .select_from(NotificationEvent)
                    .where(
                        NotificationEvent.event_type
                        == EVENT_APPOINTMENT_REQUEST_CREATED,
                        NotificationEvent.resource_id == str(request.id),
                    )
                )
                assert event_count == 1

                # Rollback loses the notification and its outbox row together.
                await notify_service.create_patient_notification(
                    db,
                    account_id=account.id,
                    kind="appointment_rejected",
                    title="Rolled back",
                    body="Rolled back.",
                    organization_id=org.id,
                    facility_id=facility.id,
                    dedup_key="rt-db-rollback",
                )
                await db.rollback()
                rolled_back_notification = await db.scalar(
                    select(func.count())
                    .select_from(PatientNotification)
                    .where(PatientNotification.dedup_key == "rt-db-rollback")
                )
                rolled_back_job = await db.scalar(
                    select(func.count())
                    .select_from(DeliveryJob)
                    .where(DeliveryJob.payload["kind"].astext == "appointment_rejected")
                )
                assert rolled_back_notification == 0
                assert rolled_back_job == 0
        finally:
            async with factory() as db:
                await db.execute(
                    delete(DeliveryJob).where(
                        or_(
                            DeliveryJob.organization_id.in_(org_ids),
                            DeliveryJob.recipient_patient_id.in_(account_ids),
                            DeliveryJob.recipient_staff_id.in_(staff_ids),
                        )
                    )
                )
                await db.execute(
                    delete(NotificationRecipient).where(
                        NotificationRecipient.organization_id.in_(org_ids)
                    )
                )
                await db.execute(
                    delete(NotificationEvent).where(
                        NotificationEvent.organization_id.in_(org_ids)
                    )
                )
                await db.execute(
                    delete(PatientRealtimeChannelState).where(
                        PatientRealtimeChannelState.patient_account_id.in_(account_ids)
                    )
                )
                await db.execute(
                    delete(PatientNotification).where(
                        PatientNotification.patient_account_id.in_(account_ids)
                    )
                )
                await db.execute(
                    delete(StaffAssignment).where(
                        StaffAssignment.staff_member_id.in_(staff_ids)
                    )
                )
                await db.execute(
                    delete(StaffMember).where(StaffMember.id.in_(staff_ids))
                )
                await db.commit()
                await _hard_cleanup(
                    db,
                    org_ids=org_ids,
                    account_ids=account_ids,
                    patient_ids=patient_ids,
                    person_ids=person_ids,
                    staff_user_ids=staff_user_ids,
                )
            await engine.dispose()

    asyncio.run(_exercise())
