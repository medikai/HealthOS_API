"""PA-BE03 regression coverage for reported frontend-integration defects.

Gated like the other DB tests: distinct ``TEST_POSTGRES_ASYNC_URL`` or
``HEALTHOS_ALLOW_LOCAL_DB_TESTS=1`` against the configured non-production DB.
Uses controlled synthetic rows and cleans them up.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.api.patient_dependencies import PatientPrincipal
from src.app.api.v1.appointment_requests import propose_alternative
from src.app.api.v1.patient_domain import (
    create_appointment_request,
    reschedule_request,
)
from src.app.models.care import Appointment
from src.app.models.organization import StaffAssignment, StaffMember
from src.app.schemas.patient_domain import (
    AppointmentRequestBody,
    RescheduleRequestBody,
    StaffAlternativeBody,
)
from tests.test_patient_domain_db import (
    ACTIVE_DB_URL,
    _hard_cleanup,
    _seed,
    _upgrade_schema,
    requires_db,
)


@requires_db
def test_pa_be03_normalize_range_callers_and_push_semantics():
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
                ctx = await _seed(db, prefix="SYNTB03")
                org_ids.append(ctx["org"].id)
                account_ids.append(ctx["account"].id)
                patient_ids.append(ctx["patient"].id)
                person_ids.append(ctx["person"].id)
                staff_user_ids.append(ctx["staff_account"].id)
                organization = ctx["org"]
                facility = ctx["facility"]
                practitioner = ctx["practitioner"]
                account = ctx["account"]
                # Staff context for the propose-alternative endpoint.
                member = StaffMember(
                    organization_id=organization.id,
                    user_account_id=ctx["staff_account"].id,
                    is_active=True,
                )
                db.add(member)
                await db.flush()
                db.add(
                    StaffAssignment(
                        staff_member_id=member.id,
                        facility_id=facility.id,
                        role_code="organization_admin",
                        is_active=True,
                    )
                )
                await db.commit()

                principal = PatientPrincipal(
                    account=account,
                    session=SimpleNamespace(id=uuid4(), csrf_token="csrf"),
                    token="opaque",
                )
                monday = datetime(2026, 10, 12, 10, 0)  # noqa: DTZ001 - naive IST wall time
                from zoneinfo import ZoneInfo

                start = monday.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                end = start + timedelta(minutes=30)

                # B1a: patient create-appointment-request (previously 500 TypeError).
                created = await create_appointment_request(
                    AppointmentRequestBody(
                        organization_uuid=organization.id,
                        facility_uuid=facility.id,
                        practitioner_uuid=practitioner.id,
                        scheduled_start=start,
                        scheduled_end=end,
                        reason_text="PA-BE03 regression",
                    ),
                    principal=principal,
                    db=db,
                    idempotency_key=f"pa-be03-{uuid4().hex}",
                )
                request_uuid = UUID(created["data"]["uuid"])
                assert created["data"]["status"] == "requested"

                # B1b: staff propose-alternative (previously 500 TypeError).
                proposed = await propose_alternative(
                    request_uuid,
                    StaffAlternativeBody(
                        scheduled_start=start + timedelta(hours=1),
                        scheduled_end=start + timedelta(hours=1, minutes=30),
                    ),
                    account=ctx["staff_account"],
                    db=db,
                )
                assert proposed["data"]["status"] == "alternative_proposed"

                # B1c: patient reschedule-request (previously 500 TypeError).
                appointment = Appointment(
                    organization_id=organization.id,
                    facility_id=facility.id,
                    practitioner_id=practitioner.id,
                    patient_id=ctx["patient"].id,
                    scheduled_start=start + timedelta(days=1),
                    scheduled_end=start + timedelta(days=1, minutes=30),
                    status="booked",
                    version=1,
                    idempotency_key=f"pa-be03-appt-{uuid4().hex}",
                )
                db.add(appointment)
                await db.commit()
                rescheduled = await reschedule_request(
                    appointment.id,
                    RescheduleRequestBody(
                        scheduled_start=start + timedelta(hours=2),
                        scheduled_end=start + timedelta(hours=2, minutes=30),
                        version=1,
                        reason="PA-BE03 regression",
                    ),
                    principal=principal,
                    db=db,
                    idempotency_key=f"pa-be03-resched-{uuid4().hex}",
                )
                assert rescheduled["data"]["kind"] == "reschedule"
                refreshed = await db.get(Appointment, appointment.id)
                await db.refresh(refreshed)
                assert refreshed.version == 1  # original preserved until approval

                # B2: documented dev allowlist JSON parses.
                from src.app.core.config import PatientPortalSettings

                parsed = PatientPortalSettings(
                    PATIENT_OTP_DEV_ALLOWLIST='["+919999900001"]'
                )
                assert parsed.PATIENT_OTP_DEV_ALLOWLIST == ["+919999900001"]
                parsed_csv = PatientPortalSettings(
                    PATIENT_OTP_DEV_ALLOWLIST="+919999900001, +919999900002"
                )
                assert parsed_csv.PATIENT_OTP_DEV_ALLOWLIST == [
                    "+919999900001",
                    "+919999900002",
                ]

                # B3: push status must not claim availability without credentials.
                from src.app.domains.patient_portal.notifications import push_status

                status = push_status()
                assert status["in_app_unaffected"] is True
                assert set(status) >= {"available", "configured", "delivery_ready", "reason", "provider"}
                assert status["available"] is status["delivery_ready"]
        finally:
            async with factory() as db:
                try:
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
                        await db.commit()
                except Exception:  # noqa: BLE001 - cleanup best effort
                    await db.rollback()
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
