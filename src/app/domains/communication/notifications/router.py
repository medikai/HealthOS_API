"""Notification inbox, read state, preferences and catch-up endpoints."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ....api.dependencies import get_current_identity_account
from ....core.db.database import async_get_db
from ....models.identity import UserAccount
from ....schemas.communication import (
    NotificationPreferencesPatch,
    ReadAllRequest,
)
from ..dependencies import CommunicationStaffContext, get_communication_staff_context
from .service import DEFAULT_LIMIT, MAX_LIMIT, NotificationService

router = APIRouter(tags=["communication"])
SERVICE = NotificationService()


@router.get("/notifications")
async def list_notifications(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
    tab: Annotated[str, Query()] = "all",
    priority: Annotated[str | None, Query()] = None,
    facility_uuid: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict:
    data = await SERVICE.list_notifications(
        db,
        context,
        tab=tab,
        priority=priority,
        facility_uuid=facility_uuid,
        query=q,
        cursor=cursor,
        limit=limit,
    )
    return {"success": True, "data": data, "meta": {}}


@router.get("/notifications/counts")
async def notification_counts(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
    facility_uuid: Annotated[str | None, Query()] = None,
) -> dict:
    data = await SERVICE.counts(db, context, facility_uuid=facility_uuid)
    return {"success": True, "data": data, "meta": {}}


@router.get("/notifications/changes")
async def notification_changes(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
    since: Annotated[datetime, Query()],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict:
    data = await SERVICE.changes(db, context, since=since, limit=limit)
    return {"success": True, "data": data, "meta": {}}


@router.post("/notifications/read-all")
async def read_all_notifications(
    payload: ReadAllRequest | None,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    body = payload or ReadAllRequest()
    scope = body.scope.model_dump(by_alias=True) if body.scope else None
    data = await SERVICE.mark_all_read(db, context, cutoff=body.cutoff, scope=scope)
    return {"success": True, "data": data, "meta": {}}


@router.get("/notifications/{notification_id}")
async def get_notification(
    notification_id: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await SERVICE.get_notification(db, context, notification_id)
    return {"success": True, "data": data, "meta": {}}


@router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await SERVICE.mark_read(db, context, notification_id)
    return {"success": True, "data": data, "meta": {}}


@router.get("/notification-preferences")
async def get_notification_preferences(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await SERVICE.get_preferences(db, context)
    return {"success": True, "data": data, "meta": {}}


@router.patch("/notification-preferences")
async def patch_notification_preferences(
    payload: NotificationPreferencesPatch,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await SERVICE.patch_preferences(
        db, context, payload.model_dump(by_alias=True, exclude_none=True)
    )
    return {"success": True, "data": data, "meta": {}}
