"""Explicit development seed for consultation fees, invoices, and payments."""

import argparse
import asyncio
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import select

from src.app.api.v1.billing import _resolve_fee
from src.app.core.db.database import local_session
from src.app.models.billing import ConsultationFee, Invoice, Payment
from src.app.models.care import Encounter, Practitioner
from src.app.models.identity import UserAccount
from src.app.models.organization import Facility


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--facility-uuid", type=UUID, required=True)
    parser.add_argument("--actor-user-uuid", type=UUID, required=True)
    parser.add_argument("--practitioner-uuid", type=UUID)
    parser.add_argument("--effective-from", type=date.fromisoformat, required=True)
    parser.add_argument("--currency", default="INR")
    parser.add_argument("--facility-minor", type=int, default=50_000)
    parser.add_argument("--specialty-minor", type=int, default=70_000)
    parser.add_argument("--practitioner-minor", type=int, default=90_000)
    parser.add_argument("--include-in-progress", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


async def _seed(args: argparse.Namespace) -> None:
    if min(args.facility_minor, args.specialty_minor, args.practitioner_minor) < 0:
        raise ValueError("fee amounts must be non-negative integer minor units")
    currency = args.currency.upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency must be a three-letter code")

    async with local_session() as db:
        facility = await db.get(Facility, args.facility_uuid)
        actor = await db.get(UserAccount, args.actor_user_uuid)
        if facility is None or not facility.is_active:
            raise ValueError("active facility not found")
        if actor is None or not actor.is_active:
            raise ValueError("active actor user not found")
        practitioner = None
        if args.practitioner_uuid:
            practitioner = await db.scalar(
                select(Practitioner).where(
                    Practitioner.id == args.practitioner_uuid,
                    Practitioner.organization_id == facility.organization_id,
                    Practitioner.is_active.is_(True),
                )
            )
            if practitioner is None:
                raise ValueError(
                    "active practitioner in facility organization not found"
                )

        planned = [("facility", None, None, args.facility_minor)]
        if practitioner and practitioner.specialty_id:
            planned.append(
                ("specialty", None, practitioner.specialty_id, args.specialty_minor)
            )
        if practitioner:
            planned.append(
                ("practitioner", practitioner.id, None, args.practitioner_minor)
            )
        created_fees = 0
        for scope_type, practitioner_id, specialty_id, amount_minor in planned:
            existing = await db.scalar(
                select(ConsultationFee).where(
                    ConsultationFee.facility_id == facility.id,
                    ConsultationFee.scope_type == scope_type,
                    ConsultationFee.practitioner_id == practitioner_id,
                    ConsultationFee.specialty_id == specialty_id,
                    ConsultationFee.is_active.is_(True),
                )
            )
            if existing:
                if args.effective_from < existing.effective_from and not await db.scalar(
                    select(ConsultationFee.id).where(
                        ConsultationFee.facility_id == facility.id,
                        ConsultationFee.scope_type == scope_type,
                        ConsultationFee.practitioner_id == practitioner_id,
                        ConsultationFee.specialty_id == specialty_id,
                        ConsultationFee.effective_from == args.effective_from,
                    )
                ):
                    db.add(ConsultationFee(
                        organization_id=facility.organization_id,
                        facility_id=facility.id,
                        scope_type=scope_type,
                        practitioner_id=practitioner_id,
                        specialty_id=specialty_id,
                        amount_minor=existing.amount_minor,
                        currency=existing.currency,
                        effective_from=args.effective_from,
                        effective_to=existing.effective_from - timedelta(days=1),
                        is_active=False,
                        created_by_user_id=actor.id,
                    ))
                    created_fees += 1
                continue
            db.add(
                ConsultationFee(
                    organization_id=facility.organization_id,
                    facility_id=facility.id,
                    scope_type=scope_type,
                    practitioner_id=practitioner_id,
                    specialty_id=specialty_id,
                    amount_minor=amount_minor,
                    currency=currency,
                    effective_from=args.effective_from,
                    created_by_user_id=actor.id,
                )
            )
            created_fees += 1
        await db.flush()

        encounters = (
            await db.scalars(
                select(Encounter)
                .outerjoin(Invoice, Invoice.encounter_id == Encounter.id)
                .where(
                    Encounter.organization_id == facility.organization_id,
                    Encounter.facility_id == facility.id,
                    Encounter.status.in_(
                        ("completed", "in_progress")
                        if args.include_in_progress else ("completed",)
                    ),
                    Invoice.id.is_(None),
                )
            )
        ).all()
        created_invoices = created_payments = 0
        for encounter in encounters:
            fee = await _resolve_fee(db, encounter)
            if fee is None:
                continue
            invoice = Invoice(
                organization_id=encounter.organization_id,
                facility_id=encounter.facility_id,
                encounter_id=encounter.id,
                patient_id=encounter.patient_id,
                practitioner_id=encounter.practitioner_id,
                consultation_fee_id=fee.id,
                amount_minor=fee.amount_minor,
                currency=fee.currency,
                created_by_user_id=actor.id,
                issued_at=encounter.completed_at or encounter.started_at,
            )
            db.add(invoice)
            await db.flush()
            created_invoices += 1
            if fee.amount_minor > 0:
                db.add(Payment(
                    organization_id=encounter.organization_id,
                    facility_id=encounter.facility_id,
                    invoice_id=invoice.id,
                    amount_minor=fee.amount_minor,
                    currency=fee.currency,
                    idempotency_key=f"demo-encounter-{encounter.id}",
                    provenance="synthetic_demo",
                    created_by_user_id=actor.id,
                    received_at=encounter.completed_at or encounter.started_at,
                ))
                invoice.status = "paid"
                created_payments += 1
        if args.apply:
            await db.commit()
        else:
            await db.rollback()
        print(f"{'Created' if args.apply else 'Would create'} {created_fees} fees, {created_invoices} invoices, {created_payments} payments.")


if __name__ == "__main__":
    asyncio.run(_seed(_arguments()))
