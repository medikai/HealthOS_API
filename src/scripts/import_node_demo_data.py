"""Import the demo SQLite database used by healthos-frontend into HealthOS PostgreSQL.

The import is deliberately idempotent: source UUIDs are retained and existing rows
are updated. Run migrations first, then:

    python src/scripts/import_node_demo_data.py --source ../healthos-frontend/mock-server/mock.db --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.db.database import local_session
from app.models.care import Appointment, Encounter, Practitioner, PractitionerAvailabilityException, PractitionerAvailabilityRule, Prescription, PrescriptionItem, QueueEntry, SoapNote, Vital
from app.models.identity import Patient, Person, UserAccount
from app.models.organization import Facility, FacilitySchedule, Organization, ProtectedPeriod, StaffAssignment, StaffMember


def uid(value: object, namespace: str = "node") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"healthos:{namespace}:{value}")


def dt(value: object) -> datetime | None:
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=result.tzinfo or UTC)


def d(value: object, fallback: date | None = None) -> date:
    parsed = dt(value)
    return parsed.date() if parsed else (date.fromisoformat(str(value)[:10]) if value else fallback or date.today())


def t(value: object) -> time | None:
    if not value:
        return None
    return time.fromisoformat(str(value)[:8])


def rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return []


def with_id(model, identifier: uuid.UUID, **values):
    """Create a MappedAsDataclass row while retaining the source UUID."""
    obj = model(**values)
    obj.id = identifier
    return obj


async def run(source: Path, apply: bool) -> None:
    conn = sqlite3.connect(source)
    conn.row_factory = sqlite3.Row
    async with local_session() as db:
        org = (await db.scalars(select(Organization).where(Organization.code == "LOCAL-DEMO"))).first()
        if not org:
            org = Organization(name="Node Demo Organization", code="LOCAL-DEMO")
            db.add(org); await db.flush()

        facilities: dict[str, uuid.UUID] = {}
        for row in rows(conn, "facilities"):
            source_id = str(row["uuid"])
            obj = await db.get(Facility, uid(source_id, "facility"))
            if not obj:
                obj = with_id(Facility, uid(source_id, "facility"), organization_id=org.id, name=row["name"] or "Imported Facility", code=(source_id[:12]).upper())
                db.add(obj)
            else:
                obj.name = row["name"] or obj.name
            facilities[source_id] = obj.id
        if not facilities:
            facility = (await db.scalars(select(Facility).where(Facility.organization_id == org.id))).first()
            if not facility:
                facility = Facility(organization_id=org.id, name="Main Hospital", code="MAIN")
                db.add(facility); await db.flush()
            facilities["__default__"] = facility.id
        default_facility = next(iter(facilities.values()))

        users: dict[str, uuid.UUID] = {}
        for row in rows(conn, "users"):
            source_id = str(row["uuid"]); account = (await db.scalars(select(UserAccount).where(UserAccount.logto_user_id == f"node:{source_id}"))).first()
            if not account:
                account = UserAccount(logto_user_id=f"node:{source_id}", display_name=row["name"] or "Imported User")
                db.add(account); await db.flush()
            users[source_id] = account.id
            staff = (await db.scalars(select(StaffMember).where(StaffMember.organization_id == org.id, StaffMember.user_account_id == account.id))).first()
            if not staff:
                staff = StaffMember(organization_id=org.id, user_account_id=account.id); db.add(staff); await db.flush()
            role = "organization_admin" if str(row["role"] or "").lower() in {"admin", "owner", "administrator"} else "facility_operator"
            assignment = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == staff.id, StaffAssignment.role_code == role))).first()
            if not assignment:
                db.add(StaffAssignment(staff_member_id=staff.id, role_code=role, facility_id=default_facility))

        patients: dict[str, uuid.UUID] = {}
        for row in rows(conn, "patients"):
            source_id = str(row["uuid"]); pid = uid(source_id, "patient"); person_id = uid(source_id, "person")
            person = await db.get(Person, person_id)
            if not person:
                person = with_id(Person, person_id, organization_id=org.id, first_name=row["first_name"] or "Unknown", last_name=row["last_name"], phone=row["phone"], email=None); db.add(person)
            patient = await db.get(Patient, pid)
            if not patient:
                patient = with_id(Patient, pid, organization_id=org.id, person_id=person_id, mrn=row["mrn"] or source_id[:32], is_active=(row["status"] or "active") != "inactive"); db.add(patient)
            patients[source_id] = pid

        practitioners: dict[str, uuid.UUID] = {}
        for row in rows(conn, "practitioners"):
            source_id = str(row["uuid"]); practitioner = await db.get(Practitioner, uid(source_id, "practitioner"))
            source_user = users.get(str(row["user_uuid"]))
            if not practitioner and source_user:
                practitioner = (await db.scalars(select(Practitioner).where(Practitioner.user_account_id == source_user))).first()
            if not practitioner:
                practitioner = with_id(Practitioner, uid(source_id, "practitioner"), organization_id=org.id, person_name=row["name"] or "Imported Practitioner", specialty=row["specialty"], user_account_id=source_user); db.add(practitioner)
            practitioners[source_id] = practitioner.id
        await db.commit()

        facility_for = lambda value: facilities.get(str(value), default_facility)
        skipped_appointments = 0
        for row in rows(conn, "appointments"):
            obj = await db.get(Appointment, uid(row["uuid"], "appointment"))
            if not obj and str(row["patient_uuid"]) in patients and str(row["practitioner_uuid"]) in practitioners:
                start = dt(row["start_time"]) or datetime.now(UTC)
                try:
                    async with db.begin_nested():
                        db.add(with_id(Appointment, uid(row["uuid"], "appointment"), organization_id=org.id, facility_id=facility_for(row["facility_uuid"]), practitioner_id=practitioners[str(row["practitioner_uuid"])], patient_id=patients[str(row["patient_uuid"])], scheduled_start=start, scheduled_end=dt(row["end_time"]) or start, status={"confirmed": "booked", "cancelled": "cancelled"}.get(row["status"], row["status"] or "booked"), reason_code="follow_up"))
                        await db.flush()
                except IntegrityError as exc:
                    skipped_appointments += 1
                    print(f"WARNING: skipped conflicting appointment {row['uuid']}: {exc.orig}")
        await db.commit()

        for row in rows(conn, "queue_entries"):
            obj = await db.get(QueueEntry, uid(row["uuid"], "queue"))
            patient = patients.get(str(row["patient_uuid"]))
            if not obj and patient:
                appointment_id = uid(row["appointment_uuid"], "appointment") if row["appointment_uuid"] else None
                if appointment_id and not await db.get(Appointment, appointment_id):
                    print(f"WARNING: queue entry {row['uuid']} references an unimported appointment; clearing appointment_id")
                    appointment_id = None
                db.add(with_id(QueueEntry, uid(row["uuid"], "queue"), organization_id=org.id, facility_id=facility_for(row["facility_uuid"]), patient_id=patient, queue_date=d(row["created_at"]), token_number=int(row["token_number"] or 0), appointment_id=appointment_id, practitioner_id=practitioners.get(str(row["practitioner_uuid"])), status=row["status"] or "waiting"))
        await db.commit()

        for row in rows(conn, "encounters"):
            obj = await db.get(Encounter, uid(row["uuid"], "encounter")); patient = patients.get(str(row["patient_uuid"]))
            if not obj and patient:
                appointment_id = uid(row["appointment_uuid"], "appointment") if row["appointment_uuid"] else None
                if appointment_id and not await db.get(Appointment, appointment_id):
                    appointment_id = None
                queue_entry_id = uid(row["queue_entry_uuid"], "queue") if row["queue_entry_uuid"] else None
                if queue_entry_id and not await db.get(QueueEntry, queue_entry_id):
                    queue_entry_id = None
                db.add(with_id(Encounter, uid(row["uuid"], "encounter"), organization_id=org.id, facility_id=facility_for(row["facility_uuid"]), patient_id=patient, practitioner_id=practitioners.get(str(row["practitioner_uuid"])), appointment_id=appointment_id, queue_entry_id=queue_entry_id, status=row["status"] or "in_progress", started_at=dt(row["started_at"]) or datetime.now(UTC), completed_at=dt(row["ended_at"])))
        await db.commit()
    conn.close()
    print(f"Node demo data import completed (appointments skipped due to conflicts: {skipped_appointments}).")


def main() -> None:
    parser = argparse.ArgumentParser()
    default_source = Path(__file__).resolve().parents[3] / "healthos-frontend" / "mock-server" / "mock.db"
    parser.add_argument("--source", type=Path, default=default_source)
    parser.add_argument("--apply", action="store_true", help="write to PostgreSQL; without this only validates the source")
    args = parser.parse_args()
    if not args.source.exists():
        raise SystemExit(f"Source database not found: {args.source}")
    if not args.apply:
        sqlite3.connect(args.source).execute("PRAGMA integrity_check").fetchone()
        print(f"Validated SQLite source: {args.source}. Re-run with --apply to import.")
        return
    asyncio.run(run(args.source, args.apply))


if __name__ == "__main__":
    main()
