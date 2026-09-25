"""Recipient/actor resolution for workflow notifications (DB helpers)."""

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ....models.care import Practitioner
from ....models.identity import UserAccount
from ....models.organization import StaffAssignment, StaffMember

ADMIN_ROLES = ("administrator", "organization_admin", "owner")


async def acting_actor(
    db: AsyncSession, actor_user_id: UUID | None
) -> tuple[str | None, str | None]:
    if actor_user_id is None:
        return None, None
    account = await db.get(UserAccount, actor_user_id)
    name = account.display_name if account else None
    role = await db.scalar(
        select(StaffAssignment.role_code)
        .join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id)
        .where(
            StaffMember.user_account_id == actor_user_id,
            StaffAssignment.is_active.is_(True),
        )
        .order_by(StaffAssignment.role_code)
        .limit(1)
    )
    return name, role


async def practitioner_staff_ids(
    db: AsyncSession, *, organization_id: UUID, practitioner_id: UUID | None
) -> list[UUID]:
    if practitioner_id is None:
        return []
    user_account_id = await db.scalar(
        select(Practitioner.user_account_id).where(Practitioner.id == practitioner_id)
    )
    if user_account_id is None:
        return []
    rows = await db.scalars(
        select(StaffMember.id).where(
            StaffMember.organization_id == organization_id,
            StaffMember.user_account_id == user_account_id,
            StaffMember.is_active.is_(True),
        )
    )
    return list(rows)


async def facility_role_staff_ids(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID | None,
    roles: tuple[str, ...],
) -> list[UUID]:
    if facility_id is None:
        return []
    rows = await db.scalars(
        select(StaffMember.id)
        .join(StaffAssignment, StaffAssignment.staff_member_id == StaffMember.id)
        .where(
            StaffMember.organization_id == organization_id,
            StaffMember.is_active.is_(True),
            StaffAssignment.is_active.is_(True),
            StaffAssignment.role_code.in_(roles),
            or_(
                StaffAssignment.facility_id == facility_id,
                StaffAssignment.role_code.in_(ADMIN_ROLES),
            ),
        )
        .distinct()
    )
    return list(rows)


async def exclude_actor(
    db: AsyncSession, staff_ids: list[UUID], actor_user_id: UUID | None
) -> list[UUID]:
    if actor_user_id is None or not staff_ids:
        return staff_ids
    actor_rows = await db.scalars(
        select(StaffMember.id).where(
            StaffMember.id.in_(staff_ids),
            StaffMember.user_account_id == actor_user_id,
        )
    )
    actor_ids = set(actor_rows)
    return [staff_id for staff_id in staff_ids if staff_id not in actor_ids]
