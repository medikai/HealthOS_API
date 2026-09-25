"""Request-scoped authorization and provider wiring for communication routes.

Reuses the existing auth dependency (``get_current_identity_account``) and
existing organization/facility models. No new identity or session store.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.config import settings
from ...core.db.database import async_get_db
from ...models.identity import UserAccount
from ...models.organization import Facility, Organization, StaffAssignment, StaffMember

if TYPE_CHECKING:
    from .realtime.service import RealtimeService

# Mirror bootstrap.py's role scopes for facility visibility. Importing the
# constant directly would couple domains; keep the same literal for now.
ADMIN_ROLES = ("administrator", "organization_admin", "owner")


@dataclass(frozen=True)
class CommunicationStaffContext:
    """Trusted principal + tenant/facility scope derived server-side."""

    account_id: UUID
    organization_id: UUID
    staff_member_id: UUID
    facility_ids: tuple[UUID, ...]
    role_codes: tuple[str, ...]


async def resolve_staff_context(
    db: AsyncSession, account: UserAccount
) -> CommunicationStaffContext:
    """Resolve the active staff membership deterministically.

    A user may belong to several organizations; pick the oldest active
    membership (matching ``/bootstrap/facilities``) so token issuance and
    channel derivation are stable. No client-supplied tenant is trusted.
    """
    row = (
        await db.execute(
            select(StaffMember, Organization)
            .join(Organization, Organization.id == StaffMember.organization_id)
            .where(
                StaffMember.user_account_id == account.id,
                StaffMember.is_active.is_(True),
                Organization.is_active.is_(True),
            )
            .order_by(StaffMember.created_at)
            .limit(1)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active HealthOS organization access.",
        )
    staff, organization = row

    facility_rows = (
        await db.execute(
            select(Facility.id)
            .join(StaffMember, StaffMember.organization_id == Facility.organization_id)
            .join(StaffAssignment, StaffAssignment.staff_member_id == StaffMember.id)
            .where(
                Facility.organization_id == organization.id,
                Facility.is_active.is_(True),
                StaffMember.id == staff.id,
                StaffAssignment.is_active.is_(True),
                or_(
                    StaffAssignment.facility_id == Facility.id,
                    StaffAssignment.role_code.in_(ADMIN_ROLES),
                ),
            )
            .distinct()
        )
    ).all()
    role_rows = (
        await db.execute(
            select(StaffAssignment.role_code)
            .where(
                StaffAssignment.staff_member_id == staff.id,
                StaffAssignment.is_active.is_(True),
            )
            .distinct()
        )
    ).all()

    return CommunicationStaffContext(
        account_id=account.id,
        organization_id=organization.id,
        staff_member_id=staff.id,
        facility_ids=tuple(row[0] for row in facility_rows),
        role_codes=tuple(sorted(row[0] for row in role_rows)),
    )


async def get_communication_staff_context(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CommunicationStaffContext:
    return await resolve_staff_context(db, account)


def get_realtime_service() -> "RealtimeService":
    """Build the realtime service from backend-only settings.

    Imported lazily to avoid import cycles with the shared ports module.
    """
    from .realtime.providers.ably import build_ably_token_provider
    from .realtime.repository import RealtimeChannelRepository
    from .realtime.service import RealtimeService

    return RealtimeService(
        provider=build_ably_token_provider(settings),
        repository=RealtimeChannelRepository(),
        namespace=settings.ABLY_CHANNEL_NAMESPACE,
        ttl_seconds=settings.ABLY_TOKEN_TTL_SECONDS,
        renew_after_seconds=settings.ABLY_TOKEN_RENEW_AFTER_SECONDS,
        presence_enabled=settings.ABLY_PRESENCE_ENABLED,
        max_presence_channels=settings.COMMUNICATION_MAX_PRESENCE_CHANNELS,
    )
