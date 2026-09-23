"""Preview or create invoices for completed visits with configured historical fees."""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select

from src.app.api.v1.billing import _resolve_fee
from src.app.core.db.database import local_session
from src.app.models.billing import Invoice
from src.app.models.care import Encounter
from src.app.models.identity import UserAccount
from src.app.models.organization import Facility, StaffMember


async def backfill(facility_uuid: UUID, actor_uuid: UUID, apply: bool) -> tuple[int, int]:
    async with local_session() as db:
        facility = await db.get(Facility, facility_uuid)
        actor = await db.get(UserAccount, actor_uuid)
        if facility is None or not facility.is_active or actor is None or not actor.is_active:
            raise ValueError("active facility and actor are required")
        staff = await db.scalar(select(StaffMember.id).where(
            StaffMember.user_account_id == actor.id,
            StaffMember.organization_id == facility.organization_id,
            StaffMember.is_active.is_(True),
        ))
        if staff is None:
            raise ValueError("actor must be active staff in the facility organization")
        encounters = (await db.scalars(
            select(Encounter)
            .outerjoin(Invoice, Invoice.encounter_id == Encounter.id)
            .where(Encounter.organization_id == facility.organization_id,
                   Encounter.facility_id == facility.id,
                   Encounter.status == "completed", Invoice.id.is_(None))
            .with_for_update(of=Encounter)
        )).all()
        eligible = missing_fee = 0
        for encounter in encounters:
            fee = await _resolve_fee(db, encounter)
            if fee is None:
                missing_fee += 1
                continue
            eligible += 1
            if apply:
                db.add(Invoice(
                    organization_id=encounter.organization_id,
                    facility_id=encounter.facility_id,
                    encounter_id=encounter.id,
                    patient_id=encounter.patient_id,
                    practitioner_id=encounter.practitioner_id,
                    consultation_fee_id=fee.id,
                    amount_minor=fee.amount_minor,
                    currency=fee.currency,
                    created_by_user_id=actor.id,
                ))
        if apply:
            await db.commit()
        return eligible, missing_fee


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--facility-uuid", required=True, type=UUID)
    parser.add_argument("--actor-user-uuid", required=True, type=UUID)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    eligible, missing_fee = asyncio.run(backfill(args.facility_uuid, args.actor_user_uuid, args.apply))
    print(f"{'Created' if args.apply else 'Eligible'} invoices: {eligible}; missing historical fee: {missing_fee}")
