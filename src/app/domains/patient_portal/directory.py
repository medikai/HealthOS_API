"""Patient-safe clinic directory and availability projections.

Reuses the staff availability engine but strips internal metadata (resource
identity, conflict reasons, staff-only fields). Only portal-enabled clinics and
their active facilities/practitioners are reachable.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.availability import evaluate_availability_range
from ...models.care import Practitioner, PractitionerAvailabilityRule
from ...models.masters import Specialty, SubSpecialty


class DirectoryError(RuntimeError):
    code = "DIRECTORY_ERROR"


class PractitionerNotFound(DirectoryError):
    code = "NOT_FOUND"


def practitioner_payload(
    practitioner: Practitioner,
    *,
    specialty_name: str | None = None,
    sub_specialty_name: str | None = None,
) -> dict[str, Any]:
    return {
        "uuid": str(practitioner.id),
        "name": practitioner.person_name,
        "specialty": specialty_name or practitioner.specialty,
        "sub_specialty": sub_specialty_name,
    }


async def list_specialties(
    db: AsyncSession, *, organization_id: UUID
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(Specialty.id, Specialty.name, Specialty.code)
            .join(Practitioner, Practitioner.specialty_id == Specialty.id)
            .where(
                Practitioner.organization_id == organization_id,
                Practitioner.is_active.is_(True),
                Specialty.is_active.is_(True),
            )
            .distinct()
            .order_by(Specialty.name)
        )
    ).all()
    return [{"uuid": str(row.id), "name": row.name, "code": row.code} for row in rows]


async def list_practitioners(
    db: AsyncSession,
    *,
    organization_id: UUID,
    specialty_id: UUID | None = None,
    facility_id: UUID | None = None,
) -> list[dict[str, Any]]:
    query = (
        select(Practitioner, Specialty.name, SubSpecialty.name)
        .outerjoin(Specialty, Specialty.id == Practitioner.specialty_id)
        .outerjoin(SubSpecialty, SubSpecialty.id == Practitioner.sub_specialty_id)
        .where(
            Practitioner.organization_id == organization_id,
            Practitioner.is_active.is_(True),
        )
    )
    if specialty_id is not None:
        query = query.where(Practitioner.specialty_id == specialty_id)
    if facility_id is not None:
        query = query.where(
            Practitioner.id.in_(
                select(PractitionerAvailabilityRule.practitioner_id).where(
                    PractitionerAvailabilityRule.facility_id == facility_id,
                    PractitionerAvailabilityRule.status == "active",
                )
            )
        )
    rows = (await db.execute(query.order_by(Practitioner.person_name))).all()
    return [
        practitioner_payload(p, specialty_name=specialty, sub_specialty_name=sub)
        for p, specialty, sub in rows
    ]


async def get_practitioner(
    db: AsyncSession, *, organization_id: UUID, practitioner_id: UUID
) -> dict[str, Any] | None:
    row = (
        await db.execute(
            select(Practitioner, Specialty.name, SubSpecialty.name)
            .outerjoin(Specialty, Specialty.id == Practitioner.specialty_id)
            .outerjoin(SubSpecialty, SubSpecialty.id == Practitioner.sub_specialty_id)
            .where(
                Practitioner.id == practitioner_id,
                Practitioner.organization_id == organization_id,
                Practitioner.is_active.is_(True),
            )
        )
    ).first()
    if row is None:
        return None
    practitioner, specialty, sub = row
    return practitioner_payload(
        practitioner, specialty_name=specialty, sub_specialty_name=sub
    )


def _sanitize_day(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": result.get("date"),
        "timezone": result.get("timezone"),
        "state": result.get("state"),
        "slots": [
            {
                "start": slot["start"],
                "end": slot["end"],
                "available": bool(slot.get("bookable")),
            }
            for slot in result.get("slots", [])
        ],
    }


async def availability(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID,
    from_date: date,
    to_date: date,
) -> list[dict[str, Any]]:
    if to_date < from_date:
        raise DirectoryError("to_date must not precede from_date.")
    if (to_date - from_date).days > 30:
        raise DirectoryError("Availability window is limited to 31 days.")
    results = await evaluate_availability_range(
        db,
        organization_id=organization_id,
        facility_id=facility_id,
        practitioner_id=practitioner_id,
        from_day=from_date,
        to_day=to_date,
        enforce_future=True,
    )
    return [_sanitize_day(result) for result in results]
