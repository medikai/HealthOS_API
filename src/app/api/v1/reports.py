from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import DEFAULT_TIMEZONE, local_datetime, timezone, to_utc
from ...models.billing import ConsultationFee, Invoice, Payment, Refund
from ...models.care import Appointment, Encounter, Practitioner
from ...models.identity import Patient, Person, UserAccount
from ...models.organization import (
    Facility,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffMember,
)
from .bootstrap import ADMIN_ROLES
from .patients import _escape_like

router = APIRouter(prefix="/reports", tags=["reports"])
MAX_REPORT_DAYS = 366


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


async def _report_context(
    db: AsyncSession, account: UserAccount, facility_uuid: UUID
) -> tuple[Organization, Facility, str]:
    organization = await db.scalar(
        select(Organization)
        .join(StaffMember, StaffMember.organization_id == Organization.id)
        .join(StaffAssignment, StaffAssignment.staff_member_id == StaffMember.id)
        .where(
            StaffMember.user_account_id == account.id,
            StaffMember.is_active.is_(True),
            Organization.is_active.is_(True),
            StaffAssignment.is_active.is_(True),
            StaffAssignment.role_code.in_(ADMIN_ROLES),
        )
    )
    if organization is None:
        raise _error(
            403, "REPORT_ACCESS_REQUIRED", "Administrator report access is required."
        )

    row = (
        await db.execute(
            select(Facility, FacilitySchedule.timezone)
            .outerjoin(FacilitySchedule, FacilitySchedule.facility_id == Facility.id)
            .where(
                Facility.id == facility_uuid,
                Facility.organization_id == organization.id,
                Facility.is_active.is_(True),
            )
        )
    ).first()
    if row is None:
        raise _error(404, "FACILITY_NOT_FOUND", "Facility not found or inactive.")
    facility, timezone_name = row
    return organization, facility, timezone_name or DEFAULT_TIMEZONE


def _patient_filter(patient_query: str | None):
    if not patient_query:
        return None
    value = f"%{_escape_like(patient_query.strip())}%"
    return or_(
        Patient.mrn.ilike(value, escape="\\"),
        Person.first_name.ilike(value, escape="\\"),
        Person.last_name.ilike(value, escape="\\"),
        (Person.first_name + " " + func.coalesce(Person.last_name, "")).ilike(
            value, escape="\\"
        ),
    )


def _encounter_filters(
    organization_id: UUID,
    facility_id: UUID,
    start_utc,
    end_utc,
    practitioner_uuid: UUID | None,
    patient_filter,
) -> list[Any]:
    filters = [
        Encounter.organization_id == organization_id,
        Encounter.facility_id == facility_id,
        Encounter.started_at >= start_utc,
        Encounter.started_at < end_utc,
    ]
    if practitioner_uuid:
        filters.append(Encounter.practitioner_id == practitioner_uuid)
    if patient_filter is not None:
        filters.append(patient_filter)
    return filters


def _appointment_filters(
    organization_id: UUID,
    facility_id: UUID,
    start_utc,
    end_utc,
    practitioner_uuid: UUID | None,
    patient_filter,
) -> list[Any]:
    filters = [
        Appointment.organization_id == organization_id,
        Appointment.facility_id == facility_id,
        Appointment.scheduled_start >= start_utc,
        Appointment.scheduled_start < end_utc,
    ]
    if practitioner_uuid:
        filters.append(Appointment.practitioner_id == practitioner_uuid)
    if patient_filter is not None:
        filters.append(patient_filter)
    return filters


async def _money_sum(db: AsyncSession, query) -> dict[str, int]:
    return {
        currency: int(amount or 0)
        for currency, amount in (await db.execute(query)).all()
    }


def _single_currency_money(
    billed: dict[str, int],
    payments: dict[str, int],
    refunds: dict[str, int],
    fallback: str | None,
) -> tuple[dict[str, Any], str | None]:
    currencies = set(billed) | set(payments) | set(refunds)
    if len(currencies) > 1:
        return {}, "MIXED_CURRENCIES"
    currency = next(iter(currencies), fallback)
    if currency is None:
        return {}, "PRICE_DATA_UNAVAILABLE"
    billed_amount = billed.get(currency, 0)
    payments_received = payments.get(currency, 0)
    refund_amount = refunds.get(currency, 0)
    return {
        "billed_amount": billed_amount,
        "payments_received": payments_received,
        "refunds": refund_amount,
        "net_collections": payments_received - refund_amount,
        "currency": currency,
    }, None


@router.get("/visits")
async def visits_report(
    facility_uuid: UUID,
    date_from: date,
    date_to: date,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    practitioner_uuid: UUID | None = None,
    patient_query: str | None = Query(default=None, min_length=1, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    if date_from > date_to:
        raise _error(
            422, "INVALID_DATE_RANGE", "date_from must be on or before date_to."
        )
    if (date_to - date_from).days + 1 > MAX_REPORT_DAYS:
        raise _error(
            422,
            "REPORT_RANGE_TOO_LARGE",
            f"Date range cannot exceed {MAX_REPORT_DAYS} days.",
        )
    if patient_query is not None and not patient_query.strip():
        raise _error(422, "INVALID_PATIENT_QUERY", "patient_query cannot be blank.")

    organization, facility, timezone_name = await _report_context(
        db, account, facility_uuid
    )
    tz = timezone(timezone_name)
    start_utc = to_utc(local_datetime(date_from, time.min, tz), tz)
    end_utc = to_utc(local_datetime(date_to + timedelta(days=1), time.min, tz), tz)

    if practitioner_uuid is not None:
        practitioner_exists = await db.scalar(
            select(Practitioner.id).where(
                Practitioner.id == practitioner_uuid,
                Practitioner.organization_id == organization.id,
                Practitioner.is_active.is_(True),
            )
        )
        if practitioner_exists is None:
            raise _error(
                404, "PRACTITIONER_NOT_FOUND", "Practitioner not found or inactive."
            )

    patient_filter = _patient_filter(patient_query)
    encounter_filters = _encounter_filters(
        organization.id,
        facility.id,
        start_utc,
        end_utc,
        practitioner_uuid,
        patient_filter,
    )
    appointment_filters = _appointment_filters(
        organization.id,
        facility.id,
        start_utc,
        end_utc,
        practitioner_uuid,
        patient_filter,
    )

    encounter_metrics_query = select(
        func.count(Encounter.id).label("visits_total"),
        func.count(Encounter.id)
        .filter(Encounter.status == "completed")
        .label("completed_visits"),
        func.count(func.distinct(Encounter.patient_id))
        .filter(Encounter.status == "completed")
        .label("unique_visited_patients"),
    )
    appointment_metrics_query = select(
        func.count(Appointment.id).label("scheduled_appointments"),
        func.count(Appointment.id)
        .filter(Appointment.status == "cancelled")
        .label("cancelled_appointments"),
        func.count(Appointment.id)
        .filter(Appointment.status == "no_show")
        .label("no_show_appointments"),
    )
    total_query = select(func.count(Encounter.id))
    if patient_filter is not None:
        encounter_metrics_query = encounter_metrics_query.join(
            Patient,
            and_(
                Patient.id == Encounter.patient_id,
                Patient.organization_id == Encounter.organization_id,
            ),
        ).join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Encounter.organization_id,
            ),
        )
        appointment_metrics_query = appointment_metrics_query.join(
            Patient,
            and_(
                Patient.id == Appointment.patient_id,
                Patient.organization_id == Appointment.organization_id,
            ),
        ).join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Appointment.organization_id,
            ),
        )
        total_query = total_query.join(
            Patient,
            and_(
                Patient.id == Encounter.patient_id,
                Patient.organization_id == Encounter.organization_id,
            ),
        ).join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Encounter.organization_id,
            ),
        )

    encounter_metrics = (
        await db.execute(encounter_metrics_query.where(*encounter_filters))
    ).one()
    appointment_metrics = (
        await db.execute(appointment_metrics_query.where(*appointment_filters))
    ).one()

    invoice_query = select(
        Invoice.currency, func.coalesce(func.sum(Invoice.amount_minor), 0)
    ).where(
        Invoice.organization_id == organization.id,
        Invoice.facility_id == facility.id,
        Invoice.status != "voided",
        Invoice.issued_at >= start_utc,
        Invoice.issued_at < end_utc,
    )
    payment_query = select(
        Payment.currency, func.coalesce(func.sum(Payment.amount_minor), 0)
    ).where(
        Payment.organization_id == organization.id,
        Payment.facility_id == facility.id,
        Payment.status == "captured",
        Payment.received_at >= start_utc,
        Payment.received_at < end_utc,
    )
    refund_query = select(
        Refund.currency, func.coalesce(func.sum(Refund.amount_minor), 0)
    ).where(
        Refund.organization_id == organization.id,
        Refund.facility_id == facility.id,
        Refund.status == "completed",
        Refund.refunded_at >= start_utc,
        Refund.refunded_at < end_utc,
    )
    if practitioner_uuid or patient_filter is not None:
        invoice_query = invoice_query.join(
            Encounter, Encounter.id == Invoice.encounter_id
        )
        payment_query = payment_query.join(
            Invoice, Invoice.id == Payment.invoice_id
        ).join(Encounter, Encounter.id == Invoice.encounter_id)
        refund_query = refund_query.join(Invoice, Invoice.id == Refund.invoice_id).join(
            Encounter, Encounter.id == Invoice.encounter_id
        )
        if practitioner_uuid:
            invoice_query = invoice_query.where(
                Encounter.practitioner_id == practitioner_uuid
            )
            payment_query = payment_query.where(
                Encounter.practitioner_id == practitioner_uuid
            )
            refund_query = refund_query.where(
                Encounter.practitioner_id == practitioner_uuid
            )
        if patient_filter is not None:
            for name, query in (
                ("invoice", invoice_query),
                ("payment", payment_query),
                ("refund", refund_query),
            ):
                query = (
                    query.join(
                        Patient,
                        and_(
                            Patient.id == Encounter.patient_id,
                            Patient.organization_id == Encounter.organization_id,
                        ),
                    )
                    .join(
                        Person,
                        and_(
                            Person.id == Patient.person_id,
                            Person.organization_id == Encounter.organization_id,
                        ),
                    )
                    .where(patient_filter)
                )
                if name == "invoice":
                    invoice_query = query
                elif name == "payment":
                    payment_query = query
                else:
                    refund_query = query
    invoice_query = invoice_query.group_by(Invoice.currency)
    payment_query = payment_query.group_by(Payment.currency)
    refund_query = refund_query.group_by(Refund.currency)

    visit_invoice_query = (
        select(Invoice.currency, func.coalesce(func.sum(Invoice.amount_minor), 0))
        .join(Encounter, Encounter.id == Invoice.encounter_id)
        .where(Invoice.status != "voided", *encounter_filters)
        .group_by(Invoice.currency)
    )
    visit_payment_query = (
        select(Payment.currency, func.coalesce(func.sum(Payment.amount_minor), 0))
        .join(Invoice, Invoice.id == Payment.invoice_id)
        .join(Encounter, Encounter.id == Invoice.encounter_id)
        .where(Payment.status == "captured", *encounter_filters)
        .group_by(Payment.currency)
    )
    visit_refund_query = (
        select(Refund.currency, func.coalesce(func.sum(Refund.amount_minor), 0))
        .join(Invoice, Invoice.id == Refund.invoice_id)
        .join(Encounter, Encounter.id == Invoice.encounter_id)
        .where(Refund.status == "completed", *encounter_filters)
        .group_by(Refund.currency)
    )
    if patient_filter is not None:
        for name, query in (
            ("invoice", visit_invoice_query),
            ("payment", visit_payment_query),
            ("refund", visit_refund_query),
        ):
            query = query.join(
                Patient,
                and_(
                    Patient.id == Encounter.patient_id,
                    Patient.organization_id == Encounter.organization_id,
                ),
            ).join(
                Person,
                and_(
                    Person.id == Patient.person_id,
                    Person.organization_id == Encounter.organization_id,
                ),
            )
            if name == "invoice":
                visit_invoice_query = query
            elif name == "payment":
                visit_payment_query = query
            else:
                visit_refund_query = query

    billed = await _money_sum(db, invoice_query)
    payments = await _money_sum(db, payment_query)
    refunds = await _money_sum(db, refund_query)
    visit_billed = await _money_sum(db, visit_invoice_query)
    visit_payments = await _money_sum(db, visit_payment_query)
    visit_refunds = await _money_sum(db, visit_refund_query)
    fallback_currency = await db.scalar(
        select(ConsultationFee.currency)
        .where(
            ConsultationFee.facility_id == facility.id,
            ConsultationFee.is_active.is_(True),
        )
        .limit(1)
    )
    money, money_error = _single_currency_money(
        billed, payments, refunds, fallback_currency
    )
    visit_linked, visit_money_error = _single_currency_money(
        visit_billed, visit_payments, visit_refunds, fallback_currency
    )

    total = int(await db.scalar(total_query.where(*encounter_filters)) or 0)
    rows = (
        await db.execute(
            select(Encounter, Patient, Person, Practitioner, Appointment)
            .join(
                Patient,
                and_(
                    Patient.id == Encounter.patient_id,
                    Patient.organization_id == Encounter.organization_id,
                ),
            )
            .join(
                Person,
                and_(
                    Person.id == Patient.person_id,
                    Person.organization_id == Encounter.organization_id,
                ),
            )
            .outerjoin(
                Practitioner,
                and_(
                    Practitioner.id == Encounter.practitioner_id,
                    Practitioner.organization_id == Encounter.organization_id,
                ),
            )
            .outerjoin(
                Appointment,
                and_(
                    Appointment.id == Encounter.appointment_id,
                    Appointment.organization_id == Encounter.organization_id,
                ),
            )
            .where(*encounter_filters)
            .order_by(Encounter.started_at.desc(), Encounter.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    if money_error:
        money = {
            key: None
            for key in (
                "billed_amount",
                "payments_received",
                "refunds",
                "net_collections",
                "currency",
            )
        }
    if visit_money_error:
        visit_linked = {
            key: None
            for key in (
                "billed_amount",
                "payments_received",
                "refunds",
                "net_collections",
                "currency",
            )
        }
    items = [
        {
            "encounter_uuid": str(encounter.id),
            "visit_started_at": encounter.started_at.isoformat(),
            "completed_at": encounter.completed_at.isoformat()
            if encounter.completed_at
            else None,
            "status": encounter.status,
            "patient": {
                "uuid": str(patient.id),
                "display_name": f"{person.first_name} {person.last_name or ''}".strip(),
                "mrn": patient.mrn,
            },
            "practitioner": (
                {"uuid": str(practitioner.id), "name": practitioner.person_name}
                if practitioner
                else None
            ),
            "appointment_uuid": str(appointment.id) if appointment else None,
            "appointment_status": appointment.status if appointment else None,
        }
        for encounter, patient, person, practitioner, appointment in rows
    ]
    total_pages = (total + page_size - 1) // page_size
    return {
        "success": True,
        "data": {
            "timezone": timezone_name,
            "range": {
                "local_from": date_from.isoformat(),
                "local_to": date_to.isoformat(),
                "utc_from": start_utc.isoformat(),
                "utc_to_exclusive": end_utc.isoformat(),
            },
            "availability": {
                "money": money_error is None and visit_money_error is None,
                "reason": money_error or visit_money_error,
                "amount_unit": "minor",
            },
            "metrics": {
                "visits_total": int(encounter_metrics.visits_total or 0),
                "completed_visits": int(encounter_metrics.completed_visits or 0),
                "unique_visited_patients": int(
                    encounter_metrics.unique_visited_patients or 0
                ),
                "scheduled_appointments": int(
                    appointment_metrics.scheduled_appointments or 0
                ),
                "cancelled_appointments": int(
                    appointment_metrics.cancelled_appointments or 0
                ),
                "no_show_appointments": int(
                    appointment_metrics.no_show_appointments or 0
                ),
                **money,
            },
            "date_basis": {
                "visits": "encounter.started_at",
                "appointments": "appointment.scheduled_start",
                "billed_amount": "invoice.issued_at",
                "payments_received": "payment.received_at",
                "refunds": "refund.refunded_at",
            },
            "visit_linked": visit_linked,
            "items": items,
        },
        "meta": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }


def _report_patient_join(query, model, patient_filter):
    if patient_filter is None:
        return query
    return (
        query.join(
            Patient,
            and_(Patient.id == model.patient_id, Patient.organization_id == model.organization_id),
        )
        .join(
            Person,
            and_(Person.id == Patient.person_id, Person.organization_id == model.organization_id),
        )
        .where(patient_filter)
    )


async def _weekly_metrics(
    db: AsyncSession, organization_id: UUID, facility_id: UUID,
    start_utc, end_utc, timezone_name: str,
    practitioner_uuid: UUID | None, patient_filter, fallback_currency: str | None,
    *, breakdown: bool,
) -> dict[str, Any]:
    """Aggregate a period with the daily report's timestamps and status rules."""
    encounter_filters = _encounter_filters(
        organization_id, facility_id, start_utc, end_utc, practitioner_uuid, patient_filter
    )
    appointment_filters = _appointment_filters(
        organization_id, facility_id, start_utc, end_utc, practitioner_uuid, patient_filter
    )
    local_day = lambda column: func.date(func.timezone(timezone_name, column))
    counters = (
        func.count(Encounter.id).label("visits_total"),
        func.count(Encounter.id).filter(Encounter.status == "completed").label("completed_visits"),
        func.count(func.distinct(Encounter.patient_id)).filter(Encounter.status == "completed").label("unique_visited_patients"),
    )
    appointment_counters = (
        func.count(Appointment.id).label("scheduled_appointments"),
        func.count(Appointment.id).filter(Appointment.status == "cancelled").label("cancelled_appointments"),
        func.count(Appointment.id).filter(Appointment.status == "no_show").label("no_show_appointments"),
    )
    totals = {key: 0 for key in (
        "visits_total", "completed_visits", "unique_visited_patients",
        "scheduled_appointments", "cancelled_appointments", "no_show_appointments",
    )}
    days: dict[date, dict[str, Any]] = {}
    practitioners: dict[UUID | None, dict[str, Any]] = {}

    def bucket(mapping, key):
        return mapping.setdefault(key, {name: 0 for name in totals})

    for model, filters, expressions, names in (
        (Encounter, encounter_filters, counters, ("visits_total", "completed_visits", "unique_visited_patients")),
        (Appointment, appointment_filters, appointment_counters, ("scheduled_appointments", "cancelled_appointments", "no_show_appointments")),
    ):
        query = _report_patient_join(select(*expressions), model, patient_filter).where(*filters)
        row = (await db.execute(query)).one()
        for name in names:
            totals[name] = int(getattr(row, name) or 0)
        if breakdown:
            day = local_day(model.started_at if model is Encounter else model.scheduled_start)
            query = _report_patient_join(
                select(day.label("day"), model.practitioner_id.label("practitioner_id"), *expressions),
                model, patient_filter,
            ).where(*filters).group_by(day, model.practitioner_id)
            for row in (await db.execute(query)).all():
                for name in names:
                    if name != "unique_visited_patients":
                        bucket(days, row.day)[name] += int(getattr(row, name) or 0)
                        bucket(practitioners, row.practitioner_id)[name] += int(getattr(row, name) or 0)
            if model is Encounter:
                query = _report_patient_join(
                    select(day.label("day"), counters[2]), Encounter, patient_filter
                ).where(*filters).group_by(day)
                for row in (await db.execute(query)).all():
                    bucket(days, row.day)["unique_visited_patients"] = int(row.unique_visited_patients or 0)
                query = _report_patient_join(
                    select(Encounter.practitioner_id, counters[2]), Encounter, patient_filter
                ).where(*filters).group_by(Encounter.practitioner_id)
                for row in (await db.execute(query)).all():
                    bucket(practitioners, row.practitioner_id)["unique_visited_patients"] = int(row.unique_visited_patients or 0)

    money_rows: dict[str, dict[tuple, dict[str, int]]] = {}
    for name, model, instant, status in (
        ("billed_amount", Invoice, Invoice.issued_at, "voided"),
        ("payments_received", Payment, Payment.received_at, "captured"),
        ("refunds", Refund, Refund.refunded_at, "completed"),
    ):
        filters = [model.organization_id == organization_id, model.facility_id == facility_id,
                   instant >= start_utc, instant < end_utc]
        filters.append(model.status != status if model is Invoice else model.status == status)
        day = local_day(instant)
        query = select(model.currency, func.sum(model.amount_minor).label("amount"))
        if breakdown:
            query = query.add_columns(day.label("day"), Encounter.practitioner_id.label("practitioner_id"))
        if breakdown or practitioner_uuid or patient_filter is not None:
            if model is not Invoice:
                query = query.join(Invoice, Invoice.id == model.invoice_id)
            query = query.join(Encounter, Encounter.id == Invoice.encounter_id)
            if practitioner_uuid:
                filters.append(Encounter.practitioner_id == practitioner_uuid)
            query = _report_patient_join(query, Encounter, patient_filter)
        query = query.where(*filters)
        if breakdown:
            query = query.group_by(model.currency, day, Encounter.practitioner_id)
        else:
            query = query.group_by(model.currency)
        grouped = {}
        for row in (await db.execute(query)).all():
            key = (row.day, row.practitioner_id) if breakdown else (None, None)
            grouped.setdefault(key, {})[row.currency] = int(row.amount or 0)
        money_rows[name] = grouped

    def money_for(keys):
        keys = tuple(keys)
        by_name = {}
        for name in money_rows:
            amounts = {}
            for key in keys:
                for currency, amount in money_rows[name].get(key, {}).items():
                    amounts[currency] = amounts.get(currency, 0) + amount
            by_name[name] = amounts
        money, reason = _single_currency_money(
            by_name["billed_amount"], by_name["payments_received"], by_name["refunds"], fallback_currency
        )
        if reason:
            money = {key: None for key in ("billed_amount", "payments_received", "refunds", "net_collections", "currency")}
        return money, reason

    all_keys = set().union(*(set(group) for group in money_rows.values()))
    money, reason = money_for(all_keys)
    totals.update(money)
    if breakdown:
        for name in money_rows:
            for day, practitioner_id in money_rows[name]:
                bucket(days, day)
                bucket(practitioners, practitioner_id)
        for day, values in days.items():
            values.update(money_for(key for key in all_keys if key[0] == day)[0])
        for practitioner_id, values in practitioners.items():
            values.update(money_for(key for key in all_keys if key[1] == practitioner_id)[0])
    return {"metrics": totals, "money_unavailable_reason": reason, "days": days, "practitioners": practitioners}


@router.get("/weekly")
async def weekly_overview(
    facility_uuid: UUID,
    week_of: date,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    practitioner_uuid: UUID | None = None,
    patient_query: str | None = Query(default=None, min_length=1, max_length=120),
) -> dict[str, Any]:
    if patient_query is not None and not patient_query.strip():
        raise _error(422, "INVALID_PATIENT_QUERY", "patient_query cannot be blank.")
    organization, facility, timezone_name = await _report_context(db, account, facility_uuid)
    tz = timezone(timezone_name)
    if practitioner_uuid is not None and await db.scalar(select(Practitioner.id).where(
        Practitioner.id == practitioner_uuid, Practitioner.organization_id == organization.id,
        Practitioner.is_active.is_(True),
    )) is None:
        raise _error(404, "PRACTITIONER_NOT_FOUND", "Practitioner not found or inactive.")
    start_day = week_of - timedelta(days=week_of.weekday())
    end_day = start_day + timedelta(days=7)
    now = datetime.now(UTC).astimezone(tz)
    if start_day > now.date():
        raise _error(422, "FUTURE_WEEK", "Weekly overview is unavailable for future weeks.")
    start_utc = to_utc(local_datetime(start_day, time.min, tz), tz)
    end_utc = to_utc(local_datetime(end_day, time.min, tz), tz)
    comparison_end = min(end_utc, now.astimezone(UTC))
    incomplete = comparison_end < end_utc
    previous_start = to_utc(local_datetime(start_day - timedelta(days=7), time.min, tz), tz)
    previous_end = to_utc(
        (comparison_end.astimezone(tz) - timedelta(days=7)), tz
    )
    fallback_currency = await db.scalar(select(ConsultationFee.currency).where(
        ConsultationFee.facility_id == facility.id, ConsultationFee.is_active.is_(True)
    ).limit(1))
    patient_filter = _patient_filter(patient_query)
    current = await _weekly_metrics(db, organization.id, facility.id, start_utc,
        comparison_end, timezone_name, practitioner_uuid, patient_filter, fallback_currency, breakdown=True)
    previous = await _weekly_metrics(db, organization.id, facility.id, previous_start,
        previous_end, timezone_name, practitioner_uuid, patient_filter, fallback_currency, breakdown=False)
    daily = []
    for offset in range(7):
        day = start_day + timedelta(days=offset)
        daily.append({"date": day.isoformat(), "metrics": current["days"].get(day, {
            **{key: 0 for key in ("visits_total", "completed_visits", "unique_visited_patients", "scheduled_appointments", "cancelled_appointments", "no_show_appointments")},
            **(lambda money: money if money else {key: None for key in (
                "billed_amount", "payments_received", "refunds", "net_collections", "currency"
            )})(_single_currency_money({}, {}, {}, fallback_currency)[0]),
        })})
    return {"success": True, "data": {
        "timezone": timezone_name,
        "week": {"local_from": start_day.isoformat(), "local_to": (end_day - timedelta(days=1)).isoformat(),
                 "utc_from": start_utc.isoformat(), "utc_to_exclusive": end_utc.isoformat(),
                 "incomplete": incomplete, "as_of_utc": comparison_end.isoformat()},
        "availability": {"money": current["money_unavailable_reason"] is None,
                         "reason": current["money_unavailable_reason"], "amount_unit": "minor"},
        "metrics": current["metrics"],
        "daily": daily,
        "practitioners": [{"practitioner_uuid": str(key) if key else None, "metrics": value}
                          for key, value in sorted(current["practitioners"].items(), key=lambda item: str(item[0]))],
        "previous_period": {"local_from": (start_day - timedelta(days=7)).isoformat(),
                            "utc_from": previous_start.isoformat(), "utc_to_exclusive": previous_end.isoformat(),
                            "metrics": previous["metrics"],
                            "availability": {"money": previous["money_unavailable_reason"] is None,
                                             "reason": previous["money_unavailable_reason"], "amount_unit": "minor"}},
        "date_basis": {"visits": "encounter.started_at", "appointments": "appointment.scheduled_start",
                       "billed_amount": "invoice.issued_at", "payments_received": "payment.received_at",
                       "refunds": "refund.refunded_at"},
    }}
