from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import DEFAULT_TIMEZONE, local_datetime, timezone
from ...models.care import Appointment, QueueEntry
from ...models.identity import UserAccount
from ...models.organization import Facility, FacilitySchedule
from .scheduling import _facility_timezone, _scope, _scope_query

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/today")
async def today_dashboard(facility_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)], dashboard_date: date | None = Query(default=None, alias="date")) -> dict[str, Any]:
    _, facility = await _scope(db, account, facility_uuid)
    tz = await _facility_timezone(db, facility.id)
    dashboard_date = dashboard_date or datetime.now(tz).date()
    start, end = local_datetime(dashboard_date, time.min, tz), local_datetime(dashboard_date + timedelta(days=1), time.min, tz)
    appointments = await db.scalar(select(func.count(Appointment.id)).where(Appointment.facility_id == facility_uuid, Appointment.scheduled_start >= start, Appointment.scheduled_start < end))
    queue_counts = dict((status, count) for status, count in (await db.execute(select(QueueEntry.status, func.count(QueueEntry.id)).where(QueueEntry.facility_id == facility_uuid, QueueEntry.queue_date == dashboard_date).group_by(QueueEntry.status))).all())
    return {"success": True, "data": {"date": dashboard_date.isoformat(), "facilities": [str(facility_uuid)], "metrics": {"appointments": appointments or 0, "checked_in": queue_counts.get("waiting", 0) + queue_counts.get("called", 0) + queue_counts.get("in_consultation", 0), "waiting": queue_counts.get("waiting", 0), "in_consultation": queue_counts.get("in_consultation", 0), "completed": queue_counts.get("completed", 0)}}, "meta": {}}


@router.get("/stats")
async def facility_stats(facility_uuid: str, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    scope_row = (
        await db.execute(
            _scope_query(account.id, facility_uuid)
            .add_columns(FacilitySchedule.timezone)
            .outerjoin(FacilitySchedule, FacilitySchedule.facility_id == Facility.id)
        )
    ).first()
    if scope_row is None:
        organization, facility = await _scope(db, account, facility_uuid)
        timezone_name = None
    else:
        organization, _, timezone_name = scope_row
    selected_facility = UUID(facility_uuid) if facility_uuid.lower() != "all" else None
    tz = timezone(timezone_name or DEFAULT_TIMEZONE)
    today = datetime.now(tz).date()
    start, end = local_datetime(today, time.min, tz), local_datetime(today + timedelta(days=1), time.min, tz)
    appointment_filters = [Appointment.organization_id == organization.id, Appointment.scheduled_start >= start, Appointment.scheduled_start < end]
    queue_filters = [QueueEntry.organization_id == organization.id, QueueEntry.queue_date == today]
    if selected_facility:
        appointment_filters.append(Appointment.facility_id == selected_facility)
        queue_filters.append(QueueEntry.facility_id == selected_facility)

    appointment_metrics = (
        select(
            func.count(Appointment.id).label("scheduled_today"),
            func.count(Appointment.id).filter(Appointment.status.in_({"booked", "confirmed"})).label("confirmed_count"),
            func.count(Appointment.id).filter(~Appointment.status.in_({"confirmed", "completed", "cancelled", "no_show"})).label("pending_count"),
            func.count(Appointment.id).filter(Appointment.status == "completed").label("completed_today"),
            func.count(func.distinct(Appointment.practitioner_id)).label("active_doctors_count"),
        )
        .where(*appointment_filters)
        .subquery()
    )
    queue_metrics = (
        select(
            func.count(QueueEntry.id).filter(QueueEntry.status.in_({"waiting", "called"})).label("in_waiting_room"),
            func.count(QueueEntry.id).filter(QueueEntry.status == "in_consultation").label("in_consultation"),
            func.count(QueueEntry.id).filter(QueueEntry.appointment_id.is_(None)).label("walk_ins_today"),
            func.count(QueueEntry.id).filter(QueueEntry.appointment_id.is_(None), QueueEntry.status.in_({"waiting", "called"})).label("walk_ins_waiting"),
        )
        .where(*queue_filters)
        .subquery()
    )
    metrics = (
        await db.execute(
            select(appointment_metrics, queue_metrics).select_from(appointment_metrics.join(queue_metrics, true()))
        )
    ).one()
    scheduled, completed = metrics.scheduled_today, metrics.completed_today
    data = {"facility_uuid": facility_uuid, "scheduled_today": scheduled, "confirmed_count": metrics.confirmed_count, "pending_count": metrics.pending_count, "in_waiting_room": metrics.in_waiting_room, "avg_wait_minutes": 0, "in_consultation": metrics.in_consultation, "active_doctors_count": metrics.active_doctors_count, "completed_today": completed, "throughput_pct": round(completed / scheduled * 1000) / 10 if scheduled else 0, "walk_ins_today": metrics.walk_ins_today, "walk_ins_waiting": metrics.walk_ins_waiting, "mean_turnaround_mins": 0}
    return {"success": True, "data": data, "meta": {}}
