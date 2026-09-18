from time import perf_counter
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Date, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.request_metrics import current_request_metrics
from ...core.timezones import DEFAULT_TIMEZONE
from ...models.care import Appointment, Practitioner, QueueEntry
from ...models.identity import UserAccount
from ...models.organization import (
    Facility,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffMember,
)

router = APIRouter(tags=["bootstrap"])
ADMIN_ROLES = ("administrator", "organization_admin", "owner")
ROLE_SCOPES = {
    "organization_admin": {
        "appointment:write",
        "clinical:author",
        "patient:read",
        "patient:write",
        "rx:signer",
    },
    "administrator": {
        "appointment:write",
        "clinical:author",
        "patient:read",
        "patient:write",
        "rx:signer",
    },
    "owner": {
        "appointment:write",
        "clinical:author",
        "patient:read",
        "patient:write",
        "rx:signer",
    },
    "practitioner": {
        "appointment:write",
        "clinical:author",
        "patient:read",
        "patient:write",
        "rx:signer",
    },
    "doctor": {
        "appointment:write",
        "clinical:author",
        "patient:read",
        "patient:write",
        "rx:signer",
    },
    "clinical_practitioner": {
        "appointment:write",
        "clinical:author",
        "patient:read",
        "patient:write",
        "rx:signer",
    },
    "nurse": {"appointment:write", "clinical:author", "patient:read", "patient:write"},
    "receptionist": {"appointment:write", "patient:read", "patient:write"},
    "facility_operator": {"appointment:write", "patient:read", "patient:write"},
    "billing_staff": {"patient:read"},
}


async def _staff_context(
    db: AsyncSession, account: UserAccount
) -> tuple[StaffMember, Organization]:
    row = await db.execute(
        select(StaffMember, Organization)
        .join(Organization, Organization.id == StaffMember.organization_id)
        .where(
            StaffMember.user_account_id == account.id,
            StaffMember.is_active.is_(True),
            Organization.is_active.is_(True),
        )
    )
    result = row.first()
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active HealthOS organization access.",
        )
    return result


@router.get("/me")
async def me(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    started_at = perf_counter()
    try:
        rows = (
            await db.execute(
                select(
                    StaffMember, Organization, Practitioner, StaffAssignment.role_code
                )
                .join(Organization, Organization.id == StaffMember.organization_id)
                .outerjoin(
                    StaffAssignment,
                    and_(
                        StaffAssignment.staff_member_id == StaffMember.id,
                        StaffAssignment.is_active.is_(True),
                    ),
                )
                .outerjoin(
                    Practitioner,
                    and_(
                        Practitioner.user_account_id == StaffMember.user_account_id,
                        Practitioner.organization_id == StaffMember.organization_id,
                        Practitioner.is_active.is_(True),
                    ),
                )
                .where(
                    StaffMember.user_account_id == account.id,
                    StaffMember.is_active.is_(True),
                    Organization.is_active.is_(True),
                )
            )
        ).all()
        if not rows:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No active HealthOS organization access.",
            )
        staff, organization, practitioner, _ = rows[0]
        role_codes = sorted(
            {
                role_code
                for s, o, p, role_code in rows
                if o.id == organization.id and role_code
            }
        )
        scopes = sorted(
            {scope for role in role_codes for scope in ROLE_SCOPES.get(role, set())}
        )
        return {
            "success": True,
            "data": {
                "user": {
                    "uuid": str(account.id),
                    "display_name": account.display_name,
                    "email": account.email,
                },
                "organization": {
                    "uuid": str(organization.id),
                    "name": organization.name,
                    "plan_code": None,
                },
                "staff": {
                    "uuid": str(staff.id),
                    "designation": role_codes[0] if role_codes else "staff",
                    "status": "active",
                    "role_codes": role_codes,
                },
                "practitioner": (
                    {
                        "uuid": str(practitioner.id),
                        "name": practitioner.person_name,
                        "specialization": practitioner.specialty,
                    }
                    if practitioner
                    else None
                ),
                "scopes": scopes,
            },
            "meta": {},
        }
    finally:
        metrics = current_request_metrics()
        if metrics is not None:
            metrics.handler_ms = round((perf_counter() - started_at) * 1000, 2)


@router.get("/facilities")
async def facilities(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    started_at = perf_counter()
    try:
        active_org = (
            select(StaffMember.organization_id)
            .join(Organization, Organization.id == StaffMember.organization_id)
            .where(
                StaffMember.user_account_id == account.id,
                StaffMember.is_active.is_(True),
                Organization.is_active.is_(True),
            )
            .order_by(StaffMember.created_at)
            .limit(1)
            .scalar_subquery()
        )
        accessible = (
            select(
                Facility.id.label("facility_id"),
                Facility.organization_id.label("organization_id"),
                Facility.code,
                Facility.name,
                Facility.created_at,
            )
            .join(StaffMember, StaffMember.organization_id == Facility.organization_id)
            .join(Organization, Organization.id == Facility.organization_id)
            .join(StaffAssignment, StaffAssignment.staff_member_id == StaffMember.id)
            .where(
                Facility.organization_id == active_org,
                StaffMember.user_account_id == account.id,
                StaffMember.is_active.is_(True),
                Organization.is_active.is_(True),
                StaffAssignment.is_active.is_(True),
                Facility.is_active.is_(True),
                or_(
                    StaffAssignment.facility_id == Facility.id,
                    StaffAssignment.role_code.in_(ADMIN_ROLES),
                ),
            )
            .distinct()
            .cte("accessible_facilities")
        )
        timezone_name = func.coalesce(FacilitySchedule.timezone, DEFAULT_TIMEZONE)
        local_today = cast(func.timezone(timezone_name, func.now()), Date)
        appointment_today = cast(
            func.timezone(timezone_name, Appointment.scheduled_start), Date
        )
        appointment_counts = (
            select(
                Appointment.facility_id.label("facility_id"),
                func.count().label("booked"),
            )
            .join(accessible, accessible.c.facility_id == Appointment.facility_id)
            .outerjoin(
                FacilitySchedule,
                FacilitySchedule.facility_id == Appointment.facility_id,
            )
            .where(
                Appointment.organization_id == accessible.c.organization_id,
                appointment_today == local_today,
                Appointment.status.not_in(["cancelled", "no_show"]),
            )
            .group_by(Appointment.facility_id)
            .cte("appointment_counts")
        )
        queue_counts = (
            select(
                QueueEntry.facility_id.label("facility_id"), func.count().label("queue")
            )
            .join(accessible, accessible.c.facility_id == QueueEntry.facility_id)
            .outerjoin(
                FacilitySchedule, FacilitySchedule.facility_id == QueueEntry.facility_id
            )
            .where(
                QueueEntry.organization_id == accessible.c.organization_id,
                QueueEntry.queue_date == local_today,
                QueueEntry.status.in_(["waiting", "called", "in_consultation"]),
            )
            .group_by(QueueEntry.facility_id)
            .cte("queue_counts")
        )
        duty_counts = (
            select(
                accessible.c.facility_id,
                func.count(func.distinct(Practitioner.id)).label("duty"),
            )
            .join(
                StaffMember, StaffMember.organization_id == accessible.c.organization_id
            )
            .join(
                StaffAssignment,
                and_(
                    StaffAssignment.staff_member_id == StaffMember.id,
                    StaffAssignment.is_active.is_(True),
                    or_(
                        StaffAssignment.facility_id == accessible.c.facility_id,
                        StaffAssignment.role_code.in_(ADMIN_ROLES),
                    ),
                ),
            )
            .join(
                Practitioner,
                and_(
                    Practitioner.organization_id == accessible.c.organization_id,
                    Practitioner.user_account_id == StaffMember.user_account_id,
                    Practitioner.is_active.is_(True),
                ),
            )
            .where(StaffMember.is_active.is_(True))
            .group_by(accessible.c.facility_id)
            .cte("duty_counts")
        )
        rows = (
            await db.execute(
                select(
                    accessible,
                    func.coalesce(FacilitySchedule.timezone, DEFAULT_TIMEZONE).label(
                        "timezone"
                    ),
                    func.coalesce(appointment_counts.c.booked, 0).label("booked"),
                    func.coalesce(queue_counts.c.queue, 0).label("queue"),
                    func.coalesce(duty_counts.c.duty, 0).label("duty"),
                )
                .outerjoin(
                    FacilitySchedule,
                    FacilitySchedule.facility_id == accessible.c.facility_id,
                )
                .outerjoin(
                    appointment_counts,
                    appointment_counts.c.facility_id == accessible.c.facility_id,
                )
                .outerjoin(
                    queue_counts, queue_counts.c.facility_id == accessible.c.facility_id
                )
                .outerjoin(
                    duty_counts, duty_counts.c.facility_id == accessible.c.facility_id
                )
                .order_by(accessible.c.created_at)
            )
        ).all()
        items = []
        for row in rows:
            booked, queued, duty = int(row.booked), int(row.queue), int(row.duty)
            # ponytail: five visits per active assigned practitioner until a room/capacity model exists.
            nominal_capacity = max(1, duty * 5)
            capacity_pct = min(100, round(booked / nominal_capacity * 100))
            capacity_label = f"{capacity_pct}% Capacity"
            items.append(
                {
                    "uuid": str(row.facility_id),
                    "code": row.code,
                    "name": row.name,
                    "timezone": row.timezone,
                    "tenant_id": str(row.organization_id),
                    "status": "active",
                    "capacity_label": capacity_label,
                    "telemetry": {
                        "booked": booked,
                        "queue": queued,
                        "duty": duty,
                        "capacity": capacity_label,
                        "capacity_pct": capacity_pct,
                    },
                }
            )
        return {
            "success": True,
            "data": {"items": items},
            "meta": {"count": len(items)},
        }
    finally:
        metrics = current_request_metrics()
        if metrics is not None:
            metrics.handler_ms = round((perf_counter() - started_at) * 1000, 2)


@router.get("/platform/context")
async def platform_context(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: str | None = Query(default=None),
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    query = (
        select(Facility)
        .join(StaffAssignment, StaffAssignment.facility_id == Facility.id)
        .where(
            StaffAssignment.staff_member_id == staff.id,
            StaffAssignment.is_active.is_(True),
            Facility.is_active.is_(True),
        )
    )
    if facility_uuid:
        query = query.where(Facility.id == facility_uuid)
    facility = (await db.scalars(query)).first()
    if facility is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Facility is not accessible."
        )
    return {
        "success": True,
        "data": {
            "organization": {"uuid": str(organization.id)},
            "facility": {"uuid": str(facility.id), "name": facility.name},
            "features": {},
            "workflows": {"opd_visit": {"version": 1}},
        },
        "meta": {},
    }
