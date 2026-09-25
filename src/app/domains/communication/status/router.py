"""Work status endpoints (own status only)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ....api.dependencies import get_current_identity_account
from ....core.db.database import async_get_db
from ....models.identity import UserAccount
from ....schemas.communication import WorkStatusPatch
from ..dependencies import CommunicationStaffContext, get_communication_staff_context
from .service import work_status_service

router = APIRouter(prefix="/status", tags=["communication"])


@router.get("")
async def get_work_status(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
    facility_uuid: UUID | None = None,
) -> dict:
    data = await work_status_service.get_status(db, context, facility_uuid=facility_uuid)
    return {"success": True, "data": data, "meta": {}}


@router.patch("")
async def patch_work_status(
    payload: WorkStatusPatch,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await work_status_service.patch_status(db, context, payload.model_dump())
    return {"success": True, "data": data, "meta": {}}
