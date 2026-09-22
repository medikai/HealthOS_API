from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import DEFAULT_TIMEZONE, timezone, to_timezone
from ...domains.governance.audit import record_audit
from ...models.billing import ConsultationFee, Invoice, Payment, Refund
from ...models.care import Encounter, Practitioner
from ...models.identity import UserAccount
from ...models.masters import Specialty
from ...models.organization import (
    Facility,
    FacilitySchedule,
    StaffAssignment,
    StaffMember,
)
from ...schemas.billing import ConsultationFeeInput, PaymentInput, RefundInput
from .bootstrap import ADMIN_ROLES

router = APIRouter(tags=["billing"])
BILLING_ROLES = {*ADMIN_ROLES, "billing_staff"}


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
    )
    db.add(payment)
    await db.flush()
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
