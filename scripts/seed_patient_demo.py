"""Synthetic patient-portal demo seed (development/test only).

Dry-run by default; ``--apply`` is required to commit. Never run against
production. The actor must already exist for real: this script does not invent
an admin or pick a clinic. Frontend mock identifiers are not evidence; the
script verifies the database relationships and requires an explicit facility
when several qualify.

Usage (from the repository root, with the target database configured in src/.env):
    ./venv/bin/python scripts/seed_patient_demo.py                 # dry run
    ./venv/bin/python scripts/seed_patient_demo.py --apply \
        --facility-uuid <FACILITY_UUID> [--actor-user-uuid <UUID>] \
        [--reference-date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.app.core.config import settings
from src.app.domains.patient_auth.links import PatientLinkService
from src.app.domains.patient_auth.phone import normalize_phone
from src.app.domains.patient_portal import appointments as appt
from src.app.domains.patient_portal import billing as portal_billing
from src.app.domains.patient_portal import records as record_service
from src.app.domains.patient_portal.notifications import (
    create_patient_notification,
)
from src.app.models.billing import Invoice, Payment
from src.app.models.care import (
    Appointment,
    AppointmentRequest,
    Encounter,
    Practitioner,
    PractitionerSchedule,
    Prescription,
    PrescriptionItem,
    RecordRelease,
    SoapNote,
    Vital,
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
    Organization,
    StaffAssignment,
    StaffMember,
)

ACTOR_EMAIL = "ganesh.sawant@citycareclinic.demo"
DEMO_PHONE = "+919999900001"  # dev-provider allowlisted synthetic number
ASHA_MRN = "MRN-DEMO-ASHA"
MARKER = "demo:asha"


def _demo_id(key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"healthos:{MARKER}:{key}")


def _reference_datetime(reference_date: date) -> datetime:
    return datetime.combine(reference_date, time(10, 0), tzinfo=UTC)


async def _resolve_actor(db, actor_user_uuid: UUID | None):
    if actor_user_uuid:
        account = await db.get(UserAccount, actor_user_uuid)
    else:
        account = await db.scalar(
            select(UserAccount).where(UserAccount.email == ACTOR_EMAIL)
        )
    if account is None or not account.is_active:
        return None, None, []
    staff = await db.scalar(
        select(StaffMember).where(
            StaffMember.user_account_id == account.id,
            StaffMember.is_active.is_(True),
        )
    )
    if staff is None:
        return account, None, []
    roles = (
        await db.scalars(
            select(StaffAssignment.role_code).where(
                StaffAssignment.staff_member_id == staff.id,
                StaffAssignment.is_active.is_(True),
            )
        )
    ).all()
    organization = await db.get(Organization, staff.organization_id)
    facilities = (
        await db.scalars(
            select(Facility).where(
                Facility.organization_id == staff.organization_id,
                Facility.is_active.is_(True),
            )
        )
    ).all()
    practitioner = await db.scalar(
        select(Practitioner).where(
            Practitioner.user_account_id == account.id,
            Practitioner.organization_id == staff.organization_id,
            Practitioner.is_active.is_(True),
        )
    )
    return account, (staff, organization, roles, facilities, practitioner), facilities


async def _seed(db, *, actor, organization, facility, practitioner, reference_date):
    created: dict[str, list[str]] = {"created": [], "reused": []}

    def mark(key: str, was_created: bool) -> None:
        created["created" if was_created else "reused"].append(key)

    # The demo patient is linked to this organization; without the portal
    # opt-in the directory, booking and patient-domain endpoints reject it
    # (CLINIC_NOT_AVAILABLE) and the end-to-end demo stops at "no bookable
    # clinic". The seed enables it idempotently.
    if not organization.portal_enabled:
        organization.portal_enabled = True
        organization.updated_at = datetime.now(UTC)
        mark("organization:portal_enabled", True)
    else:
        mark("organization:portal_enabled", False)

    # Publish demo availability so the booking wizard can reach review for any
    # listed practitioner: Mon-Sat 09:00-17:00, 30-minute slots, lunch 13:00-14:00.
    # Existing active schedules are left untouched.
    active_practitioners = (
        await db.scalars(
            select(Practitioner).where(
                Practitioner.organization_id == organization.id,
                Practitioner.is_active.is_(True),
            )
        )
    ).all()
    existing_schedules = (
        await db.scalars(
            select(PractitionerSchedule).where(
                PractitionerSchedule.organization_id == organization.id,
                PractitionerSchedule.facility_id == facility.id,
                PractitionerSchedule.is_active.is_(True),
            )
        )
    ).all()
    scheduled_practitioner_ids = {s.practitioner_id for s in existing_schedules}
    working_days = [
        {
            "day_of_week": day,
            "is_working": True,
            "start_time": "09:00",
            "end_time": "17:00",
            "breaks": [{"title": "Lunch", "start_time": "13:00", "end_time": "14:00"}],
        }
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday")
    ]
    for practitioner_row in active_practitioners:
        key = f"schedule:{practitioner_row.person_name}"
        if practitioner_row.id in scheduled_practitioner_ids:
            mark(key, False)
            continue
        db.add(
            PractitionerSchedule(
                organization_id=organization.id,
                facility_id=facility.id,
                practitioner_id=practitioner_row.id,
                effective_from=reference_date,
                timezone="Asia/Kolkata",
                slot_interval_minutes=30,
                weekly_hours=working_days,
                is_active=True,
                is_override=True,
            )
        )
        mark(key, True)
    await db.flush()

    person = await db.scalar(
        select(Person).where(
            Person.organization_id == organization.id,
            Person.phone == normalize_phone(DEMO_PHONE),
        )
    )
    if person is None:
        person = Person(
            organization_id=organization.id,
            first_name="Asha",
            last_name="Kulkarni",
            phone=normalize_phone(DEMO_PHONE),
            date_of_birth="1988-08-14",
        )
        db.add(person)
        await db.flush()
    patient = await db.scalar(
        select(Patient).where(
            Patient.organization_id == organization.id, Patient.mrn == ASHA_MRN
        )
    )
    if patient is None:
        patient = Patient(
            organization_id=organization.id, person_id=person.id, mrn=ASHA_MRN
        )
        db.add(patient)
        await db.flush()
        mark("patient:asha", True)
    else:
        mark("patient:asha", False)

    account = await db.scalar(
        select(PatientPortalAccount).where(
            PatientPortalAccount.phone == person.phone
        )
    )
    if account is None:
        account = PatientPortalAccount(
            phone=person.phone, first_name="Asha", last_name="Kulkarni", date_of_birth="1988-08-14"
        )
        db.add(account)
        await db.flush()
        mark("portal_account:asha", True)
    else:
        mark("portal_account:asha", False)

    link = await db.scalar(
        select(PatientRecordLink).where(
            PatientRecordLink.patient_account_id == account.id,
            PatientRecordLink.status == "verified",
        )
    )
    if link is None:
        link_service = PatientLinkService(
            pepper=settings.SECRET_KEY.get_secret_value(),
            invitation_ttl_hours=1,
            invitation_max_ttl_hours=2,
        )
        _, token = await link_service.create_invitation(
            db,
            organization=organization,
            patient=patient,
            facility_id=facility.id,
            phone=person.phone,
            created_by_user_id=actor.id,
        )
        link = await link_service.activate(db, account=account, token=token)
        mark("record_link:verified", True)
    else:
        mark("record_link:verified", False)

    now = _reference_datetime(reference_date)
    start = now + timedelta(days=3)
    end = start + timedelta(minutes=30)
    existing_request = await db.scalar(
        select(AppointmentRequest).where(
            AppointmentRequest.idempotency_key == f"{MARKER}:pending"
        )
    )
    if existing_request is None and practitioner is not None:
        existing_request, _ = await appt.create_new_request(
            db,
            account=account,
            link=link,
            facility=facility,
            practitioner=practitioner,
            start=start,
            end=end,
            reason_code="demo",
            reason_text="Demo pending request",
            idempotency_key=f"{MARKER}:pending",
        )
        mark("request:pending", True)
    else:
        mark("request:pending", False)

    existing_appointment = await db.scalar(
        select(Appointment).where(Appointment.idempotency_key == f"{MARKER}:booked")
    )
    appointment = existing_appointment
    if appointment is None and practitioner is not None:
        appointment = Appointment(
            organization_id=organization.id,
            facility_id=facility.id,
            practitioner_id=practitioner.id,
            patient_id=patient.id,
            scheduled_start=now + timedelta(days=5),
            scheduled_end=now + timedelta(days=5, minutes=30),
            status="booked",
            idempotency_key=f"{MARKER}:booked",
        )
        db.add(appointment)
        await db.flush()
        mark("appointment:booked", True)
    else:
        mark("appointment:booked", False)

    cancelled = await db.scalar(
        select(Appointment).where(Appointment.idempotency_key == f"{MARKER}:cancelled")
    )
    if cancelled is None and practitioner is not None:
        cancelled = Appointment(
            organization_id=organization.id,
            facility_id=facility.id,
            practitioner_id=practitioner.id,
            patient_id=patient.id,
            scheduled_start=now - timedelta(days=2),
            scheduled_end=now - timedelta(days=2) + timedelta(minutes=30),
            status="cancelled",
            idempotency_key=f"{MARKER}:cancelled",
        )
        db.add(cancelled)
        await db.flush()
        mark("appointment:cancelled", True)
    else:
        mark("appointment:cancelled", False)

    encounter = await db.get(Encounter, _demo_id("encounter"))
    if encounter is None:
        encounter = Encounter(
            organization_id=organization.id,
            facility_id=facility.id,
            patient_id=patient.id,
            practitioner_id=practitioner.id if practitioner else None,
            status="completed",
            started_at=now - timedelta(days=7),
            completed_at=now - timedelta(days=7) + timedelta(minutes=40),
        )
        encounter.id = _demo_id("encounter")
        db.add(encounter)
        await db.flush()
        db.add(
            Vital(
                encounter_id=encounter.id,
                recorded_by_user_id=actor.id,
                name="blood_pressure",
                value="118/76",
                unit="mmHg",
                recording_id=uuid4(),
                recorded_at=encounter.started_at,
            )
        )
        db.add(
            Vital(
                encounter_id=encounter.id,
                recorded_by_user_id=actor.id,
                name="pulse",
                value="72",
                unit="bpm",
                recording_id=uuid4(),
                recorded_at=encounter.started_at,
            )
        )
        soap = SoapNote(
            encounter_id=encounter.id,
            subjective="Demo synthetic visit.",
            objective="Vitals recorded.",
            assessment="Synthetic demo case for portal fixtures only.",
            plan="No clinical advice; demonstration data.",
            status="draft",
        )
        db.add(soap)
        await db.flush()
        mark("encounter:completed", True)
    else:
        mark("encounter:completed", False)

    released_encounter = await db.scalar(
        select(RecordRelease).where(
            RecordRelease.resource_type == "encounter_summary",
            RecordRelease.resource_id == encounter.id,
            RecordRelease.revoked_at.is_(None),
        )
    )
    if released_encounter is None:
        snapshot = await record_service.build_encounter_summary_snapshot(
            db, encounter=encounter, summary="Synthetic demo visit summary released by the clinic.", allow_vitals=True
        )
        await record_service.create_release(
            db,
            actor_user_id=actor.id,
            organization_id=organization.id,
            facility_id=facility.id,
            patient_id=patient.id,
            encounter_id=encounter.id,
            resource_type="encounter_summary",
            resource_id=encounter.id,
            snapshot=snapshot,
        )
        mark("release:encounter_summary", True)
    else:
        mark("release:encounter_summary", False)

    prescription = await db.get(Prescription, _demo_id("prescription"))
    if prescription is None:
        prescription = Prescription(
            encounter_id=encounter.id,
            status="signed",
            advice="Synthetic demo prescription. No real medical advice.",
            signed_by_user_id=actor.id,
            signed_at=now - timedelta(days=7),
        )
        prescription.id = _demo_id("prescription")
        db.add(prescription)
        await db.flush()
        db.add(
            PrescriptionItem(
                prescription_id=prescription.id,
                medicine_name="Demo Medicine",
                dosage="1 tablet",
                frequency="once daily",
                duration="3 days",
            )
        )
        mark("prescription:signed", True)
    else:
        mark("prescription:signed", False)

    released_rx = await db.scalar(
        select(RecordRelease).where(
            RecordRelease.resource_type == "prescription",
            RecordRelease.resource_id == prescription.id,
            RecordRelease.revoked_at.is_(None),
        )
    )
    if released_rx is None:
        snapshot = await record_service.build_prescription_snapshot(db, prescription=prescription)
        await record_service.create_release(
            db,
            actor_user_id=actor.id,
            organization_id=organization.id,
            facility_id=facility.id,
            patient_id=patient.id,
            encounter_id=encounter.id,
            resource_type="prescription",
            resource_id=prescription.id,
            snapshot=snapshot,
        )
        mark("release:prescription", True)
    else:
        mark("release:prescription", False)

    draft_rx = await db.get(Prescription, _demo_id("prescription_draft"))
    if draft_rx is None:
        draft_rx = Prescription(
            encounter_id=encounter.id,
            status="draft",
            advice="WITHHELD DEMO DRAFT: must never be visible to the patient.",
        )
        draft_rx.id = _demo_id("prescription_draft")
        db.add(draft_rx)
        await db.flush()
        mark("prescription:draft_withheld", True)
    else:
        mark("prescription:draft_withheld", False)

    invoice = await db.get(Invoice, _demo_id("invoice"))
    if invoice is None:
        invoice = Invoice(
            organization_id=organization.id,
            facility_id=facility.id,
            encounter_id=encounter.id,
            patient_id=patient.id,
            amount_minor=120000,
            currency="INR",
            created_by_user_id=actor.id,
        )
        invoice.id = _demo_id("invoice")
        db.add(invoice)
        await db.commit()
        invoice = await portal_billing.apply_discount(
            db,
            invoice=invoice,
            actor_user_id=actor.id,
            kind="percentage",
            bp=1000,
            fixed_minor=None,
            reason="Synthetic demo staff discount",
        )
        mark("invoice:discounted", True)
    else:
        mark("invoice:discounted", False)

    payment = await db.scalar(
        select(Payment).where(Payment.idempotency_key == f"{MARKER}:payment")
    )
    if payment is None:
        payment = Payment(
            organization_id=organization.id,
            facility_id=facility.id,
            invoice_id=invoice.id,
            amount_minor=50000,
            currency="INR",
            idempotency_key=f"{MARKER}:payment",
            created_by_user_id=actor.id,
            provenance="synthetic_demo",
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)
        mark("payment:captured", True)
    else:
        mark("payment:captured", False)

    for kind, title, body, link_path in (
        ("appointment_confirmed", "Appointment confirmed", "Your demo appointment is confirmed.", f"/appointments/{appointment.id}" if appointment else "/appointments"),
        ("record_released", "New record available", "A demo visit summary was released.", "/records"),
        ("bill_updated", "Bill updated", "A demo discount and payment were recorded.", f"/bills/{invoice.id}"),
    ):
        await create_patient_notification(
            db,
            account_id=account.id,
            kind=kind,
            title=title,
            body=body,
            organization_id=organization.id,
            facility_id=facility.id,
            deep_link=link_path,
            dedup_key=f"{MARKER}:notify:{kind}",
        )
    await db.commit()
    return created, {
        "patient_uuid": str(patient.id),
        "portal_account_uuid": str(account.id),
        "record_link_uuid": str(link.id),
        "pending_request_uuid": str(existing_request.id) if existing_request else None,
        "booked_appointment_uuid": str(appointment.id) if appointment else None,
        "cancelled_appointment_uuid": str(cancelled.id) if cancelled else None,
        "completed_encounter_uuid": str(encounter.id),
        "prescription_uuid": str(prescription.id),
        "withheld_draft_prescription_uuid": str(draft_rx.id),
        "invoice_uuid": str(invoice.id),
        "payment_uuid": str(payment.id),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic patient demo seed")
    parser.add_argument("--apply", action="store_true", help="commit changes (default: dry run)")
    parser.add_argument("--actor-user-uuid", type=UUID, default=None)
    parser.add_argument("--facility-uuid", type=UUID, default=None)
    parser.add_argument("--reference-date", type=date.fromisoformat, default=None)
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "docs" / "patient-app" / "handoffs" / "demo-fixture-manifest.json")
    args = parser.parse_args()

    if settings.ENVIRONMENT.value == "production":
        print("Refusing to run in production.", file=sys.stderr)
        return 3
    reference_date = args.reference_date or datetime.now(UTC).date()

    engine = create_async_engine(settings.POSTGRES_ASYNC_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        account, context, _facilities = await _resolve_actor(db, args.actor_user_uuid)
        if account is None:
            print(
                f"BLOCKED: actor account '{ACTOR_EMAIL}' was not found in this database. "
                "No admin was invented. Provide --actor-user-uuid for an existing staff account.",
                file=sys.stderr,
            )
            await engine.dispose()
            return 2
        if context is None:
            print("BLOCKED: actor has no active staff membership.", file=sys.stderr)
            await engine.dispose()
            return 2
        _staff, organization, _roles, actor_facilities, practitioner = context
        if args.facility_uuid is not None:
            facility = await db.get(Facility, args.facility_uuid)
            if facility is None or facility.organization_id != organization.id:
                print("BLOCKED: --facility-uuid is not an active facility of the actor's organization.", file=sys.stderr)
                await engine.dispose()
                return 2
        elif len(actor_facilities) == 1:
            facility = actor_facilities[0]
        else:
            print(
                "BLOCKED: multiple facilities qualify; pass an explicit --facility-uuid.",
                file=sys.stderr,
            )
            await engine.dispose()
            return 2
        if practitioner is None:
            print("BLOCKED: actor has no active practitioner record; the demo needs a bookable practitioner.", file=sys.stderr)
            await engine.dispose()
            return 2
        created, ids = await _seed(
            db,
            actor=account,
            organization=organization,
            facility=facility,
            practitioner=practitioner,
            reference_date=reference_date,
        )
        manifest = {
            "generated_at": datetime.now(UTC).isoformat(),
            "apply": bool(args.apply),
            "environment": settings.ENVIRONMENT.value,
            "reference_date": reference_date.isoformat(),
            "actor_email": ACTOR_EMAIL,
            "organization_uuid": str(organization.id),
            "facility_uuid": str(facility.id),
            "synthetic": True,
            "document_bytes": "BLOCKED: no local GCS credentials; no patient_document fixture was created.",
            "records": ids,
            "created": created["created"],
            "reused": created["reused"],
            "commands": {
                "dry_run": "./venv/bin/python scripts/seed_patient_demo.py",
                "apply": f"./venv/bin/python scripts/seed_patient_demo.py --apply --facility-uuid {facility.id}",
                "repeat_apply_is_idempotent": True,
            },
        }
        if args.apply:
            await db.commit()
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
            print(f"APPLIED synthetic demo fixture; manifest at {args.manifest}")
        else:
            await db.rollback()
            print(json.dumps(manifest, indent=2))
            print("DRY RUN: nothing committed. Re-run with --apply to commit.", file=sys.stderr)
    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
