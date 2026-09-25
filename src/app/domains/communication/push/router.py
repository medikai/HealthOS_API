"""Push device and feature-status endpoints (own device only)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ....api.dependencies import get_current_identity_account
from ....core.config import settings
from ....core.db.database import async_get_db
from ....models.identity import UserAccount
from ....schemas.communication import PushDeviceRegister
from ..dependencies import CommunicationStaffContext, get_communication_staff_context
from .service import build_push_service

router = APIRouter(prefix="/push", tags=["communication"])
PUSH = build_push_service(settings)


@router.get("/config")
async def push_config(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
) -> dict:
    """Feature availability only; public Firebase/VAPID config stays frontend-side."""
    return {"success": True, "data": PUSH.config_status(), "meta": {}}


@router.get("/devices")
async def list_push_devices(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await PUSH.list_devices(db, context)
    return {"success": True, "data": {"items": data}, "meta": {}}


@router.post("/devices")
async def register_push_device(
    payload: PushDeviceRegister,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await PUSH.register_device(db, context, payload.model_dump())
    return {"success": True, "data": data, "meta": {}}


@router.delete("/devices")
async def revoke_all_push_devices(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    """Revoke every active device for the caller (bearer or cookie auth)."""
    data = await PUSH.revoke_all_devices(db, context)
    return {"success": True, "data": data, "meta": {}}


@router.delete("/devices/{device_id}")
async def revoke_push_device(
    device_id: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await PUSH.revoke_device(db, context, device_id)
    return {"success": True, "data": data, "meta": {}}
