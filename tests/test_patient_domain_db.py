"""DB-backed checks for PA-BE02 domain slices (gated like PA-BE01 DB tests).

Run with a distinct ``TEST_POSTGRES_ASYNC_URL`` or
``HEALTHOS_ALLOW_LOCAL_DB_TESTS=1`` for the configured non-production local DB.
All rows are synthetic and removed in a finally cleanup.
"""

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.availability import rules_from_facility_schedule
from src.app.domains.patient_auth.links import PatientLinkService
from src.app.domains.patient_auth.phone import normalize_phone
from src.app.domains.patient_portal import appointments as appt_service
from src.app.domains.patient_portal import billing as billing_service
from src.app.domains.patient_portal import notifications as notify_service
from src.app.domains.patient_portal import records as record_service
from src.app.models.billing import Invoice, Payment, Receipt
from src.app.models.care import (
    Appointment,
    AppointmentRequest,
    Encounter,
    Practitioner,
    PractitionerAvailabilityRule,
    RecordRelease,
    Vital,
)
from src.app.models.communication import (
    DeliveryJob,
    NotificationEvent,
    NotificationRecipient,
    PatientNotification,
)
from src.app.models.identity import (
    Patient,
    PatientPortalAccount,
    PatientRecordLink,
    Person,
    UserAccount,
)
from src.app.models.organization import (
    Facility,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffMember,
)
from tests.test_patient_portal_db import (
    ACTIVE_DB_URL,
    _cleanup,
    _upgrade_schema,
    requires_db,
)


def _future_monday() -> date:
    day = datetime.now(UTC).date() + timedelta(days=1)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


async def _seed(db, prefix: str):
    org = Organization(
        name=f"{prefix} {uuid4().hex[:8]}",
        code=f"{prefix}-{uuid4().hex[:10]}",
        portal_enabled=True,
        is_active=True,
    )
    db.add(org)
    await db.flush()
    facility = Facility(
        organization_id=org.id, name=f"{prefix} Main", code="MAIN", is_active=True
    )
    db.add(facility)
    await db.flush()
    schedule = FacilitySchedule(
        facility_id=facility.id,
        operating_start="08:00",
        operating_end="20:00",
        slot_interval_minutes=30,
        days_of_week='["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]',
        timezone="Asia/Kolkata",
    )
    db.add(schedule)
    await db.flush()
    practitioner = Practitioner(
        organization_id=org.id, person_name=f"Dr {prefix}", is_active=True
    )
    db.add(practitioner)
    await db.flush()
    rules = rules_from_facility_schedule(schedule, org.id, facility.id, practitioner.id)
    for rule in rules:
        rule.start_time = time(9, 0)
        rule.end_time = time(17, 0)
    for rule in rules:
        db.add(rule)
    await db.flush()
    person = Person(
        organization_id=org.id,
        first_name="Asha",
        last_name="Synthetic",
        phone=f"+91988{uuid4().int % 10**8:08d}",
        date_of_birth="1988-08-14",
    )
    db.add(person)
    await db.flush()
    patient = Patient(
        organization_id=org.id, person_id=person.id, mrn=f"MRN-DOM-{uuid4().hex[:8].upper()}"
    )
    db.add(patient)
    await db.flush()
    staff_account = UserAccount(
        logto_user_id=f"local:pa02-{uuid4().hex}",
        email=f"pa02-{uuid4().hex[:8]}@example.com",
        display_name="PA02 Staff",
    )
    db.add(staff_account)
    await db.flush()
    account_phone = f"+91999{uuid4().int % 10**8:08d}"
    account = PatientPortalAccount(phone=normalize_phone(account_phone), first_name="Asha")
    db.add(account)
    await db.flush()
    link_service = PatientLinkService(
        pepper="test-pepper", invitation_ttl_hours=1, invitation_max_ttl_hours=2
    )
    invitation, token = await link_service.create_invitation(
        db,
        organization=org,
        patient=patient,
        facility_id=facility.id,
        phone=account_phone,
        created_by_user_id=staff_account.id,
    )
    link = await link_service.activate(db, account=account, token=token)
    await db.commit()
    return {
        "link": link,
        "org": org,
        "facility": facility,
        "practitioner": practitioner,
        "patient": patient,
        "person": person,
        "account": account,
        "staff_account": staff_account,
        "invitation": invitation,
    }


async def _hard_cleanup(db, *, org_ids, account_ids, patient_ids, person_ids, staff_user_ids):
    if org_ids:
        # Communication rows created by record_notification/create_patient_notification
        # must go before the organization (no cascade on these FKs).
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
            delete(DeliveryJob).where(DeliveryJob.organization_id.in_(org_ids))
        )
        if staff_user_ids:
            await db.execute(
                delete(StaffAssignment).where(
                    StaffAssignment.staff_member_id.in_(
                        select(StaffMember.id).where(
                            StaffMember.user_account_id.in_(staff_user_ids)
                        )
                    )
                )
            )
            await db.execute(
                delete(StaffMember).where(
                    StaffMember.user_account_id.in_(staff_user_ids)
                )
            )
        await db.execute(
            delete(Receipt).where(Receipt.organization_id.in_(org_ids))
        )
        await db.execute(delete(Payment).where(Payment.organization_id.in_(org_ids)))
        await db.execute(delete(Invoice).where(Invoice.organization_id.in_(org_ids)))
        await db.execute(delete(RecordRelease).where(RecordRelease.organization_id.in_(org_ids)))
        await db.execute(delete(AppointmentRequest).where(AppointmentRequest.organization_id.in_(org_ids)))
        await db.execute(delete(Vital).where(Vital.encounter_id.in_(select(Encounter.id).where(Encounter.organization_id.in_(org_ids)))))
        await db.execute(delete(Appointment).where(Appointment.organization_id.in_(org_ids)))
        await db.execute(delete(Encounter).where(Encounter.organization_id.in_(org_ids)))
        await db.execute(delete(FacilitySchedule).where(FacilitySchedule.facility_id.in_(select(Facility.id).where(Facility.organization_id.in_(org_ids)))))
        await db.execute(delete(PractitionerAvailabilityRule).where(PractitionerAvailabilityRule.organization_id.in_(org_ids)))
        await db.execute(delete(Practitioner).where(Practitioner.organization_id.in_(org_ids)))
    if account_ids:
        await db.execute(
            delete(PatientNotification).where(
                PatientNotification.patient_account_id.in_(account_ids)
            )
        )
    await db.commit()
    await _cleanup(
        db,
        org_ids=org_ids,
        account_ids=account_ids,
        patient_ids=patient_ids,
        person_ids=person_ids,
        phone_hashes=set(),
    )
    if staff_user_ids:
        await db.execute(delete(UserAccount).where(UserAccount.id.in_(staff_user_ids)))
        await db.commit()


@requires_db
def test_patient_domain_workflow_db_semantics():
    assert ACTIVE_DB_URL is not None
    _upgrade_schema(ACTIVE_DB_URL)

    async def _exercise() -> None:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        org_ids: list[UUID] = []
        account_ids: list[UUID] = []
        patient_ids: list[UUID] = []
        person_ids: list[UUID] = []
        staff_user_ids: list[UUID] = []
        try:
            async with factory() as db:
                ctx = await _seed(db, prefix="SYNTPA2")
                org_ids.append(ctx["org"].id)
                account_ids.append(ctx["account"].id)
                patient_ids.append(ctx["patient"].id)
                person_ids.append(ctx["person"].id)
                staff_user_ids.append(ctx["staff_account"].id)
                org = ctx["org"]
                facility = ctx["facility"]
                practitioner = ctx["practitioner"]
                patient = ctx["patient"]
                account = ctx["account"]
                from zoneinfo import ZoneInfo

                day = _future_monday()
                ist = ZoneInfo("Asia/Kolkata")
                start = datetime.combine(day, time(10, 0), tzinfo=ist)
                end = start + timedelta(minutes=30)

                # Idempotent request creation: replay same payload, reject a
                # different payload under the same key.
                request, replayed = await appt_service.create_new_request(
                    db,
                    account=account,
                    link=ctx["link"],
                    facility=facility,
                    practitioner=practitioner,
                    start=start,
                    end=end,
                    reason_code=None,
                    reason_text="First",
                    idempotency_key="demo-key-1",
                )
                assert replayed is False and request.status == "requested"
                same, replayed = await appt_service.create_new_request(
                    db,
                    account=account,
                    link=ctx["link"],
                    facility=facility,
                    practitioner=practitioner,
                    start=start,
                    end=end,
                    reason_code=None,
                    reason_text="First",
                    idempotency_key="demo-key-1",
                )
                assert replayed is True and same.id == request.id
                with pytest.raises(appt_service.IdempotencyConflict):
                    await appt_service.create_new_request(
                        db,
                        account=account,
                        link=ctx["link"],
                        facility=facility,
                        practitioner=practitioner,
                        start=start,
                        end=end,
                        reason_code=None,
                        reason_text="Different",
                        idempotency_key="demo-key-1",
                    )
                # Approval revalidates and creates the appointment.
                approved = await appt_service.approve_request(
                    db, request=request, actor_user_id=ctx["staff_account"].id
                )
                assert approved.status == "approved" and approved.appointment_id
                appointment = await db.get(Appointment, approved.appointment_id)
                assert appointment.status == "booked"
                assert appt_service.patient_appointment_label(appointment.status) == "confirmed"

                # Alternative proposal -> patient accept validates again.
                second, _ = await appt_service.create_new_request(
                    db,
                    account=account,
                    link=ctx["link"],
                    facility=facility,
                    practitioner=practitioner,
                    start=start + timedelta(hours=1),
                    end=start + timedelta(hours=1, minutes=30),
                    reason_code=None,
                    reason_text=None,
                    idempotency_key="demo-key-2",
                )
                alt_start = start + timedelta(hours=2)
                proposed = await appt_service.propose_alternative(
                    db,
                    request=second,
                    actor_user_id=ctx["staff_account"].id,
                    start=alt_start,
                    end=alt_start + timedelta(minutes=30),
                    resource_id=None,
                    note="earlier",
                )
                assert proposed.status == "alternative_proposed"
                accepted = await appt_service.respond_to_alternative(
                    db, request=proposed, account=account, accept=True
                )
                assert accepted.status == "approved"

                # Withdraw and lazy expiry.
                third, _ = await appt_service.create_new_request(
                    db,
                    account=account,
                    link=ctx["link"],
                    facility=facility,
                    practitioner=practitioner,
                    start=start + timedelta(hours=3),
                    end=start + timedelta(hours=3, minutes=30),
                    reason_code=None,
                    reason_text=None,
                    idempotency_key="demo-key-3",
                )
                withdrawn = await appt_service.withdraw_request(
                    db, request=third, account=account
                )
                assert withdrawn.status == "withdrawn"
                fourth, _ = await appt_service.create_new_request(
                    db,
                    account=account,
                    link=ctx["link"],
                    facility=facility,
                    practitioner=practitioner,
                    start=start + timedelta(hours=4),
                    end=start + timedelta(hours=4, minutes=30),
                    reason_code=None,
                    reason_text=None,
                    idempotency_key="demo-key-4",
                )
                fourth.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.commit()
                await appt_service.expire_stale_requests(db, account_id=account.id)
                await db.refresh(fourth)
                assert fourth.status == "expired"

                # Record release snapshot immutability + versioning.
                encounter = Encounter(
                    organization_id=org.id,
                    facility_id=facility.id,
                    patient_id=patient.id,
                    practitioner_id=practitioner.id,
                    status="completed",
                    started_at=datetime.now(UTC) - timedelta(hours=2),
                    completed_at=datetime.now(UTC) - timedelta(hours=1),
                )
                db.add(encounter)
                await db.flush()
                reloaded = await _seed_link(db, account)
                assert reloaded is not None
                release = await record_service.create_release(
                    db,
                    actor_user_id=ctx["staff_account"].id,
                    organization_id=org.id,
                    facility_id=facility.id,
                    patient_id=patient.id,
                    encounter_id=encounter.id,
                    resource_type="encounter_summary",
                    resource_id=encounter.id,
                    snapshot={"summary": "Released v1"},
                )
                await db.commit()
                assert release.version == 1
                with pytest.raises(record_service.ActiveReleaseExists):
                    await record_service.create_release(
                        db,
                        actor_user_id=ctx["staff_account"].id,
                        organization_id=org.id,
                        facility_id=facility.id,
                        patient_id=patient.id,
                        encounter_id=encounter.id,
                        resource_type="encounter_summary",
                        resource_id=encounter.id,
                        snapshot={"summary": "silent overwrite"},
                    )
                await record_service.revoke_release(
                    db, release=release, actor_user_id=ctx["staff_account"].id, reason="amend"
                )
                await db.commit()
                second_release = await record_service.create_release(
                    db,
                    actor_user_id=ctx["staff_account"].id,
                    organization_id=org.id,
                    facility_id=facility.id,
                    patient_id=patient.id,
                    encounter_id=encounter.id,
                    resource_type="encounter_summary",
                    resource_id=encounter.id,
                    snapshot={"summary": "Released v2"},
                )
                await db.commit()
                assert second_release.version == 2

                # Billing discount -> received payment -> stable receipt.
                invoice = Invoice(
                    organization_id=org.id,
                    facility_id=facility.id,
                    encounter_id=encounter.id,
                    patient_id=patient.id,
                    amount_minor=10000,
                    currency="INR",
                    created_by_user_id=ctx["staff_account"].id,
                )
                db.add(invoice)
                await db.commit()
                discounted = await billing_service.apply_discount(
                    db,
                    invoice=invoice,
                    actor_user_id=ctx["staff_account"].id,
                    kind="percentage",
                    bp=1000,
                    fixed_minor=None,
                    reason="Demo discount",
                )
                assert discounted.gross_amount_minor == 10000
                assert discounted.discount_minor == 1000
                assert discounted.amount_minor == 9000
                # Re-discount is allowed until a payment exists; snapshot is replaced.
                rediscounted = await billing_service.apply_discount(
                    db,
                    invoice=invoice,
                    actor_user_id=ctx["staff_account"].id,
                    kind="fixed",
                    bp=None,
                    fixed_minor=100,
                    reason="second review",
                )
                assert rediscounted.discount_minor == 100
                assert rediscounted.amount_minor == 9900
                payment = Payment(
                    organization_id=org.id,
                    facility_id=facility.id,
                    invoice_id=invoice.id,
                    amount_minor=4000,
                    currency="INR",
                    idempotency_key=f"demo-payment-{uuid4().hex}",
                    created_by_user_id=ctx["staff_account"].id,
                    provenance="synthetic_demo",
                )
                db.add(payment)
                await db.commit()
                await db.refresh(payment)
                await db.refresh(invoice)
                receipt = await billing_service.get_or_create_receipt(
                    db, invoice=invoice, payment=payment
                )
                again = await billing_service.get_or_create_receipt(
                    db, invoice=invoice, payment=payment
                )
                assert receipt.id == again.id
                assert again.receipt_number.startswith("RCPT-")
                pdf = billing_service.render_receipt_pdf(
                    receipt=again,
                    invoice=invoice,
                    payment=payment,
                    facility_name="Synthetic Clinic",
                    patient={"display_name": "Asha Synthetic", "mrn": patient.mrn},
                    paid_to_date_minor=4000,
                    refunded_minor=0,
                    balance_minor=5900,
                )
                assert pdf.startswith(b"%PDF-1.4") and b"4000" in pdf and b"5900" in pdf

                # Notification dedup and read state.
                created = await notify_service.create_patient_notification(
                    db,
                    account_id=account.id,
                    kind="appointment_confirmed",
                    title="Confirmed",
                    body="Your appointment is confirmed.",
                    organization_id=org.id,
                    facility_id=facility.id,
                    dedup_key="demo-notify-1",
                )
                duplicate = await notify_service.create_patient_notification(
                    db,
                    account_id=account.id,
                    kind="appointment_confirmed",
                    title="Confirmed",
                    body="Your appointment is confirmed.",
                    organization_id=org.id,
                    facility_id=facility.id,
                    dedup_key="demo-notify-1",
                )
                await db.commit()
                assert created is True and duplicate is False
                from src.app.models.communication import PatientNotification

                dedup_count = await db.scalar(
                    select(func.count(PatientNotification.id)).where(
                        PatientNotification.dedup_key == "demo-notify-1"
                    )
                )
                assert dedup_count == 1
                rows, total, _unread = await notify_service.list_notifications(
                    db, account_id=account.id
                )
                assert total >= 1
                notification = next(
                    row for row in rows if row.dedup_key == "demo-notify-1"
                )
                marked = await notify_service.mark_read(
                    db, account_id=account.id, notification_id=notification.id
                )
                assert marked is not None and marked.read_at is not None
                # Cross-patient isolation: another account sees nothing.
                other_rows, other_total, _ = await notify_service.list_notifications(
                    db, account_id=uuid4()
                )
                assert other_total == 0 and other_rows == []
        finally:
            async with factory() as db:
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


async def _seed_link(db, account: PatientPortalAccount):
    return await db.scalar(
        select(PatientRecordLink).where(
            PatientRecordLink.patient_account_id == account.id,
            PatientRecordLink.status == "verified",
        )
    )
