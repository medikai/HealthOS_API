"""Chat conversation/message endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ....api.dependencies import get_current_identity_account
from ....core.config import settings
from ....core.db.database import async_get_db
from ....models.identity import UserAccount
from ....schemas.communication import (
    ConversationCreate,
    MemberAddRequest,
    MessageCreate,
    ReadCursorRequest,
)
from ..dependencies import CommunicationStaffContext, get_communication_staff_context
from .service import build_chat_service

router = APIRouter(prefix="/chat", tags=["communication"])
CHAT = build_chat_service(settings)


@router.get("/conversations")
async def list_conversations(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await CHAT.list_conversations(db, context)
    return {"success": True, "data": data, "meta": {}}


@router.post("/conversations")
async def create_conversation(
    payload: ConversationCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await CHAT.create_conversation(db, context, payload.model_dump())
    return {"success": True, "data": data, "meta": {}}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await CHAT.get_conversation(db, context, conversation_id)
    return {"success": True, "data": data, "meta": {}}


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
    before: Annotated[int | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
) -> dict:
    data = await CHAT.list_messages(db, context, conversation_id, before=before, limit=limit)
    return {"success": True, "data": data, "meta": {}}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: UUID,
    payload: MessageCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await CHAT.send_message(
        db,
        context,
        conversation_id,
        body=payload.body,
        client_message_id=payload.client_message_id,
    )
    return {"success": True, "data": data, "meta": {}}


@router.put("/conversations/{conversation_id}/read")
async def mark_conversation_read(
    conversation_id: UUID,
    payload: ReadCursorRequest,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await CHAT.mark_read(db, context, conversation_id, payload.message_id)
    return {"success": True, "data": data, "meta": {}}


@router.post("/conversations/{conversation_id}/members")
async def add_member(
    conversation_id: UUID,
    payload: MemberAddRequest,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await CHAT.add_member(db, context, conversation_id, payload.staff_uuid)
    return {"success": True, "data": data, "meta": {}}


@router.delete("/conversations/{conversation_id}/members/{staff_uuid}")
async def remove_member(
    conversation_id: UUID,
    staff_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
) -> dict:
    data = await CHAT.remove_member(db, context, conversation_id, staff_uuid)
    return {"success": True, "data": data, "meta": {}}
