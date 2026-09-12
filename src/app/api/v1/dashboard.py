from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.care import Appointment, QueueEntry
from ...models.identity import UserAccount
from .scheduling import _scope

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/today")
async def today_dashboard(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], dashboard_date: date = Query(default_factory=date.today, alias="date")) -> dict[str, Any]:
    await _scope(db, account, facility_uuid)
    start, end = datetime.combine(dashboard_date, time.min), datetime.combine(dashboard_date + timedelta(days=1), time.min)
    appointments = await db.scalar(select(func.count(Appointment.id)).where(Appointment.facility_id == facility_uuid, Appointment.scheduled_start >= start, Appointment.scheduled_start < end))
    queue_counts = dict((status, count) for status, count in (await db.execute(select(QueueEntry.status, func.count(QueueEntry.id)).where(QueueEntry.facility_id == facility_uuid, QueueEntry.queue_date == dashboard_date).group_by(QueueEntry.status))).all())
    return {"success": True, "data": {"date": dashboard_date.isoformat(), "facilities": [str(facility_uuid)], "metrics": {"appointments": appointments or 0, "checked_in": queue_counts.get("waiting", 0) + queue_counts.get("called", 0) + queue_counts.get("in_consultation", 0), "waiting": queue_counts.get("waiting", 0), "in_consultation": queue_counts.get("in_consultation", 0), "completed": queue_counts.get("completed", 0)}}, "meta": {}}


@router.get("/masters/visit-reasons")
async def visit_reasons() -> dict[str, Any]:
    return {"success": True, "data": {"items": [{"code": "examination", "name": "Examination"}, {"code": "follow_up", "name": "Follow-up"}, {"code": "prescription_renewal", "name": "Prescription renewal"}, {"code": "custom", "name": "Custom"}]}, "meta": {}}


@router.get("/masters/specialties")
async def specialties() -> dict[str, Any]:
    return {"success": True, "data": {"items": [{"code": "general_medicine", "name": "General Medicine"}, {"code": "orthopaedics", "name": "Orthopaedics"}, {"code": "paediatrics", "name": "Paediatrics"}]}, "meta": {}}


@router.get("/masters/staff-designations")
async def staff_designations() -> dict[str, Any]:
    return {"success": True, "data": {"items": [{"code": "entry_operator", "name": "Entry operator"}, {"code": "assistant", "name": "Assistant"}, {"code": "practitioner", "name": "Practitioner"}, {"code": "administrator", "name": "Administrator"}]}, "meta": {}}


@router.get("/masters/access-roles")
async def access_roles() -> dict[str, Any]:
    return {"success": True, "data": {"items": [{"key": "organization_admin"}, {"key": "facility_operator"}, {"key": "clinical_practitioner"}]}, "meta": {}}
