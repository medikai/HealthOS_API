from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.identity import UserAccount
from ...models.organization import Facility, Organization, StaffAssignment, StaffMember

router = APIRouter(tags=["bootstrap"])


async def _staff_context(db: AsyncSession, account: UserAccount) -> tuple[StaffMember, Organization]:
    row = await db.execute(
        select(StaffMember, Organization)
        .join(Organization, Organization.id == StaffMember.organization_id)
        .where(StaffMember.user_account_id == account.id, StaffMember.is_active.is_(True), Organization.is_active.is_(True))
    )
    result = row.first()
    if result is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No active HealthOS organization access.")
    return result


@router.get("/me")
async def me(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    assignments = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == staff.id, StaffAssignment.is_active.is_(True)))).all()
    return {"success": True, "data": {"user": {"uuid": str(account.id), "external_subject": account.logto_user_id, "display_name": account.display_name}, "organization": {"uuid": str(organization.id), "name": organization.name, "logto_organization_id": organization.logto_organization_id}, "staff": {"uuid": str(staff.id), "status": "active", "role_codes": sorted({a.role_code for a in assignments})}}, "meta": {}}


@router.get("/facilities")
async def facilities(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, _ = await _staff_context(db, account)
    rows = await db.execute(select(Facility).join(StaffAssignment, StaffAssignment.facility_id == Facility.id).where(StaffAssignment.staff_member_id == staff.id, StaffAssignment.is_active.is_(True), Facility.is_active.is_(True)))
    items = [{"uuid": str(f.id), "code": f.code, "name": f.name} for f in rows.scalars().unique().all()]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/platform/context")
async def platform_context(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: str | None = Query(default=None),
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    query = select(Facility).join(StaffAssignment, StaffAssignment.facility_id == Facility.id).where(StaffAssignment.staff_member_id == staff.id, StaffAssignment.is_active.is_(True), Facility.is_active.is_(True))
    if facility_uuid:
        query = query.where(Facility.id == facility_uuid)
    facility = (await db.scalars(query)).first()
    if facility is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Facility is not accessible.")
    return {"success": True, "data": {"organization": {"uuid": str(organization.id)}, "facility": {"uuid": str(facility.id), "name": facility.name}, "features": {}, "workflows": {"opd_visit": {"version": 1}}}, "meta": {}}
