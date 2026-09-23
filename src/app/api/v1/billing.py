from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import String

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import (
    DEFAULT_TIMEZONE,
    local_datetime,
    timezone,
    to_timezone,
    to_utc,
)
from ...domains.governance.audit import record_audit
from ...models.billing import ConsultationFee, Invoice, Payment, Refund
from ...models.care import Encounter, Practitioner
from ...models.identity import Patient, Person, UserAccount
from ...models.masters import Specialty
from ...models.organization import (
    Facility,
    FacilitySchedule,
    StaffAssignment,
    StaffMember,
)
from ...schemas.billing import ConsultationFeeInput, PaymentInput, RefundInput
from .bootstrap import ADMIN_ROLES
from .patients import _escape_like

router = APIRouter(tags=["billing"])
BILLING_ROLES = {*ADMIN_ROLES, "billing_staff"}


async def _billing_read_context(db: AsyncSession, account: UserAccount, facility_uuid: UUID):
    staff = await db.scalar(select(StaffMember).join(
        Facility, Facility.organization_id == StaffMember.organization_id).where(
        Facility.id == facility_uuid, StaffMember.user_account_id == account.id,
        StaffMember.is_active.is_(True)))
    if staff is None:
        raise _error(403, "BILLING_ACCESS_REQUIRED", "Billing access is required.")
    permitted = await db.scalar(select(StaffAssignment.id).where(
        StaffAssignment.staff_member_id == staff.id,
        StaffAssignment.is_active.is_(True),
        StaffAssignment.role_code.in_(BILLING_ROLES),
        or_(StaffAssignment.facility_id == facility_uuid,
            and_(StaffAssignment.facility_id.is_(None), StaffAssignment.role_code.in_(ADMIN_ROLES))),
    ))
    if permitted is None:
        raise _error(403, "BILLING_ACCESS_REQUIRED", "Billing access is required.")
    row = (await db.execute(select(Facility, FacilitySchedule.timezone)
        .outerjoin(FacilitySchedule, FacilitySchedule.facility_id == Facility.id)
        .where(Facility.id == facility_uuid, Facility.organization_id == staff.organization_id,
               Facility.is_active.is_(True)))).first()
    if row is None:
        raise _error(404, "FACILITY_NOT_FOUND", "Facility not found or inactive.")
    return staff.organization_id, row[0], row[1] or DEFAULT_TIMEZONE


def _balance_columns(as_of: datetime | None = None):
    paid = select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
        Payment.invoice_id == Invoice.id, Payment.status == "captured"
    )
    refunded = select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
        Refund.invoice_id == Invoice.id, Refund.status == "completed"
    )
    if as_of is not None:
        paid = paid.where(Payment.received_at <= as_of)
        refunded = refunded.where(Refund.refunded_at <= as_of)
    paid = paid.correlate(Invoice).scalar_subquery()
    refunded = refunded.correlate(Invoice).scalar_subquery()
    return paid, refunded, func.greatest(Invoice.amount_minor - paid + refunded, 0)


def _invoice_demo_provenance():
    seeded = select(Payment.id).where(
        Payment.invoice_id == Invoice.id,
        Payment.idempotency_key == func.concat("demo-encounter-", cast(Invoice.encounter_id, String)),
    ).correlate(Invoice).exists()
    return case((seeded, "synthetic_demo"), else_="unknown")


def _local_bounds(date_from: date | None, date_to: date | None, timezone_name: str):
    if date_from and date_to and date_from > date_to:
        raise _error(422, "INVALID_DATE_RANGE", "date_from must be on or before date_to.")
    tz = timezone(timezone_name)
    return (to_utc(local_datetime(date_from, time.min, tz), tz) if date_from else None,
            to_utc(local_datetime(date_to + timedelta(days=1), time.min, tz), tz) if date_to else None)


def _billing_patient_filter(query: str | None):
    if query is None:
        return None
    if not query.strip():
        raise _error(422, "INVALID_PATIENT_QUERY", "patient_query cannot be blank.")
    value = f"%{_escape_like(query.strip())}%"
    return or_(Patient.mrn.ilike(value, escape="\\"), Person.first_name.ilike(value, escape="\\"),
               Person.last_name.ilike(value, escape="\\"))


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


async def _access(
    db: AsyncSession, account: UserAccount, facility_uuid: UUID
) -> tuple[UUID, Facility, set[str], StaffMember]:
    staff = await db.scalar(
        select(StaffMember).where(
            StaffMember.user_account_id == account.id,
            StaffMember.is_active.is_(True),
        )
    )
    if staff is None:
        raise _error(403, "FORBIDDEN", "Active staff access is required.")
    assignments = (
        await db.scalars(
            select(StaffAssignment).where(
                StaffAssignment.staff_member_id == staff.id,
                StaffAssignment.is_active.is_(True),
            )
        )
    ).all()
    roles = {assignment.role_code for assignment in assignments}
    is_admin = bool(roles.intersection(ADMIN_ROLES))
    if not is_admin and not any(
        assignment.facility_id is None or assignment.facility_id == facility_uuid
        for assignment in assignments
    ):
        raise _error(403, "FORBIDDEN", "Facility access is not permitted.")
    facility = await db.scalar(
        select(Facility).where(
            Facility.id == facility_uuid,
            Facility.organization_id == staff.organization_id,
            Facility.is_active.is_(True),
        )
    )
    if facility is None:
        raise _error(404, "FACILITY_NOT_FOUND", "Facility not found or inactive.")
    return staff.organization_id, facility, roles, staff


def _fee_item(fee: ConsultationFee) -> dict[str, Any]:
    return {
        "uuid": str(fee.id),
        "organization_uuid": str(fee.organization_id),
        "facility_uuid": str(fee.facility_id),
        "scope_type": fee.scope_type,
        "practitioner_uuid": str(fee.practitioner_id) if fee.practitioner_id else None,
        "specialty_uuid": str(fee.specialty_id) if fee.specialty_id else None,
        "amount_minor": fee.amount_minor,
        "currency": fee.currency,
        "effective_from": fee.effective_from.isoformat(),
        "effective_to": fee.effective_to.isoformat() if fee.effective_to else None,
        "is_active": fee.is_active,
    }


async def _resolve_fee(
    db: AsyncSession, encounter: Encounter
) -> ConsultationFee | None:
    practitioner = (
        await db.get(Practitioner, encounter.practitioner_id)
        if encounter.practitioner_id
        else None
    )
    timezone_name = await db.scalar(
        select(FacilitySchedule.timezone).where(
            FacilitySchedule.facility_id == encounter.facility_id
        )
    )
    visit_day = to_timezone(
        encounter.started_at, timezone(timezone_name or DEFAULT_TIMEZONE)
    ).date()
    candidates = (
        await db.scalars(
            select(ConsultationFee)
            .where(
                ConsultationFee.organization_id == encounter.organization_id,
                ConsultationFee.facility_id == encounter.facility_id,
                ConsultationFee.effective_from <= visit_day,
                or_(
                    ConsultationFee.effective_to.is_(None),
                    ConsultationFee.effective_to >= visit_day,
                ),
                or_(
                    ConsultationFee.scope_type == "facility",
                    and_(
                        ConsultationFee.scope_type == "practitioner",
                        ConsultationFee.practitioner_id == encounter.practitioner_id,
                    ),
                    and_(
                        ConsultationFee.scope_type == "specialty",
                        ConsultationFee.specialty_id
                        == (practitioner.specialty_id if practitioner else None),
                    ),
                ),
            )
            .order_by(ConsultationFee.effective_from.desc())
        )
    ).all()
    priority = {"practitioner": 0, "specialty": 1, "facility": 2}
    return min(candidates, key=lambda fee: priority[fee.scope_type], default=None)


async def _invoice_totals(db: AsyncSession, invoice_id: UUID) -> tuple[int, int]:
    paid = int(
        await db.scalar(
            select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
                Payment.invoice_id == invoice_id, Payment.status == "captured"
            )
        )
        or 0
    )
    refunded = int(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
                Refund.invoice_id == invoice_id, Refund.status == "completed"
            )
        )
        or 0
    )
    return paid, refunded


async def _refresh_invoice_status(
    db: AsyncSession, invoice: Invoice
) -> tuple[int, int]:
    paid, refunded = await _invoice_totals(db, invoice.id)
    net = paid - refunded
    invoice.status = (
        "paid"
        if net >= invoice.amount_minor
        else "partially_paid"
        if net > 0
        else "issued"
    )
    return paid, refunded


def _invoice_item(invoice: Invoice, paid: int = 0, refunded: int = 0) -> dict[str, Any]:
    return {
        "uuid": str(invoice.id),
        "encounter_uuid": str(invoice.encounter_id),
        "facility_uuid": str(invoice.facility_id),
        "patient_uuid": str(invoice.patient_id),
        "practitioner_uuid": str(invoice.practitioner_id)
        if invoice.practitioner_id
        else None,
        "consultation_fee_uuid": str(invoice.consultation_fee_id)
        if invoice.consultation_fee_id
        else None,
        "amount_minor": invoice.amount_minor,
        "currency": invoice.currency,
        "status": invoice.status,
        "issued_at": invoice.issued_at.isoformat(),
        "paid_minor": paid,
        "refunded_minor": refunded,
        "net_collected_minor": paid - refunded,
        "balance_minor": max(invoice.amount_minor - (paid - refunded), 0),
    }


@router.get("/billing/invoices")
async def list_invoices(
    facility_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    date_from: date | None = None,
    date_to: date | None = None,
    patient_query: str | None = Query(default=None, max_length=120),
    status: Literal["issued", "partially_paid", "paid", "voided"] | None = None,
    outstanding_only: bool = False,
    sort_by: Literal["issued_at", "amount_minor", "balance_minor", "status", "patient_name"] = "issued_at",
    sort_order: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    organization_id, facility, timezone_name = await _billing_read_context(db, account, facility_uuid)
    start, end = _local_bounds(date_from, date_to, timezone_name)
    patient_filter = _billing_patient_filter(patient_query)
    paid, refunded, balance = _balance_columns()
    filters = [Invoice.organization_id == organization_id, Invoice.facility_id == facility.id]
    if start is not None:
        filters.append(Invoice.issued_at >= start)
    if end is not None:
        filters.append(Invoice.issued_at < end)
    if status:
        filters.append(Invoice.status == status)
    if outstanding_only:
        filters.extend((Invoice.status != "voided", balance > 0))
    if patient_filter is not None:
        filters.append(patient_filter)
    query = (select(Invoice, Patient, Person, paid.label("paid"), refunded.label("refunded"),
                    balance.label("balance"), _invoice_demo_provenance().label("demo_provenance"))
             .join(Patient, and_(Patient.id == Invoice.patient_id, Patient.organization_id == Invoice.organization_id))
             .join(Person, and_(Person.id == Patient.person_id, Person.organization_id == Invoice.organization_id))
             .where(*filters))
    sort = {"issued_at": Invoice.issued_at, "amount_minor": Invoice.amount_minor,
            "balance_minor": balance, "status": Invoice.status,
            "patient_name": func.lower(Person.first_name + " " + func.coalesce(Person.last_name, ""))}[sort_by]
    rows = (await db.execute(query.order_by(sort.asc() if sort_order == "asc" else sort.desc(), Invoice.id.desc())
                             .offset((page - 1) * page_size).limit(page_size))).all()
    count_query = select(func.count()).select_from(Invoice)
    if patient_filter is not None:
        count_query = count_query.join(Patient, and_(Patient.id == Invoice.patient_id, Patient.organization_id == Invoice.organization_id)).join(
            Person, and_(Person.id == Patient.person_id, Person.organization_id == Invoice.organization_id))
    total = int(await db.scalar(count_query.where(*filters)) or 0)
    items = []
    for invoice, patient, person, paid_minor, refunded_minor, balance_minor, provenance in rows:
        item = _invoice_item(invoice, int(paid_minor), int(refunded_minor))
        item.update({"patient": {"uuid": str(patient.id), "display_name": f"{person.first_name} {person.last_name or ''}".strip(), "mrn": patient.mrn},
                     "balance_minor": int(balance_minor), "invoice_number": None, "due_date": None,
                     "demo_provenance": provenance})
        items.append(item)
    return {"success": True, "data": {"timezone": timezone_name, "items": items},
            "meta": {"page": page, "page_size": page_size, "total": total,
                     "total_pages": (total + page_size - 1) // page_size}}


@router.get("/billing/invoices/{invoice_uuid}")
async def billing_invoice_detail(
    invoice_uuid: UUID,
    facility_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization_id, facility, _ = await _billing_read_context(db, account, facility_uuid)
    row = (await db.execute(select(Invoice, Patient, Person)
        .join(Patient, and_(Patient.id == Invoice.patient_id, Patient.organization_id == Invoice.organization_id))
        .join(Person, and_(Person.id == Patient.person_id, Person.organization_id == Invoice.organization_id))
        .where(Invoice.id == invoice_uuid, Invoice.organization_id == organization_id,
               Invoice.facility_id == facility.id))).first()
    if row is None:
        raise _error(404, "INVOICE_NOT_FOUND", "Invoice not found.")
    invoice, patient, person = row
    paid, refunded = await _invoice_totals(db, invoice.id)
    item = _invoice_item(invoice, paid, refunded)
    provenance = await db.scalar(select(_invoice_demo_provenance()).where(Invoice.id == invoice.id))
    item.update({"patient": {"uuid": str(patient.id), "display_name": f"{person.first_name} {person.last_name or ''}".strip(), "mrn": patient.mrn},
                 "invoice_number": None, "due_date": None, "demo_provenance": provenance})
    return {"success": True, "data": item, "meta": {}}


@router.get("/billing/outstanding")
async def outstanding_snapshot(
    facility_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization_id, facility, timezone_name = await _billing_read_context(db, account, facility_uuid)
    as_of = datetime.now(UTC)
    _, _, balance = _balance_columns(as_of)
    balances = (select(Invoice.currency.label("currency"), balance.label("balance"))
                .where(Invoice.organization_id == organization_id, Invoice.facility_id == facility.id,
                       Invoice.status != "voided", Invoice.issued_at <= as_of).subquery())
    rows = (await db.execute(select(balances.c.currency, func.sum(balances.c.balance))
                             .group_by(balances.c.currency))).all()
    return {"success": True, "data": {"as_of_utc": as_of.isoformat(),
            "timezone": timezone_name, "amount_unit": "minor",
            "balances": [{"currency": currency, "amount_minor": int(amount or 0)} for currency, amount in rows],
            "historical_available": False}, "meta": {}}


@router.get("/billing/transactions")
async def list_billing_transactions(
    facility_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    date_from: date | None = None,
    date_to: date | None = None,
    patient_query: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> dict[str, Any]:
    organization_id, facility, timezone_name = await _billing_read_context(db, account, facility_uuid)
    start, end = _local_bounds(date_from, date_to, timezone_name)
    patient_filter = _billing_patient_filter(patient_query)
    def transaction_query(model, kind, instant, state, sign):
        filters = [model.organization_id == organization_id, model.facility_id == facility.id,
                   model.status == state]
        if start is not None:
            filters.append(instant >= start)
        if end is not None:
            filters.append(instant < end)
        if patient_filter is not None:
            filters.append(patient_filter)
        return (select(model.id.label("uuid"), model.invoice_id.label("invoice_uuid"),
                       Invoice.patient_id.label("patient_uuid"),
                       (model.amount_minor * sign).label("amount_minor"), model.currency.label("currency"),
                       instant.label("occurred_at"), literal(kind).label("kind"),
                       model.idempotency_key.label("reference"))
                .join(Invoice, and_(Invoice.id == model.invoice_id,
                                    Invoice.organization_id == model.organization_id,
                                    Invoice.facility_id == model.facility_id))
                .join(Patient, and_(Patient.id == Invoice.patient_id, Patient.organization_id == Invoice.organization_id))
                .join(Person, and_(Person.id == Patient.person_id, Person.organization_id == Invoice.organization_id))
                .where(*filters))
    transactions = union_all(transaction_query(Payment, "payment", Payment.received_at, "captured", 1),
                             transaction_query(Refund, "refund", Refund.refunded_at, "completed", -1)).subquery()
    totals = (await db.execute(select(transactions.c.currency, transactions.c.kind,
                                      func.sum(transactions.c.amount_minor))
                               .group_by(transactions.c.currency, transactions.c.kind))).all()
    total = int(await db.scalar(select(func.count()).select_from(transactions)) or 0)
    rows = (await db.execute(select(transactions).order_by(transactions.c.occurred_at.desc(), transactions.c.uuid.desc())
                             .offset((page - 1) * page_size).limit(page_size))).mappings().all()
    return {"success": True, "data": {"timezone": timezone_name,
            "method_available": False,
            "totals": [{"currency": currency, "kind": kind, "amount_minor": int(amount or 0)}
                       for currency, kind, amount in totals],
            "items": [{**{key: str(value) if isinstance(value, UUID) else value.isoformat() if isinstance(value, datetime) else value
                           for key, value in row.items()},
                       "method": None, "demo_provenance": "synthetic_demo" if row["reference"].startswith("demo-encounter-") else "unknown"}
                      for row in rows]},
            "meta": {"page": page, "page_size": page_size, "total": total,
                     "total_pages": (total + page_size - 1) // page_size}}


@router.get("/facilities/{facility_uuid}/consultation-fees")
async def list_consultation_fees(
    facility_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization_id, facility, _, _ = await _access(db, account, facility_uuid)
    fees = (
        await db.scalars(
            select(ConsultationFee)
            .where(
                ConsultationFee.organization_id == organization_id,
                ConsultationFee.facility_id == facility.id,
                ConsultationFee.is_active.is_(True),
            )
            .order_by(ConsultationFee.scope_type, ConsultationFee.effective_from.desc())
        )
    ).all()
    return {
        "success": True,
        "data": {"items": [_fee_item(fee) for fee in fees]},
        "meta": {"count": len(fees)},
    }


@router.put("/facilities/{facility_uuid}/consultation-fees")
async def put_consultation_fee(
    facility_uuid: UUID,
    payload: ConsultationFeeInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization_id, facility, roles, _ = await _access(db, account, facility_uuid)
    is_admin = bool(roles.intersection(ADMIN_ROLES))
    practitioner = None
    if payload.scope_type == "practitioner":
        practitioner = await db.scalar(
            select(Practitioner).where(
                Practitioner.id == payload.practitioner_uuid,
                Practitioner.organization_id == organization_id,
                Practitioner.is_active.is_(True),
            )
        )
        if practitioner is None:
            raise _error(
                404, "PRACTITIONER_NOT_FOUND", "Practitioner not found or inactive."
            )
        if not is_admin and practitioner.user_account_id != account.id:
            raise _error(
                403, "FORBIDDEN", "Doctors may only manage their own consultation fee."
            )
    elif not is_admin:
        raise _error(
            403,
            "FORBIDDEN",
            "Administrator access is required for facility and specialty fees.",
        )
    if payload.scope_type == "specialty" and not await db.scalar(
        select(Specialty.id).where(
            Specialty.id == payload.specialty_uuid, Specialty.is_active.is_(True)
        )
    ):
        raise _error(404, "SPECIALTY_NOT_FOUND", "Specialty not found or inactive.")

    other_currency = await db.scalar(
        select(ConsultationFee.currency).where(
            ConsultationFee.facility_id == facility.id,
            ConsultationFee.is_active.is_(True),
            ConsultationFee.currency != payload.currency,
        )
    )
    if other_currency:
        raise _error(
            409,
            "FACILITY_CURRENCY_MISMATCH",
            "All active fees at a facility must use one currency.",
        )

    scope_filters = [
        ConsultationFee.facility_id == facility.id,
        ConsultationFee.scope_type == payload.scope_type,
        ConsultationFee.is_active.is_(True),
    ]
    if payload.scope_type == "practitioner":
        scope_filters.append(
            ConsultationFee.practitioner_id == payload.practitioner_uuid
        )
    elif payload.scope_type == "specialty":
        scope_filters.append(ConsultationFee.specialty_id == payload.specialty_uuid)
    current = await db.scalar(
        select(ConsultationFee).where(*scope_filters).with_for_update()
    )
    now = datetime.now(UTC)
    if current and current.effective_from == payload.effective_from:
        current.amount_minor, current.currency, current.updated_at = (
            payload.amount_minor,
            payload.currency,
            now,
        )
        fee = current
    else:
        if current and payload.effective_from <= current.effective_from:
            raise _error(
                409,
                "FEE_EFFECTIVE_DATE_CONFLICT",
                "A replacement fee must start after the current fee.",
            )
        if current:
            current.is_active = False
            current.effective_to = payload.effective_from - timedelta(days=1)
            current.updated_at = now
        fee = ConsultationFee(
            organization_id=organization_id,
            facility_id=facility.id,
            scope_type=payload.scope_type,
            practitioner_id=payload.practitioner_uuid,
            specialty_id=payload.specialty_uuid,
            amount_minor=payload.amount_minor,
            currency=payload.currency,
            effective_from=payload.effective_from,
            created_by_user_id=account.id,
        )
        db.add(fee)
    await record_audit(
        db,
        organization_id=organization_id,
        actor_user_id=account.id,
        action="consultation_fee.updated" if current else "consultation_fee.created",
        resource_type="consultation_fee",
        resource_id=fee.id,
        facility_id=facility.id,
        details={
            "scope_type": fee.scope_type,
            "amount_minor": fee.amount_minor,
            "currency": fee.currency,
        },
    )
    await db.commit()
    return {"success": True, "data": _fee_item(fee), "meta": {}}


@router.post("/encounters/{encounter_uuid}/invoice")
async def create_encounter_invoice(
    encounter_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    encounter = await db.scalar(
        select(Encounter).where(Encounter.id == encounter_uuid).with_for_update()
    )
    if encounter is None:
        raise _error(404, "ENCOUNTER_NOT_FOUND", "Encounter not found.")
    _, _, roles, _ = await _access(db, account, encounter.facility_id)
    own_practitioner = await db.scalar(
        select(Practitioner.id).where(
            Practitioner.id == encounter.practitioner_id,
            Practitioner.user_account_id == account.id,
        )
    )
    if not roles.intersection(BILLING_ROLES) and own_practitioner is None:
        raise _error(
            403,
            "BILLING_ACCESS_REQUIRED",
            "Billing or encounter practitioner access is required.",
        )
    if encounter.status != "completed":
        raise _error(
            409, "ENCOUNTER_NOT_COMPLETED", "Only completed encounters can be invoiced."
        )
    existing = await db.scalar(
        select(Invoice).where(Invoice.encounter_id == encounter.id)
    )
    if existing:
        paid, refunded = await _invoice_totals(db, existing.id)
        return {
            "success": True,
            "data": _invoice_item(existing, paid, refunded),
            "meta": {"idempotent": True},
        }
    fee = await _resolve_fee(db, encounter)
    if fee is None:
        raise _error(
            409,
            "CONSULTATION_FEE_NOT_CONFIGURED",
            "No effective consultation fee exists for this encounter.",
        )
    invoice = Invoice(
        organization_id=encounter.organization_id,
        facility_id=encounter.facility_id,
        encounter_id=encounter.id,
        patient_id=encounter.patient_id,
        practitioner_id=encounter.practitioner_id,
        consultation_fee_id=fee.id,
        amount_minor=fee.amount_minor,
        currency=fee.currency,
        created_by_user_id=account.id,
    )
    db.add(invoice)
    await record_audit(
        db,
        organization_id=invoice.organization_id,
        actor_user_id=account.id,
        action="invoice.created",
        resource_type="invoice",
        resource_id=invoice.id,
        facility_id=invoice.facility_id,
        patient_id=invoice.patient_id,
        details={
            "encounter_uuid": str(encounter.id),
            "amount_minor": invoice.amount_minor,
            "currency": invoice.currency,
        },
    )
    await db.commit()
    return {
        "success": True,
        "data": _invoice_item(invoice),
        "meta": {"idempotent": False},
    }


@router.get("/encounters/{encounter_uuid}/invoice")
async def get_encounter_invoice(
    encounter_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    invoice = await db.scalar(
        select(Invoice).where(Invoice.encounter_id == encounter_uuid)
    )
    if invoice is None:
        raise _error(404, "INVOICE_NOT_FOUND", "Invoice not found.")
    await _access(db, account, invoice.facility_id)
    paid, refunded = await _invoice_totals(db, invoice.id)
    return {"success": True, "data": _invoice_item(invoice, paid, refunded), "meta": {}}


@router.post("/invoices/{invoice_uuid}/payments")
async def create_payment(
    invoice_uuid: UUID,
    payload: PaymentInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == invoice_uuid).with_for_update()
    )
    if invoice is None:
        raise _error(404, "INVOICE_NOT_FOUND", "Invoice not found.")
    _, _, roles, _ = await _access(db, account, invoice.facility_id)
    if not roles.intersection(BILLING_ROLES):
        raise _error(403, "BILLING_ACCESS_REQUIRED", "Billing access is required.")
    existing = await db.scalar(
        select(Payment).where(
            Payment.organization_id == invoice.organization_id,
            Payment.idempotency_key == payload.idempotency_key,
        )
    )
    if existing:
        if existing.invoice_id != invoice.id:
            raise _error(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key belongs to another invoice.",
            )
        return {
            "success": True,
            "data": {"uuid": str(existing.id), "status": existing.status},
            "meta": {"idempotent": True},
        }
    if invoice.status == "voided":
        raise _error(409, "INVOICE_VOIDED", "A voided invoice cannot receive payment.")
    paid, refunded = await _invoice_totals(db, invoice.id)
    if paid - refunded + payload.amount_minor > invoice.amount_minor:
        raise _error(
            409, "PAYMENT_EXCEEDS_BALANCE", "Payment exceeds the invoice balance."
        )
    payment = Payment(
        organization_id=invoice.organization_id,
        facility_id=invoice.facility_id,
        invoice_id=invoice.id,
        amount_minor=payload.amount_minor,
        currency=invoice.currency,
        idempotency_key=payload.idempotency_key,
        received_at=payload.received_at or datetime.now(UTC),
        created_by_user_id=account.id,
        provenance="synthetic_demo" if payload.idempotency_key.startswith("demo-encounter-") else "recorded",
    )
    db.add(payment)
    await db.flush()
    from .earnings import record_earning_event
    await record_earning_event(db, payment, "payment", payment.id,
                               payment.amount_minor, payment.received_at)
    await _refresh_invoice_status(db, invoice)
    await record_audit(
        db,
        organization_id=invoice.organization_id,
        actor_user_id=account.id,
        action="payment.captured",
        resource_type="payment",
        resource_id=payment.id,
        facility_id=invoice.facility_id,
        patient_id=invoice.patient_id,
        details={
            "invoice_uuid": str(invoice.id),
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
        },
    )
    await db.commit()
    return {
        "success": True,
        "data": {
            "uuid": str(payment.id),
            "invoice_uuid": str(invoice.id),
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
            "status": payment.status,
            "received_at": payment.received_at.isoformat(),
        },
        "meta": {"idempotent": False},
    }


@router.post("/payments/{payment_uuid}/refunds")
async def create_refund(
    payment_uuid: UUID,
    payload: RefundInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    payment = await db.scalar(
        select(Payment).where(Payment.id == payment_uuid).with_for_update()
    )
    if payment is None:
        raise _error(404, "PAYMENT_NOT_FOUND", "Payment not found.")
    _, _, roles, _ = await _access(db, account, payment.facility_id)
    if not roles.intersection(BILLING_ROLES):
        raise _error(403, "BILLING_ACCESS_REQUIRED", "Billing access is required.")
    existing = await db.scalar(
        select(Refund).where(
            Refund.organization_id == payment.organization_id,
            Refund.idempotency_key == payload.idempotency_key,
        )
    )
    if existing:
        if existing.payment_id != payment.id:
            raise _error(
                409,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key belongs to another payment.",
            )
        return {
            "success": True,
            "data": {"uuid": str(existing.id), "status": existing.status},
            "meta": {"idempotent": True},
        }
    if payment.status != "captured":
        raise _error(
            409, "PAYMENT_NOT_REFUNDABLE", "Only captured payments can be refunded."
        )
    refunded = int(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
                Refund.payment_id == payment.id, Refund.status == "completed"
            )
        )
        or 0
    )
    if refunded + payload.amount_minor > payment.amount_minor:
        raise _error(
            409, "REFUND_EXCEEDS_PAYMENT", "Refund exceeds the captured payment amount."
        )
    refund = Refund(
        organization_id=payment.organization_id,
        facility_id=payment.facility_id,
        invoice_id=payment.invoice_id,
        payment_id=payment.id,
        amount_minor=payload.amount_minor,
        currency=payment.currency,
        idempotency_key=payload.idempotency_key,
        reason=payload.reason,
        refunded_at=payload.refunded_at or datetime.now(UTC),
        created_by_user_id=account.id,
    )
    db.add(refund)
    await db.flush()
    from .earnings import record_earning_event
    await record_earning_event(db, payment, "refund", refund.id,
                               refund.amount_minor, refund.refunded_at)
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == payment.invoice_id).with_for_update()
    )
    if invoice:
        await _refresh_invoice_status(db, invoice)
        await record_audit(
            db,
            organization_id=invoice.organization_id,
            actor_user_id=account.id,
            action="refund.completed",
            resource_type="refund",
            resource_id=refund.id,
            facility_id=invoice.facility_id,
            patient_id=invoice.patient_id,
            details={
                "payment_uuid": str(payment.id),
                "amount_minor": refund.amount_minor,
                "currency": refund.currency,
            },
        )
    await db.commit()
    return {
        "success": True,
        "data": {
            "uuid": str(refund.id),
            "payment_uuid": str(payment.id),
            "invoice_uuid": str(payment.invoice_id),
            "amount_minor": refund.amount_minor,
            "currency": refund.currency,
            "status": refund.status,
            "refunded_at": refund.refunded_at.isoformat(),
        },
        "meta": {"idempotent": False},
    }


@router.post("/invoices/{invoice_uuid}/void")
async def void_invoice(
    invoice_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == invoice_uuid).with_for_update()
    )
    if invoice is None:
        raise _error(404, "INVOICE_NOT_FOUND", "Invoice not found.")
    _, _, roles, _ = await _access(db, account, invoice.facility_id)
    if not roles.intersection(BILLING_ROLES):
        raise _error(403, "BILLING_ACCESS_REQUIRED", "Billing access is required.")
    paid, _ = await _invoice_totals(db, invoice.id)
    if paid:
        raise _error(
            409,
            "INVOICE_HAS_PAYMENTS",
            "Void captured payments before voiding the invoice.",
        )
    if invoice.status != "voided":
        invoice.status, invoice.voided_at = "voided", datetime.now(UTC)
        await record_audit(
            db,
            organization_id=invoice.organization_id,
            actor_user_id=account.id,
            action="invoice.voided",
            resource_type="invoice",
            resource_id=invoice.id,
            facility_id=invoice.facility_id,
            patient_id=invoice.patient_id,
        )
        await db.commit()
    return {"success": True, "data": _invoice_item(invoice), "meta": {}}


@router.post("/payments/{payment_uuid}/void")
async def void_payment(
    payment_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    payment = await db.scalar(
        select(Payment).where(Payment.id == payment_uuid).with_for_update()
    )
    if payment is None:
        raise _error(404, "PAYMENT_NOT_FOUND", "Payment not found.")
    _, _, roles, _ = await _access(db, account, payment.facility_id)
    if not roles.intersection(BILLING_ROLES):
        raise _error(403, "BILLING_ACCESS_REQUIRED", "Billing access is required.")
    refunded = int(
        await db.scalar(
            select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
                Refund.payment_id == payment.id, Refund.status == "completed"
            )
        )
        or 0
    )
    if refunded:
        raise _error(
            409,
            "PAYMENT_HAS_REFUNDS",
            "Void completed refunds before voiding the payment.",
        )
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == payment.invoice_id).with_for_update()
    )
    if payment.status != "voided":
        payment.status, payment.voided_at = "voided", datetime.now(UTC)
        await db.flush()
        from .earnings import record_earning_event
        await record_earning_event(db, payment, "payment_void", payment.id,
                                   payment.amount_minor, payment.voided_at)
        if invoice:
            await _refresh_invoice_status(db, invoice)
        await record_audit(
            db,
            organization_id=payment.organization_id,
            actor_user_id=account.id,
            action="payment.voided",
            resource_type="payment",
            resource_id=payment.id,
            facility_id=payment.facility_id,
            patient_id=invoice.patient_id if invoice else None,
        )
        await db.commit()
    return {
        "success": True,
        "data": {"uuid": str(payment.id), "status": payment.status},
        "meta": {},
    }


@router.post("/refunds/{refund_uuid}/void")
async def void_refund(
    refund_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    refund = await db.scalar(
        select(Refund).where(Refund.id == refund_uuid).with_for_update()
    )
    if refund is None:
        raise _error(404, "REFUND_NOT_FOUND", "Refund not found.")
    _, _, roles, _ = await _access(db, account, refund.facility_id)
    if not roles.intersection(BILLING_ROLES):
        raise _error(403, "BILLING_ACCESS_REQUIRED", "Billing access is required.")
    invoice = await db.scalar(
        select(Invoice).where(Invoice.id == refund.invoice_id).with_for_update()
    )
    if refund.status != "voided":
        refund.status, refund.voided_at = "voided", datetime.now(UTC)
        await db.flush()
        payment = await db.get(Payment, refund.payment_id)
        from .earnings import record_earning_event
        await record_earning_event(db, payment, "refund_void", refund.id,
                                   refund.amount_minor, refund.voided_at)
        if invoice:
            await _refresh_invoice_status(db, invoice)
        await record_audit(
            db,
            organization_id=refund.organization_id,
            actor_user_id=account.id,
            action="refund.voided",
            resource_type="refund",
            resource_id=refund.id,
            facility_id=refund.facility_id,
            patient_id=invoice.patient_id if invoice else None,
        )
        await db.commit()
    return {
        "success": True,
        "data": {"uuid": str(refund.id), "status": refund.status},
        "meta": {},
    }
