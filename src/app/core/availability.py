import json
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.care import (
    Appointment,
    PractitionerAvailabilityException,
    PractitionerAvailabilityRule,
)
from ..models.organization import FacilityResource, FacilitySchedule, ProtectedPeriod
from .timezones import DEFAULT_TIMEZONE, local_datetime, timezone, to_timezone

ACTIVE_APPOINTMENT_STATUSES = ("booked", "confirmed", "checked_in", "in_consultation")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and end > other_start


def _clock(value: str) -> time:
    return time.fromisoformat(value)


def _unavailable(exception: PractitionerAvailabilityException, start: datetime, end: datetime, tz) -> bool:
    if exception.exception_type.lower() in {"available", "extra_hours"}:
        return False
    if exception.start_time is None or exception.end_time is None:
        return True
    return _overlaps(
        start,
        end,
        local_datetime(exception.exception_date, exception.start_time, tz),
        local_datetime(exception.exception_date, exception.end_time, tz),
    )


async def _context(
    db: AsyncSession,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID | None,
    day: date,
) -> dict[str, Any]:
    schedule = await db.scalar(select(FacilitySchedule).where(FacilitySchedule.facility_id == facility_id))
    tz = timezone(schedule.timezone if schedule else DEFAULT_TIMEZONE)
    start_of_day = local_datetime(day, time.min, tz)
    end_of_day = local_datetime(day + timedelta(days=1), time.min, tz)
    rules = []
    exceptions = []
    if practitioner_id:
        rules = list((await db.scalars(select(PractitionerAvailabilityRule).where(
            PractitionerAvailabilityRule.organization_id == organization_id,
            PractitionerAvailabilityRule.facility_id == facility_id,
            PractitionerAvailabilityRule.practitioner_id == practitioner_id,
            PractitionerAvailabilityRule.status == "active",
        ))).all())
        exceptions = list((await db.scalars(select(PractitionerAvailabilityException).where(
            PractitionerAvailabilityException.organization_id == organization_id,
            PractitionerAvailabilityException.facility_id == facility_id,
            PractitionerAvailabilityException.practitioner_id == practitioner_id,
            PractitionerAvailabilityException.exception_date == day,
        ))).all())
    periods = list((await db.scalars(select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility_id))).all())
    resource_ids = {rule.resource_id for rule in rules if rule.resource_id}
    resources = {
        value.id: value
        for value in (await db.scalars(select(FacilityResource).where(FacilityResource.id.in_(resource_ids)))).all()
    } if resource_ids else {}
    appointments = list((await db.scalars(select(Appointment).where(
        Appointment.organization_id == organization_id,
        Appointment.facility_id == facility_id,
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.scheduled_start < end_of_day,
        Appointment.scheduled_end > start_of_day,
    ))).all())
    return {"schedule": schedule, "tz": tz, "rules": rules, "exceptions": exceptions, "periods": periods, "resources": resources, "appointments": appointments}


def _facility_open(schedule: FacilitySchedule, day: date) -> bool:
    return WEEKDAYS[day.weekday()] in json.loads(schedule.days_of_week)


def _active_rules(context: dict[str, Any], day: date) -> list[PractitionerAvailabilityRule]:
    return [
        rule for rule in context["rules"]
        if rule.weekday == day.weekday()
        and day >= rule.valid_from
        and (rule.valid_until is None or day <= rule.valid_until)
    ]


def _protected(context: dict[str, Any], day: date, start: datetime, end: datetime) -> bool:
    for period in context["periods"]:
        if WEEKDAYS[day.weekday()] not in json.loads(period.days_of_week):
            continue
        if _overlaps(start, end, local_datetime(day, _clock(period.start_time), context["tz"]), local_datetime(day, _clock(period.end_time), context["tz"])):
            return True
    return False


def _conflict(context: dict[str, Any], practitioner_id: UUID, resource_id: UUID | None, start: datetime, end: datetime, ignore_appointment_id: UUID | None = None) -> str | None:
    for appointment in context["appointments"]:
        if appointment.id == ignore_appointment_id or not _overlaps(start, end, appointment.scheduled_start, appointment.scheduled_end):
            continue
        if appointment.practitioner_id == practitioner_id:
            return "BOOKED"
        if resource_id and appointment.resource_id == resource_id:
            return "RESOURCE_BOOKED"
    return None


async def evaluate_availability(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID | None,
    day: date,
    duration_minutes: int | None = None,
    enforce_future: bool = False,
) -> dict[str, Any]:
    context = await _context(db, organization_id, facility_id, practitioner_id, day)
    schedule = context["schedule"]
    facility_open = _facility_open(schedule, day) if schedule else None
    base = {"date": day.isoformat(), "timezone": context["tz"].key, "facility_open": facility_open, "operating_hours": {"start": schedule.operating_start, "end": schedule.operating_end} if schedule else None, "slots": [], "usable_slots": []}
    if schedule is None:
        return base | {"status": "MISSING_CONFIGURATION", "reason": "Facility schedule is not configured."}
    if not facility_open:
        return base | {"status": "FACILITY_CLOSED", "reason": "Facility is closed on this date."}
    if practitioner_id is None:
        return base | {"status": "AVAILABLE", "reason": None}
    if not context["rules"]:
        return base | {"status": "MISSING_CONFIGURATION", "reason": "Practitioner working hours are not configured for this facility."}
    rules = _active_rules(context, day)
    if not rules:
        return base | {"status": "DOCTOR_UNAVAILABLE", "reason": "Practitioner is not scheduled to work on this date."}

    facility_start = local_datetime(day, _clock(schedule.operating_start), context["tz"])
    facility_end = local_datetime(day, _clock(schedule.operating_end), context["tz"])
    now = datetime.now(context["tz"])
    slots: list[dict[str, Any]] = []
    seen: set[tuple[datetime, datetime, UUID | None]] = set()
    for rule in rules:
        resource_id = rule.resource_id
        resource = context["resources"].get(resource_id) if resource_id else None
        start = max(facility_start, local_datetime(day, rule.start_time, context["tz"]))
        finish = min(facility_end, local_datetime(day, rule.end_time, context["tz"]))
        slot_duration = duration_minutes or rule.slot_duration_minutes
        step = timedelta(minutes=max(5, schedule.slot_interval_minutes if duration_minutes else rule.slot_duration_minutes))
        while start + timedelta(minutes=slot_duration) <= finish:
            end = start + timedelta(minutes=slot_duration)
            key = (start, end, resource_id)
            if key in seen:
                start += step
                continue
            seen.add(key)
            status = "AVAILABLE"
            reason = None
            if resource_id and (resource is None or not resource.is_active or resource.facility_id != facility_id):
                status, reason = "MISSING_CONFIGURATION", "Required room/resource is not active at this facility."
            elif any(_unavailable(exception, start, end, context["tz"]) for exception in context["exceptions"]):
                status, reason = "DOCTOR_UNAVAILABLE", "Practitioner is unavailable."
            elif _protected(context, day, start, end):
                status, reason = "PROTECTED_PERIOD", "Facility protected period."
            else:
                conflict = _conflict(context, practitioner_id, resource_id, start, end)
                if conflict:
                    status, reason = conflict, "Existing appointment." if conflict == "BOOKED" else "Required room/resource is already booked."
                elif enforce_future and start < now:
                    status, reason = "PAST", "Slot is in the past."
            slots.append({"start": start.isoformat(), "end": end.isoformat(), "status": status, "bookable": status == "AVAILABLE", "reason": reason, "resource_uuid": str(resource_id) if resource_id else None})
            start += step
    usable = [slot for slot in slots if slot["bookable"]]
    if usable:
        status, reason = "AVAILABLE", None
    elif slots and all(slot["status"] == "MISSING_CONFIGURATION" for slot in slots):
        status, reason = "MISSING_CONFIGURATION", "Required room/resource configuration is missing."
    elif slots and all(slot["status"] == "DOCTOR_UNAVAILABLE" for slot in slots):
        status, reason = "DOCTOR_UNAVAILABLE", "Practitioner is unavailable for the requested date."
    else:
        status, reason = "FULLY_BOOKED", "No usable appointment slots remain for the requested date."
    return base | {"status": status, "reason": reason, "slots": slots, "usable_slots": usable}


async def validate_interval(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID,
    start: datetime,
    end: datetime,
    resource_id: UUID | None = None,
    allowed_overrides: set[str] | None = None,
    ignore_appointment_id: UUID | None = None,
) -> UUID | None:
    allowed_overrides = allowed_overrides or set()
    context = await _context(db, organization_id, facility_id, practitioner_id, to_timezone(start, timezone((await db.scalar(select(FacilitySchedule.timezone).where(FacilitySchedule.facility_id == facility_id))) or DEFAULT_TIMEZONE)).date())
    day = to_timezone(start, context["tz"]).date()
    if to_timezone(end, context["tz"]).date() != day:
        raise ValueError("Appointment must start and end on the same facility-local date.")
    schedule = context["schedule"]
    if schedule is None:
        raise ValueError("MISSING_CONFIGURATION|Facility schedule is not configured.")
    facility_start = local_datetime(day, _clock(schedule.operating_start), context["tz"])
    facility_end = local_datetime(day, _clock(schedule.operating_end), context["tz"])
    facility_closed = not _facility_open(schedule, day) or start < facility_start or end > facility_end
    if facility_closed and "facility_closed" not in allowed_overrides:
        raise ValueError("FACILITY_CLOSED|Facility is closed for the requested interval.")
    can_override_hours = "facility_closed" in allowed_overrides or "practitioner_off_hours" in allowed_overrides
    if not context["rules"] and not can_override_hours:
        raise ValueError("MISSING_CONFIGURATION|Practitioner working hours are not configured for this facility.")
    rules = _active_rules(context, day)
    matching = [rule for rule in rules if start >= local_datetime(day, rule.start_time, context["tz"]) and end <= local_datetime(day, rule.end_time, context["tz"])]
    if not matching and not can_override_hours:
        raise ValueError("DOCTOR_UNAVAILABLE|Practitioner is not working during the requested interval.")
    required_resources = {rule.resource_id for rule in matching if rule.resource_id}
    if not matching and "practitioner_off_hours" in allowed_overrides and "facility_closed" not in allowed_overrides:
        configured_resources = {rule.resource_id for rule in context["rules"] if rule.resource_id}
        required_resources = configured_resources
        if len(configured_resources) > 1 and resource_id is None:
            raise ValueError("RESOURCE_REQUIRED|A required room/resource must be selected for this off-hours booking.")
    if required_resources and resource_id and resource_id not in required_resources:
        raise ValueError("RESOURCE_REQUIRED|The requested interval requires its configured room/resource.")
    resolved_resource = resource_id or next(iter(required_resources), None)
    if resolved_resource:
        resource = await db.scalar(select(FacilityResource).where(FacilityResource.id == resolved_resource, FacilityResource.facility_id == facility_id, FacilityResource.is_active.is_(True)))
        if resource is None:
            raise ValueError("MISSING_CONFIGURATION|Required room/resource is not active at this facility.")
    if any(_unavailable(exception, start, end, context["tz"]) for exception in context["exceptions"]):
        raise ValueError("DOCTOR_UNAVAILABLE|Practitioner is on leave or otherwise unavailable.")
    if _protected(context, day, start, end):
        raise ValueError("PROTECTED_PERIOD|Requested interval is protected.")
    conflict = _conflict(context, practitioner_id, resolved_resource, start, end, ignore_appointment_id)
    if conflict:
        raise ValueError(f"{conflict}|The requested interval conflicts with an existing appointment.")
    return resolved_resource
