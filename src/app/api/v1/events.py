from typing import Annotated, Any
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import AuditLog, QueueEntry
from ...models.identity import UserAccount
from .scheduling import _scope

router = APIRouter(tags=["events"])

@router.get("/admin/audit-logs")
async def audit_logs(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], action: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    organization, _ = await _scope(db, account, facility_uuid)
    query = select(AuditLog).where(AuditLog.organization_id == organization.id, AuditLog.facility_id == facility_uuid)
    if action:
        query = query.where(AuditLog.action == action)
    values = (await db.scalars(query.order_by(AuditLog.occurred_at.desc()).limit(limit))).all()
    return {"success": True, "data": {"items": [{"uuid": str(v.id), "action": v.action, "resource_type": v.resource_type, "resource_id": v.resource_id, "patient_id": v.patient_id, "actor_user_id": str(v.actor_user_id) if v.actor_user_id else None, "occurred_at": v.occurred_at.isoformat()} for v in values]}, "meta": {"count": len(values)}}
