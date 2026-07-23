import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...domains.organization import access_service
from ...models.identity import UserAccount
from ...schemas.access import DepartmentCreate, FacilityCreate, OrganizationCreate, OrganizationFeatureCreate, StaffRoleAssign

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization = await access_service.create_organization(db, account, payload.name, payload.code)
    return {
        "success": True,
        "data": {
            "id": str(organization.id),
            "name": organization.name,
            "code": organization.code,
            "logto_organization_id": organization.logto_organization_id,
            "role": "organization_admin",
        },
        "meta": {},
    }


@router.post("/{organization_id}/members/roles", status_code=status.HTTP_201_CREATED)
async def assign_member_role(
    organization_id: uuid.UUID,
    payload: StaffRoleAssign,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    assignment = await access_service.assign_role(db, account, organization_id, payload.logto_user_id, payload.role_code)
    return {"success": True, "data": {"assignment_id": str(assignment.id), "role_code": assignment.role_code}, "meta": {}}


@router.post("/{organization_id}/features", status_code=status.HTTP_201_CREATED)
async def add_organization_feature(
    organization_id: uuid.UUID,
    payload: OrganizationFeatureCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    assignment = await access_service.add_feature(db, account, organization_id, payload.code, payload.name, payload.description)
    return {"success": True, "data": {"assignment_id": str(assignment.id), "organization_id": str(organization_id), "is_enabled": assignment.is_enabled}, "meta": {}}


@router.post("/{organization_id}/facilities", status_code=status.HTTP_201_CREATED)
async def create_facility(
    organization_id: uuid.UUID,
    payload: FacilityCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    facility = await access_service.create_facility(db, account, organization_id, payload.name, payload.code)
    return {"success": True, "data": {"id": str(facility.id), "organization_id": str(organization_id), "name": facility.name, "code": facility.code}, "meta": {}}


@router.post("/{organization_id}/departments", status_code=status.HTTP_201_CREATED)
async def create_department(
    organization_id: uuid.UUID,
    payload: DepartmentCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    department = await access_service.create_department(
        db, account, organization_id, payload.name, payload.code, payload.facility_id
    )
    return {"success": True, "data": {"id": str(department.id), "organization_id": str(organization_id), "facility_id": str(department.facility_id) if department.facility_id else None, "name": department.name, "code": department.code}, "meta": {}}
