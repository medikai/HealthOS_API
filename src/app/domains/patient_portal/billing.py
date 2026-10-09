"""Patient-facing billing projections, staff discounts and payment receipts.

Amounts are integer minor units. Discounts snapshot gross/discount/net with
half-up percentage rounding and never produce a negative net. Receipts are
derived from captured Payment rows, never from a mocked paid flag.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.simple_pdf import render_text_pdf
from ...models.billing import Invoice, Payment, Receipt, Refund
from ...models.organization import Facility
from ..governance.audit import record_audit

DISCOUNT_KINDS = ("percentage", "fixed")


class BillingPortalError(RuntimeError):
    code = "BILLING_PORTAL_ERROR"


class InvoiceNotDiscountable(BillingPortalError):
    code = "INVOICE_NOT_DISCOUNTABLE"


class InvalidDiscount(BillingPortalError):
    code = "INVALID_DISCOUNT"


class ReceiptUnavailable(BillingPortalError):
    code = "RECEIPT_UNAVAILABLE"


def compute_discount(
    gross_minor: int,
    *,
    kind: str,
    bp: int | None = None,
    fixed_minor: int | None = None,
) -> tuple[int, int]:
    """Return ``(discount_minor, net_minor)`` with half-up rounding and net >= 0."""
    if gross_minor < 0:
        raise InvalidDiscount("Gross amount cannot be negative.")
    if kind == "percentage":
        if bp is None or not (1 <= bp <= 10000):
            raise InvalidDiscount("Percentage discount must be between 1 and 10000 basis points.")
        discount = (gross_minor * bp + 5000) // 10000
    elif kind == "fixed":
        if fixed_minor is None or fixed_minor <= 0:
            raise InvalidDiscount("Fixed discount must be a positive minor-unit amount.")
        discount = fixed_minor
    else:
        raise InvalidDiscount("Discount kind must be 'percentage' or 'fixed'.")
    discount = min(discount, gross_minor)
    return discount, gross_minor - discount


async def apply_discount(
    db: AsyncSession,
    *,
    invoice: Invoice,
    actor_user_id: UUID,
    kind: str,
    bp: int | None,
    fixed_minor: int | None,
    reason: str | None,
) -> Invoice:
    if invoice.status == "voided":
        raise InvoiceNotDiscountable("A voided invoice cannot be discounted.")
    paid = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount_minor), 0)).where(
            Payment.invoice_id == invoice.id,
            Payment.status == "captured",
        )
    ) or 0
    refunded = await db.scalar(
        select(func.coalesce(func.sum(Refund.amount_minor), 0)).where(
            Refund.invoice_id == invoice.id,
            Refund.status == "completed",
        )
    ) or 0
    if int(paid) - int(refunded) > 0:
        raise InvoiceNotDiscountable(
            "Discounts cannot be applied after payments have been recorded."
        )
    gross = invoice.gross_amount_minor if invoice.gross_amount_minor is not None else invoice.amount_minor
    discount, net = compute_discount(
        gross, kind=kind, bp=bp, fixed_minor=fixed_minor
    )
    invoice.gross_amount_minor = gross
    invoice.discount_minor = discount
    invoice.discount_kind = kind
    invoice.discount_bp = bp if kind == "percentage" else None
    invoice.discount_fixed_minor = fixed_minor if kind == "fixed" else None
    invoice.discount_reason = reason
    invoice.discounted_by_user_id = actor_user_id
    invoice.discounted_at = datetime.now(UTC)
    invoice.amount_minor = net
    await record_audit(
        db,
        organization_id=invoice.organization_id,
        facility_id=invoice.facility_id,
        actor_user_id=actor_user_id,
        action="invoice.discount_applied",
        resource_type="invoice",
        resource_id=invoice.id,
        patient_id=invoice.patient_id,
        details={
            "gross_minor": gross,
            "discount_minor": discount,
            "net_minor": net,
            "kind": kind,
        },
    )
    await db.commit()
    await db.refresh(invoice)
    return invoice


async def get_or_create_receipt(
    db: AsyncSession,
    *,
    invoice: Invoice,
    payment: Payment,
) -> Receipt:
    from sqlalchemy import inspect as sa_inspect

    # Re-load by identity: callers may hold instances whose column attributes
    # were unloaded by earlier commits in the same session.
    if sa_inspect(invoice).identity:
        await db.refresh(invoice)
    if sa_inspect(payment).identity:
        await db.refresh(payment)
    if sa_inspect(invoice).identity:
        await db.refresh(invoice)
    if payment.invoice_id != invoice.id:
        raise ReceiptUnavailable("The payment does not belong to this invoice.")
    if payment.status != "captured":
        raise ReceiptUnavailable("A receipt is available only for captured payments.")
    existing = await db.scalar(
        select(Receipt).where(Receipt.payment_id == payment.id)
    )
    if existing is not None:
        return existing
    facility = await db.get(Facility, invoice.facility_id)
    facility_code = (facility.code if facility else "CLINIC").upper()[:10]
    day = (payment.received_at or datetime.now(UTC)).strftime("%Y%m%d")
    for _ in range(5):
        count = await db.scalar(
            select(func.count(Receipt.id)).where(
                Receipt.facility_id == invoice.facility_id,
                func.date(Receipt.issued_at) == func.date(payment.received_at),
            )
        ) or 0
        receipt = Receipt(
            organization_id=invoice.organization_id,
            facility_id=invoice.facility_id,
            invoice_id=invoice.id,
            payment_id=payment.id,
            receipt_number=f"RCPT-{facility_code}-{str(invoice.facility_id).replace('-', '')[:6].upper()}-{day}-{int(count) + 1:04d}",
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            issued_by_user_id=None,
        )
        db.add(receipt)
        try:
            await db.commit()
            await db.refresh(receipt)
            return receipt
        except IntegrityError:
            await db.rollback()
            continue
    raise ReceiptUnavailable("A receipt could not be issued; retry shortly.")


def render_receipt_pdf(
    *,
    receipt: Receipt,
    invoice: Invoice,
    payment: Payment,
    facility_name: str | None,
    patient: dict[str, Any],
    paid_to_date_minor: int,
    refunded_minor: int,
    balance_minor: int,
) -> bytes:
    currency = receipt.currency
    sections: list[tuple[str, list[str]]] = [
        ("Receipt", [f"Receipt number: {receipt.receipt_number}"]),
        (
            "Patient",
            [f"Name: {patient.get('display_name') or 'Patient'}", f"MRN: {patient.get('mrn') or '-'}"],
        ),
        (
            "Payment",
            [
                f"Amount received: {payment.amount_minor} {currency} (minor units)",
                f"Received at: {payment.received_at.isoformat() if payment.received_at else '-'}",
                f"Payment status: {payment.status}",
                f"Payment reference: {payment.id}",
            ],
        ),
        (
            "Invoice",
            [
                f"Invoice reference: {invoice.id}",
                f"Invoice amount (net): {invoice.amount_minor} {currency}",
                f"Facility: {facility_name or '-'}",
            ],
        ),
        (
            "Balance",
            [
                f"Paid to date: {paid_to_date_minor} {currency}",
                f"Refunded: {refunded_minor} {currency}",
                f"Remaining balance: {balance_minor} {currency}",
            ],
        ),
    ]
    return render_text_pdf(
        title="Payment receipt",
        subtitle="MedikAI patient portal copy",
        sections=sections,
        footer_note="System-generated receipt for a recorded payment; not a tax invoice.",
    )
