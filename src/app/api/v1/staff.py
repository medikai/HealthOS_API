from typing import Annotated, Any
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.identity import UserAccount
from ...models.organization import Facility, Organization, StaffAssignment, StaffMember
from ...schemas.staff import StaffAssignmentsInput, StaffRolesInput, StaffCreate, StaffUpdate
from ...core.config import settings
from ...domains.auth.logto import logto_oidc_client
from .bootstrap import _staff_context
from ...domains.governance.audit import record_audit

router = APIRouter(prefix="/admin/staff", tags=["staff"])


async def _admin(db: AsyncSession, account: UserAccount) -> tuple[StaffMember, Organization]:
    staff, organization = await _staff_context(db, account)
    role = await db.scalar(select(StaffAssignment).where(StaffAssignment.staff_member_id == staff.id, StaffAssignment.role_code.in_(["organization_admin", "owner", "administrator"]), StaffAssignment.is_active.is_(True)))
    if role is None:
        raise HTTPException(status_code=403, detail="Staff administration access is required.")
    return staff, organization


def _item(member: StaffMember, account: UserAccount, assignments: list[StaffAssignment]) -> dict[str, Any]:
    return {"uuid": str(member.id), "user_uuid": str(account.id), "display_name": account.display_name, "email": account.email, "status": "active" if member.is_active else "inactive", "role_codes": sorted({a.role_code for a in assignments}), "facility_uuids": [str(a.facility_id) for a in assignments if a.facility_id]}


@router.get("")
async def list_staff(account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], facility_uuid: UUID | None = None, status: str = "active", q: str | None = None) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    query = select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.organization_id == organization.id)
    if status == "active":
        query = query.where(StaffMember.is_active.is_(True))
    if q:
        query = query.where(UserAccount.display_name.ilike(f"%{q}%"))
    rows = (await db.execute(query)).all()
    items = []
    for member, user in rows:
        assignments = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == member.id, StaffAssignment.is_active.is_(True), *( [StaffAssignment.facility_id == facility_uuid] if facility_uuid else [])))).all()
        if facility_uuid and not assignments:
            continue
        items.append(_item(member, user, assignments))
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.post("", status_code=201)
async def create_staff(payload: StaffCreate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    user = await db.get(UserAccount, payload.user_account_uuid)
    if user is None:
        raise HTTPException(status_code=404, detail="User account not found. Provision the user in Logto first.")
    existing = await db.scalar(select(StaffMember).where(StaffMember.organization_id == organization.id, StaffMember.user_account_id == user.id))
    if existing:
        raise HTTPException(status_code=409, detail="User is already staff in this organization.")
    member = StaffMember(organization_id=organization.id, user_account_id=user.id)
    db.add(member)
    await db.flush()
    for facility_id in payload.facility_uuids:
        db.add(StaffAssignment(staff_member_id=member.id, facility_id=facility_id, role_code=payload.role_code))
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.created", resource_type="staff_member", resource_id=member.id)
    await db.commit()
    return {"success": True, "data": {"uuid": str(member.id), "user_uuid": str(user.id)}, "meta": {}}


@router.get("/{staff_uuid}")
async def get_staff(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    assignments = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == staff_uuid, StaffAssignment.is_active.is_(True)))).all()
    return {"success": True, "data": _item(*row, assignments), "meta": {}}


@router.patch("/{staff_uuid}")
async def update_staff(staff_uuid: UUID, payload: StaffUpdate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    if payload.display_name is not None:
        row[1].display_name = payload.display_name
    if payload.email is not None:
        row[1].email = payload.email
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.updated", resource_type="staff_member", resource_id=staff_uuid)
    await db.commit()
    return {"success": True, "data": {"uuid": str(staff_uuid), "display_name": row[1].display_name, "email": row[1].email}, "meta": {}}


@router.post("/{staff_uuid}/deactivate")
async def deactivate_staff(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    member = await db.scalar(select(StaffMember).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))
    if member is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    member.is_active = False
    assignments = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == member.id))).all()
    for assignment in assignments:
        assignment.is_active = False
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.deactivated", resource_type="staff_member", resource_id=member.id)
    await db.commit()
    return {"success": True, "data": {"uuid": str(member.id), "status": "inactive"}, "meta": {}}


@router.get("/{staff_uuid}/assignments")
async def get_assignments(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    values = (await db.execute(select(StaffAssignment).join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id).where(StaffAssignment.staff_member_id == staff_uuid, StaffMember.organization_id == organization.id, StaffAssignment.is_active.is_(True)))).scalars().all()
    return {"success": True, "data": {"items": [{"uuid": str(a.id), "facility_uuid": str(a.facility_id) if a.facility_id else None, "role_code": a.role_code} for a in values]}, "meta": {}}


@router.put("/{staff_uuid}/assignments")
async def replace_assignments(staff_uuid: UUID, payload: StaffAssignmentsInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    member = await db.scalar(select(StaffMember).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))
    if member is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    facilities = (await db.scalars(select(Facility).where(Facility.organization_id == organization.id, Facility.id.in_(payload.assignments), Facility.is_active.is_(True)))).all()
    if len(facilities) != len(set(payload.assignments)):
        raise HTTPException(status_code=400, detail="One or more facilities are not in this organization.")
    existing = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == member.id))).all()
    for assignment in existing:
        assignment.is_active = False
    for facility_id in payload.assignments:
        db.add(StaffAssignment(staff_member_id=member.id, facility_id=facility_id, role_code="facility_operator", is_active=True))
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.assignments.replaced", resource_type="staff_member", resource_id=member.id)
    await db.commit()
    return {"success": True, "data": {"facility_uuids": [str(x) for x in payload.assignments]}, "meta": {}}


@router.get("/{staff_uuid}/roles")
async def get_roles(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    return {"success": True, "data": {"logto_user_id": row[1].logto_user_id, "role_keys": []}, "meta": {"source": "logto"}}


@router.put("/{staff_uuid}/roles")
async def replace_roles(staff_uuid: UUID, payload: StaffRolesInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    unknown = [key for key in payload.role_keys if key not in settings.LOGTO_ORGANIZATION_ROLE_IDS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported role keys: {', '.join(unknown)}")
    if not organization.logto_organization_id:
        raise HTTPException(status_code=409, detail="Organization is not linked to Logto.")
    for key in payload.role_keys:
        await logto_oidc_client.assign_management_organization_role(organization.logto_organization_id, row[1].logto_user_id, settings.LOGTO_ORGANIZATION_ROLE_IDS[key])
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.roles.replaced", resource_type="staff_member", resource_id=staff_uuid)
    await db.commit()
    return {"success": True, "data": {"role_keys": payload.role_keys}, "meta": {"source": "logto"}}
