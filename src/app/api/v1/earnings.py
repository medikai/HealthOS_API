"""Consultation compensation entries and recorded external payouts."""
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import DEFAULT_TIMEZONE, timezone, to_timezone
from ...domains.governance.audit import record_audit
from ...models.billing import (
    CompensationPolicy,
    ConsultationFee,
    EarningEntry,
    Invoice,
    Payment,
    PayoutAllocation,
    PayoutEntry,
    Refund,
)
from ...models.care import Encounter, Practitioner
from ...models.identity import UserAccount
from ...models.organization import FacilitySchedule
from .billing import _error, _local_bounds
from .reports import _report_context, _self_report_context

router = APIRouter(tags=["earnings"])


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    practitioner_uuid: UUID
    currency: str = Field(min_length=3, max_length=3)
    basis_points: int = Field(ge=1, le=10000)
    effective_from: date
    effective_to: date | None = None

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("currency must contain three letters")
        return value.upper()


class PayoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    practitioner_uuid: UUID
    currency: str = Field(min_length=3, max_length=3)
    amount_minor: int = Field(gt=0)
    paid_at: datetime
    method: str = Field(min_length=1, max_length=80)
    reference: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("paid_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("paid_at must include a timezone offset")
        return value

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("currency must contain three letters")
        return value.upper()


class ReversalInput(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)


def _round_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator // 2) // denominator


def _refund_entitlement(original_earned: int, payment_minor: int,
                        cumulative_refund_minor: int, previously_reversed: int) -> int:
    target = original_earned if cumulative_refund_minor >= payment_minor else _round_div(
        original_earned * cumulative_refund_minor, payment_minor)
    return -target - previously_reversed


async def _eligible_invoice(db: AsyncSession, payment: Payment) -> tuple[Invoice | None, str | None]:
    row = (await db.execute(select(Invoice, Encounter, ConsultationFee)
        .join(Encounter, Encounter.id == Invoice.encounter_id)
        .outerjoin(ConsultationFee, ConsultationFee.id == Invoice.consultation_fee_id)
        .where(Invoice.id == payment.invoice_id, Invoice.organization_id == payment.organization_id,
               Invoice.facility_id == payment.facility_id))).first()
    if row is None:
        return None, "INVOICE_MISSING"
    invoice, encounter, fee = row
    if payment.provenance != "recorded":
        return None, "UNVERIFIED_PROVENANCE"
    if invoice.status == "voided" or encounter.status != "completed":
        return None, "CONSULTATION_NOT_COMPLETED"
    if not (invoice.practitioner_id and invoice.practitioner_id == encounter.practitioner_id
            and fee and fee.amount_minor == invoice.amount_minor and fee.currency == invoice.currency
            and payment.currency == invoice.currency):
        return None, "AMBIGUOUS_OR_UNSUPPORTED_ALLOCATION"
    return invoice, None


async def record_earning_event(db: AsyncSession, payment: Payment, source_type: str,
                               source_id: UUID, amount_minor: int, occurred_at: datetime) -> str | None:
    """Called inside the existing billing transaction. No entry means excluded with reason."""
    existing = await db.scalar(select(EarningEntry.id).where(
        EarningEntry.source_type == source_type, EarningEntry.source_id == source_id))
    if existing:
        return None
    invoice, reason = await _eligible_invoice(db, payment)
    if reason:
        return reason
    assert invoice is not None
    await db.scalar(select(Practitioner.id).where(Practitioner.id == invoice.practitioner_id).with_for_update())
    if await db.scalar(select(EarningEntry.id).where(
        EarningEntry.source_type == source_type, EarningEntry.source_id == source_id)):
        return None
    if source_type == "payment":
        timezone_name = await db.scalar(select(FacilitySchedule.timezone)
                                        .where(FacilitySchedule.facility_id == invoice.facility_id))
        policy_day = to_timezone(occurred_at, timezone(timezone_name or DEFAULT_TIMEZONE)).date()
        policy = await db.scalar(select(CompensationPolicy).where(
            CompensationPolicy.organization_id == invoice.organization_id,
            CompensationPolicy.facility_id == invoice.facility_id,
            CompensationPolicy.practitioner_id == invoice.practitioner_id,
            CompensationPolicy.currency == invoice.currency,
            CompensationPolicy.effective_from <= policy_day,
            or_(CompensationPolicy.effective_to.is_(None), CompensationPolicy.effective_to >= policy_day)))
        if policy is None:
            return "POLICY_NOT_CONFIGURED"
        base = payment.amount_minor
        amount = _round_div(base * policy.basis_points, 10000)
    else:
        original = await db.scalar(select(EarningEntry).where(
            EarningEntry.source_type == "payment", EarningEntry.source_id == payment.id))
        if original is None:
            return "ORIGINAL_ENTITLEMENT_UNAVAILABLE"
        policy = await db.get(CompensationPolicy, original.policy_id)
        if source_type == "refund":
            prior_base = int(await db.scalar(select(func.coalesce(func.sum(EarningEntry.base_minor), 0)).where(
                EarningEntry.payment_id == payment.id,
                EarningEntry.source_type.in_(("refund", "refund_void")))) or 0)
            refunded = -prior_base + amount_minor
            previous = int(await db.scalar(select(func.coalesce(func.sum(EarningEntry.amount_minor), 0)).where(
                EarningEntry.payment_id == payment.id,
                EarningEntry.source_type.in_(("refund", "refund_void")))) or 0)
            amount = _refund_entitlement(original.amount_minor, payment.amount_minor,
                                         refunded, previous)
            base = -amount_minor
        elif source_type == "refund_void":
            reversed_entry = await db.scalar(select(EarningEntry).where(
                EarningEntry.source_type == "refund", EarningEntry.source_id == source_id))
            if reversed_entry is None:
                return "ORIGINAL_ENTITLEMENT_UNAVAILABLE"
            amount, base = -reversed_entry.amount_minor, amount_minor
        else:  # payment_void
            prior = int(await db.scalar(select(func.coalesce(func.sum(EarningEntry.amount_minor), 0)).where(
                EarningEntry.payment_id == payment.id)) or 0)
            amount, base = -prior, -amount_minor
    db.add(EarningEntry(organization_id=invoice.organization_id, facility_id=invoice.facility_id,
        practitioner_id=invoice.practitioner_id, currency=invoice.currency, source_type=source_type,
        source_id=source_id, payment_id=payment.id, policy_id=policy.id,
        basis_points=policy.basis_points, base_minor=base, amount_minor=amount, occurred_at=occurred_at))
    await db.flush()
    return None


async def _admin(db: AsyncSession, account: UserAccount, facility_uuid: UUID):
    organization, facility, tz_name = await _report_context(db, account, facility_uuid)
    return organization, facility, tz_name


async def _practitioner(db: AsyncSession, organization_id: UUID, practitioner_uuid: UUID) -> Practitioner:
    practitioner = await db.scalar(select(Practitioner).where(
        Practitioner.id == practitioner_uuid, Practitioner.organization_id == organization_id,
        Practitioner.is_active.is_(True)))
    if practitioner is None:
        raise _error(404, "PRACTITIONER_NOT_FOUND", "Practitioner not found.")
    return practitioner


def _policy_item(policy: CompensationPolicy) -> dict[str, Any]:
    return {"uuid": str(policy.id), "practitioner_uuid": str(policy.practitioner_id),
            "currency": policy.currency, "basis_points": policy.basis_points,
            "effective_from": policy.effective_from.isoformat(),
            "effective_to": policy.effective_to.isoformat() if policy.effective_to else None}


@router.post("/facilities/{facility_uuid}/compensation-policies")
async def create_policy(facility_uuid: UUID, payload: PolicyInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)]):
    organization, facility, tz_name = await _admin(db, account, facility_uuid)
    await _practitioner(db, organization.id, payload.practitioner_uuid)
    if payload.effective_from < datetime.now(timezone(tz_name)).date() or (payload.effective_to and payload.effective_to < payload.effective_from):
        raise _error(422, "INVALID_POLICY_DATES", "Policy must start today or later and have a valid end date.")
    # Serialize policy writes; the exclusion constraint is the final overlap guard.
    await db.scalar(select(Practitioner.id).where(Practitioner.id == payload.practitioner_uuid).with_for_update())
    overlap = await db.scalar(select(CompensationPolicy.id).where(
        CompensationPolicy.organization_id == organization.id, CompensationPolicy.facility_id == facility.id,
        CompensationPolicy.practitioner_id == payload.practitioner_uuid,
        CompensationPolicy.currency == payload.currency,
        CompensationPolicy.effective_from <= (payload.effective_to or date.max),
        or_(CompensationPolicy.effective_to.is_(None), CompensationPolicy.effective_to >= payload.effective_from)))
    if overlap:
        raise _error(409, "POLICY_OVERLAP", "Effective periods cannot overlap.")
    policy = CompensationPolicy(organization_id=organization.id, facility_id=facility.id,
        practitioner_id=payload.practitioner_uuid, currency=payload.currency,
        basis_points=payload.basis_points, effective_from=payload.effective_from,
        effective_to=payload.effective_to, created_by_user_id=account.id)
    db.add(policy)
    await db.flush()
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id,
        action="compensation_policy.created", resource_type="compensation_policy", resource_id=policy.id,
        facility_id=facility.id, details=_policy_item(policy))
    await db.commit()
    return {"success": True, "data": _policy_item(policy), "meta": {}}


@router.get("/facilities/{facility_uuid}/compensation-policies")
async def list_policies(facility_uuid: UUID, practitioner_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)]):
    organization, facility, _ = await _admin(db, account, facility_uuid)
    await _practitioner(db, organization.id, practitioner_uuid)
    rows = (await db.scalars(select(CompensationPolicy).where(
        CompensationPolicy.organization_id == organization.id, CompensationPolicy.facility_id == facility.id,
        CompensationPolicy.practitioner_id == practitioner_uuid).order_by(CompensationPolicy.effective_from))).all()
    return {"success": True, "data": {"items": [_policy_item(x) for x in rows]}, "meta": {}}


def _scope(model, org_id, facility_id, practitioner_id, currency):
    return (model.organization_id == org_id, model.facility_id == facility_id,
            model.practitioner_id == practitioner_id, model.currency == currency)


async def _balance(db, org_id, facility_id, practitioner_id, currency, as_of=None):
    earning_time = (EarningEntry.occurred_at <= as_of,) if as_of else ()
    payout_time = (PayoutEntry.paid_at <= as_of,) if as_of else ()
    earned = int(await db.scalar(select(func.coalesce(func.sum(EarningEntry.amount_minor), 0)).where(
        *_scope(EarningEntry, org_id, facility_id, practitioner_id, currency), *earning_time)) or 0)
    paid = int(await db.scalar(select(func.coalesce(func.sum(PayoutEntry.amount_minor), 0)).where(
        *_scope(PayoutEntry, org_id, facility_id, practitioner_id, currency), *payout_time)) or 0)
    return earned - paid


async def _statement(db, org_id, facility_id, practitioner, currency, start, end, timezone_name):
    now = datetime.now(UTC)
    cutoff = min(end, now)
    opening_cutoff = min(start, now)
    entries = (await db.scalars(select(EarningEntry).where(
        *_scope(EarningEntry, org_id, facility_id, practitioner.id, currency),
        EarningEntry.occurred_at >= start, EarningEntry.occurred_at < cutoff)
        .order_by(EarningEntry.occurred_at, EarningEntry.id))).all()
    payouts = (await db.scalars(select(PayoutEntry).where(
        *_scope(PayoutEntry, org_id, facility_id, practitioner.id, currency),
        PayoutEntry.paid_at >= start, PayoutEntry.paid_at < cutoff)
        .order_by(PayoutEntry.paid_at, PayoutEntry.id))).all()
    opening_earned = int(await db.scalar(select(func.coalesce(func.sum(EarningEntry.amount_minor), 0)).where(
        *_scope(EarningEntry, org_id, facility_id, practitioner.id, currency), EarningEntry.occurred_at < opening_cutoff)) or 0)
    opening_paid = int(await db.scalar(select(func.coalesce(func.sum(PayoutEntry.amount_minor), 0)).where(
        *_scope(PayoutEntry, org_id, facility_id, practitioner.id, currency), PayoutEntry.paid_at < opening_cutoff)) or 0)
    earned = sum(x.amount_minor for x in entries)
    paid = sum(x.amount_minor for x in payouts)
    policies = (await db.scalars(select(CompensationPolicy).where(
        *_scope(CompensationPolicy, org_id, facility_id, practitioner.id, currency))
        .order_by(CompensationPolicy.effective_from))).all()
    local_from = to_timezone(start, timezone(timezone_name)).date()
    local_to = to_timezone(end - timedelta(microseconds=1), timezone(timezone_name)).date()
    covered_days = sum(any(p.effective_from <= day and
        (p.effective_to is None or day <= p.effective_to) for p in policies)
        for day in (local_from + timedelta(days=offset)
                    for offset in range((local_to - local_from).days + 1)))
    full_coverage = covered_days == (local_to - local_from).days + 1
    coverage_reason = None if full_coverage else ("POLICY_NOT_CONFIGURED" if not covered_days
                                                  else "PARTIAL_POLICY_COVERAGE")
    excluded = (await db.execute(select(Payment).join(Invoice, Invoice.id == Payment.invoice_id).where(
        Payment.organization_id == org_id, Payment.facility_id == facility_id,
        Invoice.practitioner_id == practitioner.id, Payment.currency == currency,
        Payment.status == "captured", Payment.received_at >= start, Payment.received_at < cutoff))).scalars().all()
    covered_ids = {x.payment_id for x in entries if x.source_type == "payment"}
    excluded_items = []
    for payment in excluded:
        if payment.id not in covered_ids:
            _, reason = await _eligible_invoice(db, payment)
            excluded_items.append({"payment_uuid": str(payment.id), "amount_minor": payment.amount_minor,
                                   "reason": reason or "POLICY_NOT_CONFIGURED_OR_ENTRY_MISSING"})
    if full_coverage and any(x["reason"] == "POLICY_NOT_CONFIGURED_OR_ENTRY_MISSING"
                             for x in excluded_items):
        full_coverage, coverage_reason = False, "INCOMPLETE_EARNING_ENTRIES"
    return {"practitioner": {"uuid": str(practitioner.id), "name": practitioner.person_name},
        "currency": currency, "timezone": timezone_name, "amount_unit": "minor",
        "as_of_utc": now.isoformat(), "period": {"utc_from": start.isoformat(), "utc_to_exclusive": end.isoformat()},
        "availability": {"earnings": full_coverage, "reason": coverage_reason,
                         "coverage": "recorded eligible consultation receipts only"},
        "earned_in_period_minor": earned if full_coverage else None,
        "signed_adjustments_minor": 0, "paid_in_period_minor": paid,
        "opening_balance_minor": opening_earned - opening_paid if policies else None,
        "closing_balance_minor": opening_earned + earned - opening_paid - paid if policies else None,
        "current_balance_minor": await _balance(db, org_id, facility_id, practitioner.id, currency, now)
                                 if policies else None,
        "entries": [{"uuid": str(x.id), "source_type": x.source_type, "source_uuid": str(x.source_id),
                     "occurred_at": x.occurred_at.isoformat(), "base_minor": x.base_minor,
                     "basis_points": x.basis_points, "amount_minor": x.amount_minor} for x in entries],
        "payouts": [{"uuid": str(x.id), "kind": x.kind, "paid_at": x.paid_at.isoformat(),
                     "amount_minor": x.amount_minor, "method": x.method, "reference": x.reference} for x in payouts],
        "excluded": {"count": len(excluded_items), "amount_minor": sum(x["amount_minor"] for x in excluded_items),
                     "items": excluded_items},
        "demo_provenance": "historical_unknown_and_synthetic_excluded"}


async def _read_statement(db, account, facility_uuid, practitioner_uuid, currency, date_from, date_to, own):
    if own:
        organization, facility, tz_name, practitioner = await _self_report_context(db, account, facility_uuid)
        if practitioner_uuid and practitioner_uuid != practitioner.id:
            raise _error(403, "PRACTITIONER_SCOPE_DENIED", "Another practitioner's earnings are not available.")
    else:
        organization, facility, tz_name = await _admin(db, account, facility_uuid)
        practitioner = await _practitioner(db, organization.id, practitioner_uuid)
    if (date_to - date_from).days not in range(366):
        raise _error(422, "INVALID_DATE_RANGE", "Date range must contain 1 to 366 days.")
    start, end = _local_bounds(date_from, date_to, tz_name)
    return await _statement(db, organization.id, facility.id, practitioner, currency.upper(), start, end, tz_name)


@router.get("/reports/my-earnings")
async def my_earnings(facility_uuid: UUID, date_from: date, date_to: date, currency: str,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)], practitioner_uuid: UUID | None = None):
    return {"success": True, "data": await _read_statement(db, account, facility_uuid, practitioner_uuid,
             currency, date_from, date_to, True), "meta": {}}


@router.get("/billing/practitioner-earnings")
async def practitioner_earnings(facility_uuid: UUID, practitioner_uuid: UUID, date_from: date,
    date_to: date, currency: str, account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)]):
    return {"success": True, "data": await _read_statement(db, account, facility_uuid, practitioner_uuid,
             currency, date_from, date_to, False), "meta": {}}


@router.get("/billing/practitioner-payouts")
async def payout_summary(facility_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)]):
    organization, facility, _ = await _admin(db, account, facility_uuid)
    policies = (await db.execute(select(CompensationPolicy.practitioner_id,
        CompensationPolicy.currency, Practitioner.person_name)
        .join(Practitioner, Practitioner.id == CompensationPolicy.practitioner_id)
        .where(CompensationPolicy.organization_id == organization.id,
               CompensationPolicy.facility_id == facility.id,
               Practitioner.organization_id == organization.id)
        .distinct())).all()
    as_of = datetime.now(UTC)
    earned = {(pid, currency): int(amount) for pid, currency, amount in
        (await db.execute(select(EarningEntry.practitioner_id, EarningEntry.currency,
            func.sum(EarningEntry.amount_minor)).where(EarningEntry.organization_id == organization.id,
            EarningEntry.facility_id == facility.id, EarningEntry.occurred_at <= as_of)
            .group_by(EarningEntry.practitioner_id, EarningEntry.currency))).all()}
    paid = {(pid, currency): int(amount) for pid, currency, amount in
        (await db.execute(select(PayoutEntry.practitioner_id, PayoutEntry.currency,
            func.sum(PayoutEntry.amount_minor)).where(PayoutEntry.organization_id == organization.id,
            PayoutEntry.facility_id == facility.id, PayoutEntry.paid_at <= as_of)
            .group_by(PayoutEntry.practitioner_id, PayoutEntry.currency))).all()}
    return {"success": True, "data": {"as_of_utc": as_of.isoformat(), "amount_unit": "minor",
        "items": [{"practitioner_uuid": str(pid), "name": name, "currency": currency,
                   "earned_minor": earned.get((pid, currency), 0),
                   "paid_minor": paid.get((pid, currency), 0),
                   "balance_minor": earned.get((pid, currency), 0) - paid.get((pid, currency), 0)}
                  for pid, currency, name in policies]}, "meta": {}}


@router.post("/billing/practitioner-earnings/catch-up")
async def catch_up(facility_uuid: UUID, practitioner_uuid: UUID, currency: str,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)]):
    organization, facility, _ = await _admin(db, account, facility_uuid)
    await _practitioner(db, organization.id, practitioner_uuid)
    payments = (await db.scalars(select(Payment).join(Invoice, Invoice.id == Payment.invoice_id).where(
        Payment.organization_id == organization.id, Payment.facility_id == facility.id,
        Invoice.practitioner_id == practitioner_uuid, Payment.currency == currency.upper(),
        Payment.status == "captured", Payment.provenance == "recorded")
        .order_by(Payment.received_at, Payment.id))).all()
    created = 0
    excluded: dict[str, int] = {}
    for payment in payments:
        before = await db.scalar(select(EarningEntry.id).where(EarningEntry.source_type == "payment",
                                                               EarningEntry.source_id == payment.id))
        reason = await record_earning_event(db, payment, "payment", payment.id,
                                            payment.amount_minor, payment.received_at)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        elif not before:
            created += 1
        refunds = (await db.scalars(select(Refund).where(Refund.payment_id == payment.id,
            Refund.status == "completed").order_by(Refund.refunded_at, Refund.id))).all()
        for refund in refunds:
            before = await db.scalar(select(EarningEntry.id).where(EarningEntry.source_type == "refund",
                                                                   EarningEntry.source_id == refund.id))
            reason = await record_earning_event(db, payment, "refund", refund.id,
                                                refund.amount_minor, refund.refunded_at)
            if reason:
                excluded[reason] = excluded.get(reason, 0) + 1
            elif not before:
                created += 1
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id,
        action="earnings.catch_up", resource_type="practitioner", resource_id=practitioner_uuid,
        facility_id=facility.id, details={"created": created, "excluded": excluded})
    await db.commit()
    return {"success": True, "data": {"created": created, "excluded": excluded}, "meta": {}}


@router.post("/billing/practitioner-payouts")
async def record_payout(facility_uuid: UUID, payload: PayoutInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)]):
    organization, facility, _ = await _admin(db, account, facility_uuid)
    await _practitioner(db, organization.id, payload.practitioner_uuid)
    await db.scalar(select(Practitioner.id).where(Practitioner.id == payload.practitioner_uuid).with_for_update())
    existing = await db.scalar(select(PayoutEntry).where(PayoutEntry.organization_id == organization.id,
                                                         PayoutEntry.idempotency_key == payload.idempotency_key))
    if existing:
        if (existing.facility_id, existing.practitioner_id, existing.currency, existing.amount_minor,
            existing.paid_at, existing.method, existing.reference) != (
            facility.id, payload.practitioner_uuid, payload.currency, payload.amount_minor,
            payload.paid_at, payload.method, payload.reference):
            raise _error(409, "IDEMPOTENCY_KEY_REUSED", "Key belongs to a different payout.")
        return {"success": True, "data": {"uuid": str(existing.id)}, "meta": {"idempotent": True}}
    policy_currency = await db.scalar(select(CompensationPolicy.currency).where(
        CompensationPolicy.organization_id == organization.id, CompensationPolicy.facility_id == facility.id,
        CompensationPolicy.practitioner_id == payload.practitioner_uuid,
        CompensationPolicy.currency == payload.currency).limit(1))
    if policy_currency is None:
        another_currency = await db.scalar(select(CompensationPolicy.currency).where(
            CompensationPolicy.organization_id == organization.id, CompensationPolicy.facility_id == facility.id,
            CompensationPolicy.practitioner_id == payload.practitioner_uuid).limit(1))
        raise _error(409, "CURRENCY_MISMATCH" if another_currency else "COMPENSATION_NOT_CONFIGURED",
                     "No compensation policy is configured for this practitioner and currency.")
    if payload.paid_at > datetime.now(UTC):
        raise _error(422, "PAYOUT_IN_FUTURE", "paid_at cannot be in the future.")
    balance = await _balance(db, organization.id, facility.id, payload.practitioner_uuid, payload.currency,
                             datetime.now(UTC))
    balance_at_payment = await _balance(db, organization.id, facility.id, payload.practitioner_uuid,
                                        payload.currency, payload.paid_at)
    if balance_at_payment < payload.amount_minor:
        raise _error(409, "PAYOUT_EXCEEDS_BALANCE", "Payout exceeds balance at paid_at.")
    if balance < payload.amount_minor:
        raise _error(409, "PAYOUT_EXCEEDS_BALANCE", "Payout exceeds current eligible balance.")
    payout = PayoutEntry(organization_id=organization.id, facility_id=facility.id,
        practitioner_id=payload.practitioner_uuid, currency=payload.currency,
        amount_minor=payload.amount_minor, kind="payout", paid_at=payload.paid_at,
        method=payload.method, reference=payload.reference,
        idempotency_key=payload.idempotency_key, created_by_user_id=account.id)
    db.add(payout)
    await db.flush()
    # Allocate only positive earned entries. Negative refunds reduce the checked balance above.
    rows = (await db.scalars(select(EarningEntry).where(*_scope(EarningEntry, organization.id,
        facility.id, payload.practitioner_uuid, payload.currency), EarningEntry.amount_minor > 0,
        EarningEntry.occurred_at <= payload.paid_at)
        .order_by(EarningEntry.occurred_at, EarningEntry.id))).all()
    remaining = payload.amount_minor
    for entry in rows:
        allocations = (await db.execute(select(PayoutAllocation.amount_minor, PayoutEntry.id)
            .join(PayoutEntry, PayoutEntry.id == PayoutAllocation.payout_id)
            .where(PayoutAllocation.earning_id == entry.id, PayoutEntry.kind == "payout"))).all()
        reversed_ids = set((await db.scalars(select(PayoutEntry.reverses_id).where(
            PayoutEntry.reverses_id.in_([row[1] for row in allocations])))).all()) if allocations else set()
        used = sum(amount for amount, payout_id in allocations if payout_id not in reversed_ids)
        amount = min(remaining, entry.amount_minor - used)
        if amount > 0:
            db.add(PayoutAllocation(payout_id=payout.id, earning_id=entry.id, amount_minor=amount))
            remaining -= amount
        if remaining == 0:
            break
    if remaining:
        raise _error(409, "PAYOUT_ALLOCATION_UNAVAILABLE", "Eligible entries cannot cover payout.")
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id,
        action="payout.recorded", resource_type="payout", resource_id=payout.id,
        facility_id=facility.id, details={"amount_minor": payout.amount_minor,
                                          "currency": payout.currency, "reference": payout.reference})
    await db.commit()
    return {"success": True, "data": {"uuid": str(payout.id), "amount_minor": payout.amount_minor,
        "currency": payout.currency, "paid_at": payout.paid_at.isoformat(),
        "balance_minor": balance - payload.amount_minor}, "meta": {"idempotent": False}}


@router.post("/billing/practitioner-payouts/{payout_uuid}/reverse")
async def reverse_payout(payout_uuid: UUID, payload: ReversalInput,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)]):
    original = await db.scalar(select(PayoutEntry).where(PayoutEntry.id == payout_uuid))
    if original is None or original.kind != "payout":
        raise _error(404, "PAYOUT_NOT_FOUND", "Payout not found.")
    organization, facility, _ = await _admin(db, account, original.facility_id)
    await db.scalar(select(Practitioner.id).where(Practitioner.id == original.practitioner_id).with_for_update())
    existing = await db.scalar(select(PayoutEntry).where(PayoutEntry.reverses_id == original.id))
    if existing:
        if existing.idempotency_key != payload.idempotency_key:
            raise _error(409, "PAYOUT_ALREADY_REVERSED", "Payout already reversed.")
        return {"success": True, "data": {"uuid": str(existing.id)}, "meta": {"idempotent": True}}
    key_owner = await db.scalar(select(PayoutEntry.id).where(PayoutEntry.organization_id == organization.id,
                                                              PayoutEntry.idempotency_key == payload.idempotency_key))
    if key_owner:
        raise _error(409, "IDEMPOTENCY_KEY_REUSED", "Key belongs to another payout.")
    reversal = PayoutEntry(organization_id=original.organization_id, facility_id=original.facility_id,
        practitioner_id=original.practitioner_id, currency=original.currency,
        amount_minor=-original.amount_minor, kind="reversal", paid_at=datetime.now(UTC),
        method=original.method, reference=original.reference,
        idempotency_key=payload.idempotency_key, created_by_user_id=account.id,
        reverses_id=original.id)
    db.add(reversal)
    await db.flush()
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id,
        action="payout.reversed", resource_type="payout", resource_id=reversal.id,
        facility_id=facility.id, details={"reverses_uuid": str(original.id), "reason": payload.reason})
    await db.commit()
    return {"success": True, "data": {"uuid": str(reversal.id), "reverses_uuid": str(original.id),
        "amount_minor": reversal.amount_minor}, "meta": {"idempotent": False}}
