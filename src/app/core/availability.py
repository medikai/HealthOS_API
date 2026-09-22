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
    PractitionerSchedule,
)
from ..models.organization import FacilityResource, FacilitySchedule, ProtectedPeriod
from .timezones import DEFAULT_TIMEZONE, local_datetime, timezone, to_timezone

ACTIVE_APPOINTMENT_STATUSES = ("booked", "confirmed", "checked_in", "in_consultation")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

BOOKED_STATUSES = {"BOOKED", "CROSS_FACILITY_BOOKED", "RESOURCE_BOOKED", "FULLY_BOOKED"}
BLOCKED_STATUSES = {"BREAK", "PROTECTED_PERIOD", "DOCTOR_UNAVAILABLE", "PAST", "MISSING_CONFIGURATION"}
CLOSED_STATUSES = {"FACILITY_CLOSED"}


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and end > other_start


def _categorize_state(status: str) -> str:
    if status == "AVAILABLE":
        return "AVAILABLE"
    if status in BOOKED_STATUSES:
        return "BOOKED"
    if status in CLOSED_STATUSES:
        return "CLOSED"
    if status in BLOCKED_STATUSES:
        return "BLOCKED"
    return "BLOCKED"


def _clock(value: str) -> time:
    if len(value) == 5:
        return time.fromisoformat(value)
    return time.fromisoformat(value[:8])


def rules_from_facility_schedule(
    schedule: FacilitySchedule,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID,
) -> list[PractitionerAvailabilityRule]:
    valid_from = datetime.now(timezone(schedule.timezone)).date()
    return [
        PractitionerAvailabilityRule(
            organization_id=organization_id,
            facility_id=facility_id,
            practitioner_id=practitioner_id,
            weekday=WEEKDAYS.index(day),
            start_time=_clock(schedule.operating_start),
            end_time=_clock(schedule.operating_end),
            slot_duration_minutes=schedule.slot_interval_minutes,
            valid_from=valid_from,
        )
        for value in json.loads(schedule.days_of_week)
        if (day := value.lower()) in WEEKDAYS
    ]


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

    practitioner_schedule = None
    rules = []
    exceptions = []
    if practitioner_id:
        # 1. Check for active PractitionerSchedule override
        practitioner_schedule = await db.scalar(
            select(PractitionerSchedule)
            .where(
                PractitionerSchedule.organization_id == organization_id,
                PractitionerSchedule.facility_id == facility_id,
                PractitionerSchedule.practitioner_id == practitioner_id,
                PractitionerSchedule.is_active.is_(True),
                PractitionerSchedule.effective_from <= day,
            )
            .filter(
                (PractitionerSchedule.effective_to.is_(None)) | (PractitionerSchedule.effective_to >= day)
            )
            .order_by(PractitionerSchedule.created_at.desc())
        )

        # 2. If no PractitionerSchedule override, load legacy rules
        if practitioner_schedule is None:
            rules = list((await db.scalars(select(PractitionerAvailabilityRule).where(
                PractitionerAvailabilityRule.organization_id == organization_id,
                PractitionerAvailabilityRule.facility_id == facility_id,
                PractitionerAvailabilityRule.practitioner_id == practitioner_id,
                PractitionerAvailabilityRule.status == "active",
            ))).all())

        # Load legacy exceptions
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

    # Query all active appointments for practitioner across ALL facilities (cross-facility check)
    # AND all active appointments for this facility's resources
    appointments_query = select(Appointment).where(
        Appointment.organization_id == organization_id,
        Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
        Appointment.scheduled_start < end_of_day,
        Appointment.scheduled_end > start_of_day,
    )
    if practitioner_id:
        appointments_query = appointments_query.where(
            (Appointment.practitioner_id == practitioner_id) | (Appointment.facility_id == facility_id)
        )
    else:
        appointments_query = appointments_query.where(Appointment.facility_id == facility_id)

    appointments = list((await db.scalars(appointments_query)).all())

    return {
        "schedule": schedule,
        "tz": tz,
        "practitioner_schedule": practitioner_schedule,
        "rules": rules,
        "exceptions": exceptions,
        "periods": periods,
        "resources": resources,
        "appointments": appointments,
    }


def _facility_open(schedule: Any, day: date) -> bool:
    days_json = getattr(schedule, "days_of_week", None) or getattr(schedule, "operating_days", None)
    if not days_json:
        return True
    try:
        allowed = {str(d).lower() for d in json.loads(days_json)}
        weekday_name = WEEKDAYS[day.weekday()]
        short_name = weekday_name[:3]
        return weekday_name in allowed or short_name in allowed
    except Exception:
        return False


def _resolve_doctor_day_plan(
    context: dict[str, Any],
    day: date,
) -> dict[str, Any]:
    """
    Resolves doctor working hours, breaks, and date exceptions for a specific date.
    Hierarchy:
    1. Date-specific doctor exceptions
    2. Weekly doctor rules (PractitionerSchedule override or legacy rules)
    3. Inherited practice working hours
    """
    tz = context["tz"]
    schedule = context["schedule"]
    ps = context.get("practitioner_schedule")
    weekday_name = WEEKDAYS[day.weekday()]
    slot_interval = getattr(schedule, "slot_interval_minutes", 30) if schedule else 30

    # Result structure
    plan: dict[str, Any] = {
        "is_working": False,
        "start_time": None,
        "end_time": None,
        "breaks": [],  # list of (start_dt, end_dt)
        "leaves": [],  # list of (start_dt, end_dt)
        "is_full_day_leave": False,
        "reason": None,
        "slot_interval_minutes": slot_interval,
        "resource_id": None,
    }

    if ps is not None:
        plan["slot_interval_minutes"] = getattr(ps, "slot_interval_minutes", None) or slot_interval

        # Check date_exceptions in PractitionerSchedule
        date_str = day.isoformat()
        date_exceptions = [
            exc for exc in getattr(ps, "date_exceptions", [])
            if exc.get("date") == date_str or exc.get("date") == day
        ]

        custom_hours_exc = None
        for exc in date_exceptions:
            exc_type = str(exc.get("exception_type", "")).lower()
            if exc_type in {"leave", "unavailable", "off", "holiday", "vacation", "sick_leave"}:
                start_t_str = exc.get("start_time")
                end_t_str = exc.get("end_time")
                if not start_t_str or not end_t_str:
                    plan["is_full_day_leave"] = True
                    plan["reason"] = exc.get("reason") or "Practitioner is on leave."
                    return plan
                else:
                    leave_start = local_datetime(day, _clock(start_t_str), tz)
                    leave_end = local_datetime(day, _clock(end_t_str), tz)
                    plan["leaves"].append((leave_start, leave_end))
            elif exc_type in {"custom_hours", "working", "available", "extra_hours"}:
                custom_hours_exc = exc

        # Resolve weekly hours
        if custom_hours_exc is not None and custom_hours_exc.get("start_time") and custom_hours_exc.get("end_time"):
            plan["is_working"] = True
            plan["start_time"] = _clock(custom_hours_exc["start_time"])
            plan["end_time"] = _clock(custom_hours_exc["end_time"])
        else:
            weekly_hours = getattr(ps, "weekly_hours", [])
            day_entry = next((d for d in weekly_hours if d.get("day_of_week") == weekday_name), None)
            if day_entry and day_entry.get("is_working", False) and day_entry.get("start_time") and day_entry.get("end_time"):
                plan["is_working"] = True
                plan["start_time"] = _clock(day_entry["start_time"])
                plan["end_time"] = _clock(day_entry["end_time"])
                for brk in day_entry.get("breaks", []):
                    if brk.get("start_time") and brk.get("end_time"):
                        b_start = local_datetime(day, _clock(brk["start_time"]), tz)
                        b_end = local_datetime(day, _clock(brk["end_time"]), tz)
                        plan["breaks"].append((b_start, b_end))
            else:
                plan["is_working"] = False
                plan["reason"] = "Practitioner is not scheduled to work on this date."

    elif context.get("rules"):
        # Legacy PractitionerAvailabilityRule
        matching_rules = [
            rule for rule in context["rules"]
            if rule.weekday == day.weekday()
            and day >= rule.valid_from
            and (rule.valid_until is None or day <= rule.valid_until)
        ]
        if matching_rules:
            rule = matching_rules[0]
            plan["is_working"] = True
            plan["start_time"] = rule.start_time
            plan["end_time"] = rule.end_time
            plan["slot_interval_minutes"] = getattr(rule, "slot_duration_minutes", slot_interval)
            plan["resource_id"] = getattr(rule, "resource_id", None)
        else:
            plan["is_working"] = False
            plan["reason"] = "Practitioner is not scheduled to work on this date."

    else:
        # Inherited practice working hours
        if schedule is not None and _facility_open(schedule, day):
            plan["is_working"] = True
            plan["start_time"] = _clock(getattr(schedule, "operating_start", "08:00"))
            plan["end_time"] = _clock(getattr(schedule, "operating_end", "20:00"))
            plan["slot_interval_minutes"] = slot_interval
        else:
            plan["is_working"] = False
            plan["reason"] = "Facility is closed on this date."

    # Legacy exceptions check
    for exc in context.get("exceptions", []):
        exc_type = str(exc.exception_type).lower()
        if exc_type in {"leave", "unavailable", "off", "holiday", "vacation"}:
            if exc.start_time is None or exc.end_time is None:
                plan["is_full_day_leave"] = True
                plan["reason"] = getattr(exc, "reason", None) or "Practitioner is on leave."
                return plan
            else:
                l_start = local_datetime(day, exc.start_time, tz)
                l_end = local_datetime(day, exc.end_time, tz)
                plan["leaves"].append((l_start, l_end))

    return plan


def _protected(context: dict[str, Any], day: date, start: datetime, end: datetime) -> bool:
    weekday_name = WEEKDAYS[day.weekday()]
    for period in context["periods"]:
        try:
            period_days = [d.lower() for d in json.loads(period.days_of_week)]
        except Exception:
            period_days = list(WEEKDAYS)
        if weekday_name not in period_days:
            continue
        p_start = local_datetime(day, _clock(period.start_time), context["tz"])
        p_end = local_datetime(day, _clock(period.end_time), context["tz"])
        if _overlaps(start, end, p_start, p_end):
            return True
    return False


def _conflict(
    context: dict[str, Any],
    facility_id: UUID,
    practitioner_id: UUID,
    resource_id: UUID | None,
    start: datetime,
    end: datetime,
    ignore_appointment_id: UUID | None = None,
) -> tuple[str | None, str | None]:
    for appointment in context["appointments"]:
        if appointment.id == ignore_appointment_id or not _overlaps(start, end, appointment.scheduled_start, appointment.scheduled_end):
            continue
        app_fac_id = getattr(appointment, "facility_id", facility_id)
        if appointment.practitioner_id == practitioner_id:
            if app_fac_id != facility_id:
                return "CROSS_FACILITY_BOOKED", "Practitioner is booked at another facility."
            return "BOOKED", "Existing appointment."
        if resource_id and app_fac_id == facility_id and getattr(appointment, "resource_id", None) == resource_id:
            return "RESOURCE_BOOKED", "Required room/resource is already booked."
    return None, None


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
    tz = context["tz"]
    facility_open = _facility_open(schedule, day) if schedule else False
    op_start = getattr(schedule, "operating_start", "08:00") if schedule else None
    op_end = getattr(schedule, "operating_end", "20:00") if schedule else None
    base = {
        "date": day.isoformat(),
        "timezone": tz.key,
        "facility_open": facility_open,
        "operating_hours": {"start": op_start, "end": op_end} if schedule else None,
        "slots": [],
        "usable_slots": [],
    }

    if schedule is None:
        return base | {"state": "BLOCKED", "status": "MISSING_CONFIGURATION", "reason": "Facility schedule is not configured."}
    if not facility_open:
        return base | {"state": "CLOSED", "status": "FACILITY_CLOSED", "reason": "Facility is closed on this date."}
    if practitioner_id is None:
        return base | {"state": "AVAILABLE", "status": "AVAILABLE", "reason": None}

    plan = _resolve_doctor_day_plan(context, day)
    if plan["is_full_day_leave"]:
        return base | {"state": "BLOCKED", "status": "DOCTOR_UNAVAILABLE", "reason": plan["reason"] or "Practitioner is on leave."}
    if not plan["is_working"] or not plan["start_time"] or not plan["end_time"]:
        return base | {"state": "BLOCKED", "status": "DOCTOR_UNAVAILABLE", "reason": plan["reason"] or "Practitioner is not scheduled to work on this date."}

    facility_start = local_datetime(day, _clock(getattr(schedule, "operating_start", "08:00")), tz)
    facility_end = local_datetime(day, _clock(getattr(schedule, "operating_end", "20:00")), tz)
    doctor_start = local_datetime(day, plan["start_time"], tz)
    doctor_end = local_datetime(day, plan["end_time"], tz)

    start_window = max(facility_start, doctor_start)
    finish_window = min(facility_end, doctor_end)

    if start_window >= finish_window:
        return base | {"state": "BLOCKED", "status": "DOCTOR_UNAVAILABLE", "reason": "Practitioner working hours fall outside facility operating hours."}

    slot_duration = duration_minutes or plan["slot_interval_minutes"]
    step = timedelta(minutes=max(5, duration_minutes if duration_minutes else plan["slot_interval_minutes"]))
    now = datetime.now(tz)
    resource_id = plan["resource_id"]
    resource = context["resources"].get(resource_id) if resource_id else None

    slots: list[dict[str, Any]] = []
    seen: set[tuple[datetime, datetime, UUID | None]] = set()
    current = start_window

    while current + timedelta(minutes=slot_duration) <= finish_window:
        end = current + timedelta(minutes=slot_duration)
        key = (current, end, resource_id)
        if key in seen:
            current += step
            continue
        seen.add(key)

        status = "AVAILABLE"
        reason = None

        if resource_id and (resource is None or not resource.is_active or resource.facility_id != facility_id):
            status, reason = "MISSING_CONFIGURATION", "Required room/resource is not active at this facility."
        elif any(_overlaps(current, end, l_start, l_end) for l_start, l_end in plan["leaves"]):
            status, reason = "DOCTOR_UNAVAILABLE", "Practitioner is on leave."
        elif any(_overlaps(current, end, b_start, b_end) for b_start, b_end in plan["breaks"]):
            status, reason = "BREAK", "Doctor break."
        elif _protected(context, day, current, end):
            status, reason = "PROTECTED_PERIOD", "Facility protected period."
        else:
            conflict_code, conflict_msg = _conflict(context, facility_id, practitioner_id, resource_id, current, end)
            if conflict_code:
                status, reason = conflict_code, conflict_msg
            elif enforce_future and current < now:
                status, reason = "PAST", "Slot is in the past."

        slots.append({
            "start": current.isoformat(),
            "end": end.isoformat(),
            "state": _categorize_state(status),
            "status": status,
            "bookable": status == "AVAILABLE",
            "reason": reason,
            "resource_uuid": str(resource_id) if resource_id else None,
        })
        current += step

    usable = [slot for slot in slots if slot["bookable"]]
    if usable:
        status, reason = "AVAILABLE", None
    elif slots and all(slot["status"] == "MISSING_CONFIGURATION" for slot in slots):
        status, reason = "MISSING_CONFIGURATION", "Required room/resource configuration is missing."
    elif slots and all(slot["status"] == "DOCTOR_UNAVAILABLE" for slot in slots):
        status, reason = "DOCTOR_UNAVAILABLE", "Practitioner is unavailable for the requested date."
    elif slots and all(slot["status"] == "BREAK" for slot in slots):
        status, reason = "BREAK", "Doctor is on break for the entire requested window."
    else:
        status, reason = "FULLY_BOOKED", "No usable appointment slots remain for the requested date."

    return base | {"state": _categorize_state(status), "status": status, "reason": reason, "slots": slots, "usable_slots": usable}


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
    schedule_tz_value = await db.scalar(
        select(FacilitySchedule.timezone).where(FacilitySchedule.facility_id == facility_id)
    )
    schedule_tz_name = getattr(schedule_tz_value, "timezone", schedule_tz_value) or DEFAULT_TIMEZONE
    facility_tz = timezone(schedule_tz_name)
    day = to_timezone(start, facility_tz).date()

    if to_timezone(end, facility_tz).date() != day:
        raise ValueError("Appointment must start and end on the same facility-local date.")

    context = await _context(db, organization_id, facility_id, practitioner_id, day)
    schedule = context["schedule"]
    if schedule is None:
        raise ValueError("MISSING_CONFIGURATION|Facility schedule is not configured.")

    facility_start = local_datetime(day, _clock(getattr(schedule, "operating_start", "08:00")), context["tz"])
    facility_end = local_datetime(day, _clock(getattr(schedule, "operating_end", "20:00")), context["tz"])
    facility_open = _facility_open(schedule, day)
    facility_closed = not facility_open or start < facility_start or end > facility_end

    if facility_closed and "facility_closed" not in allowed_overrides:
        raise ValueError("FACILITY_CLOSED|Facility is closed for the requested interval.")

    can_override_hours = "facility_closed" in allowed_overrides or "practitioner_off_hours" in allowed_overrides

    plan = _resolve_doctor_day_plan(context, day)
    if plan["is_full_day_leave"] and not can_override_hours:
        raise ValueError("DOCTOR_UNAVAILABLE|Practitioner is on leave or otherwise unavailable.")

    doctor_working = (
        plan["is_working"]
        and plan["start_time"]
        and plan["end_time"]
        and start >= local_datetime(day, plan["start_time"], context["tz"])
        and end <= local_datetime(day, plan["end_time"], context["tz"])
    )

    if not doctor_working and not can_override_hours:
        raise ValueError("DOCTOR_UNAVAILABLE|Practitioner is not working during the requested interval.")

    # Check partial day leave
    if any(_overlaps(start, end, l_start, l_end) for l_start, l_end in plan["leaves"]) and not can_override_hours:
        raise ValueError("DOCTOR_UNAVAILABLE|Practitioner is on leave during the requested interval.")

    # Check breaks
    if any(_overlaps(start, end, b_start, b_end) for b_start, b_end in plan["breaks"]) and not can_override_hours:
        raise ValueError("BREAK|Requested interval overlaps a doctor break.")

    # Check protected periods
    if _protected(context, day, start, end) and not can_override_hours:
        raise ValueError("PROTECTED_PERIOD|Requested interval is protected.")

    resolved_resource = resource_id or plan.get("resource_id")
    if resolved_resource:
        resource = context["resources"].get(resolved_resource)
        if resource is None:
            resource = await db.scalar(
                select(FacilityResource).where(
                    FacilityResource.id == resolved_resource,
                    FacilityResource.facility_id == facility_id,
                    FacilityResource.is_active.is_(True),
                )
            )
        if resource is None or not resource.is_active or resource.facility_id != facility_id:
            raise ValueError("MISSING_CONFIGURATION|Required room/resource is not active at this facility.")

    # Check conflicts (existing appointment or cross-facility)
    conflict_code, conflict_msg = _conflict(
        context, facility_id, practitioner_id, resolved_resource, start, end, ignore_appointment_id
    )
    if conflict_code:
        raise ValueError(f"{conflict_code}|{conflict_msg}")

    return resolved_resource


async def find_schedule_conflicts(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID,
    proposed_weekly_hours: list[dict[str, Any]],
    proposed_date_exceptions: list[dict[str, Any]],
    proposed_timezone: str,
    effective_from: date,
    effective_to: date | None = None,
) -> list[dict[str, Any]]:
    """
    Evaluates future active appointments for a practitioner against a proposed schedule update.
    Returns a list of conflicting appointments.
    """
    tz = timezone(proposed_timezone)
    now_utc = datetime.now(tz)

    future_appointments = list((await db.scalars(
        select(Appointment).where(
            Appointment.organization_id == organization_id,
            Appointment.facility_id == facility_id,
            Appointment.practitioner_id == practitioner_id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.scheduled_end > now_utc,
        ).order_by(Appointment.scheduled_start)
    )).all())

    if not future_appointments:
        return []

    schedule = await db.scalar(select(FacilitySchedule).where(FacilitySchedule.facility_id == facility_id))
    periods = list((await db.scalars(select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility_id))).all())

    conflicts = []
    for app in future_appointments:
        app_start_local = to_timezone(app.scheduled_start, tz)
        app_end_local = to_timezone(app.scheduled_end, tz)
        app_day = app_start_local.date()

        # Check if appointment falls within the effective date range of this schedule
        if app_day < effective_from:
            continue
        if effective_to is not None and app_day > effective_to:
            continue

        weekday_name = WEEKDAYS[app_day.weekday()]

        # 1. Facility checks
        if schedule is not None:
            if not _facility_open(schedule, app_day):
                conflicts.append({
                    "appointment_uuid": str(app.id),
                    "patient_uuid": str(app.patient_id),
                    "scheduled_start": app.scheduled_start.isoformat(),
                    "scheduled_end": app.scheduled_end.isoformat(),
                    "conflict_reason": "FACILITY_CLOSED",
                })
                continue
            fac_start = local_datetime(app_day, _clock(getattr(schedule, "operating_start", "08:00")), tz)
            fac_end = local_datetime(app_day, _clock(getattr(schedule, "operating_end", "20:00")), tz)
            if app_start_local < fac_start or app_end_local > fac_end:
                conflicts.append({
                    "appointment_uuid": str(app.id),
                    "patient_uuid": str(app.patient_id),
                    "scheduled_start": app.scheduled_start.isoformat(),
                    "scheduled_end": app.scheduled_end.isoformat(),
                    "conflict_reason": "FACILITY_CLOSED",
                })
                continue

        # 2. Check proposed date exceptions
        date_str = app_day.isoformat()
        date_exceptions = [
            exc for exc in proposed_date_exceptions
            if exc.get("date") == date_str or exc.get("date") == app_day
        ]

        full_day_leave = False
        partial_leaves = []
        custom_hours = None

        for exc in date_exceptions:
            exc_type = str(exc.get("exception_type", "")).lower()
            if exc_type in {"leave", "unavailable", "off", "holiday", "vacation", "sick_leave"}:
                start_t = exc.get("start_time")
                end_t = exc.get("end_time")
                if not start_t or not end_t:
                    full_day_leave = True
                    break
                else:
                    partial_leaves.append((
                        local_datetime(app_day, _clock(start_t), tz),
                        local_datetime(app_day, _clock(end_t), tz)
                    ))
            elif exc_type in {"custom_hours", "working", "available", "extra_hours"}:
                custom_hours = exc

        if full_day_leave:
            conflicts.append({
                "appointment_uuid": str(app.id),
                "patient_uuid": str(app.patient_id),
                "scheduled_start": app.scheduled_start.isoformat(),
                "scheduled_end": app.scheduled_end.isoformat(),
                "conflict_reason": "DOCTOR_UNAVAILABLE",
            })
            continue

        if any(_overlaps(app_start_local, app_end_local, l_start, l_end) for l_start, l_end in partial_leaves):
            conflicts.append({
                "appointment_uuid": str(app.id),
                "patient_uuid": str(app.patient_id),
                "scheduled_start": app.scheduled_start.isoformat(),
                "scheduled_end": app.scheduled_end.isoformat(),
                "conflict_reason": "DOCTOR_UNAVAILABLE",
            })
            continue

        # 3. Check proposed working hours
        if custom_hours and custom_hours.get("start_time") and custom_hours.get("end_time"):
            doc_start = local_datetime(app_day, _clock(custom_hours["start_time"]), tz)
            doc_end = local_datetime(app_day, _clock(custom_hours["end_time"]), tz)
            breaks = []
        else:
            day_entry = next((d for d in proposed_weekly_hours if d.get("day_of_week") == weekday_name), None)
            if not day_entry or not day_entry.get("is_working", False) or not day_entry.get("start_time") or not day_entry.get("end_time"):
                conflicts.append({
                    "appointment_uuid": str(app.id),
                    "patient_uuid": str(app.patient_id),
                    "scheduled_start": app.scheduled_start.isoformat(),
                    "scheduled_end": app.scheduled_end.isoformat(),
                    "conflict_reason": "DOCTOR_UNAVAILABLE",
                })
                continue
            doc_start = local_datetime(app_day, _clock(day_entry["start_time"]), tz)
            doc_end = local_datetime(app_day, _clock(day_entry["end_time"]), tz)
            breaks = [
                (local_datetime(app_day, _clock(b["start_time"]), tz), local_datetime(app_day, _clock(b["end_time"]), tz))
                for b in day_entry.get("breaks", [])
                if b.get("start_time") and b.get("end_time")
            ]

        if app_start_local < doc_start or app_end_local > doc_end:
            conflicts.append({
                "appointment_uuid": str(app.id),
                "patient_uuid": str(app.patient_id),
                "scheduled_start": app.scheduled_start.isoformat(),
                "scheduled_end": app.scheduled_end.isoformat(),
                "conflict_reason": "DOCTOR_UNAVAILABLE",
            })
            continue

        # 4. Check proposed breaks
        if any(_overlaps(app_start_local, app_end_local, b_start, b_end) for b_start, b_end in breaks):
            conflicts.append({
                "appointment_uuid": str(app.id),
                "patient_uuid": str(app.patient_id),
                "scheduled_start": app.scheduled_start.isoformat(),
                "scheduled_end": app.scheduled_end.isoformat(),
                "conflict_reason": "BREAK",
            })
            continue

    return conflicts


async def find_facility_schedule_conflicts(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID,
    proposed_operating_start: str,
    proposed_operating_end: str,
    proposed_days_of_week: list[str],
    proposed_timezone: str,
    proposed_slot_interval_minutes: int | None = None,
    proposed_protected_periods: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Evaluates future active appointments across ALL practitioners at a facility
    against a proposed facility schedule update. Returns a list of conflicting
    appointments so staff can explicitly resolve them before the edit is applied.
    """
    tz = timezone(proposed_timezone)
    now_utc = datetime.now(tz)

    future_appointments = list((await db.scalars(
        select(Appointment).where(
            Appointment.organization_id == organization_id,
            Appointment.facility_id == facility_id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.scheduled_end > now_utc,
        ).order_by(Appointment.scheduled_start)
    )).all())

    if not future_appointments:
        return []

    try:
        allowed_days = {str(d).lower() for d in proposed_days_of_week}
    except Exception:
        allowed_days = set(WEEKDAYS)

    periods: list[dict[str, Any]] = []
    if proposed_protected_periods is not None:
        periods = proposed_protected_periods
    else:
        existing_periods = list((await db.scalars(
            select(ProtectedPeriod).where(ProtectedPeriod.facility_id == facility_id)
        )).all())
        for p in existing_periods:
            try:
                p_days = [d.lower() for d in json.loads(p.days_of_week)]
            except Exception:
                p_days = list(WEEKDAYS)
            periods.append({
                "start_time": p.start_time,
                "end_time": p.end_time,
                "days_of_week": p_days,
            })

    conflicts = []
    for app in future_appointments:
        app_start_local = to_timezone(app.scheduled_start, tz)
        app_end_local = to_timezone(app.scheduled_end, tz)
        app_day = app_start_local.date()
        weekday_name = WEEKDAYS[app_day.weekday()]
        short_name = weekday_name[:3]

        if weekday_name not in allowed_days and short_name not in allowed_days:
            conflicts.append({
                "appointment_uuid": str(app.id),
                "practitioner_uuid": str(app.practitioner_id),
                "patient_uuid": str(app.patient_id),
                "scheduled_start": app.scheduled_start.isoformat(),
                "scheduled_end": app.scheduled_end.isoformat(),
                "conflict_reason": "FACILITY_CLOSED",
            })
            continue

        fac_start = local_datetime(app_day, _clock(proposed_operating_start), tz)
        fac_end = local_datetime(app_day, _clock(proposed_operating_end), tz)
        if app_start_local < fac_start or app_end_local > fac_end:
            conflicts.append({
                "appointment_uuid": str(app.id),
                "practitioner_uuid": str(app.practitioner_id),
                "patient_uuid": str(app.patient_id),
                "scheduled_start": app.scheduled_start.isoformat(),
                "scheduled_end": app.scheduled_end.isoformat(),
                "conflict_reason": "FACILITY_CLOSED",
            })
            continue

        for period in periods:
            try:
                p_days = [d.lower() for d in period.get("days_of_week", WEEKDAYS)]
            except Exception:
                p_days = list(WEEKDAYS)
            if weekday_name not in p_days and short_name not in p_days:
                continue
            p_start = local_datetime(app_day, _clock(period["start_time"]), tz)
            p_end = local_datetime(app_day, _clock(period["end_time"]), tz)
            if _overlaps(app_start_local, app_end_local, p_start, p_end):
                conflicts.append({
                    "appointment_uuid": str(app.id),
                    "practitioner_uuid": str(app.practitioner_id),
                    "patient_uuid": str(app.patient_id),
                    "scheduled_start": app.scheduled_start.isoformat(),
                    "scheduled_end": app.scheduled_end.isoformat(),
                    "conflict_reason": "PROTECTED_PERIOD",
                })
                break

    return conflicts


async def find_legacy_rule_conflicts(
    db: AsyncSession,
    *,
    organization_id: UUID,
    facility_id: UUID,
    practitioner_id: UUID,
    effective_date: date,
) -> list[dict[str, Any]]:
    """
    After any legacy availability rule/exception edit, re-evaluate the affected
    practitioner's future appointments for conflicts. Returns a conflict list
    that must be resolved by staff before the edit can proceed.
    """
    tz_name = (
        await db.scalar(select(FacilitySchedule.timezone).where(FacilitySchedule.facility_id == facility_id))
    ) or DEFAULT_TIMEZONE
    tz = timezone(tz_name)
    now_utc = datetime.now(tz)

    future_appointments = list((await db.scalars(
        select(Appointment).where(
            Appointment.organization_id == organization_id,
            Appointment.facility_id == facility_id,
            Appointment.practitioner_id == practitioner_id,
            Appointment.status.in_(ACTIVE_APPOINTMENT_STATUSES),
            Appointment.scheduled_end > now_utc,
        ).order_by(Appointment.scheduled_start)
    )).all())

    if not future_appointments:
        return []

    conflicts = []
    for app in future_appointments:
        app_day = to_timezone(app.scheduled_start, tz).date()
        if app_day < effective_date:
            continue
        try:
            await validate_interval(
                db,
                organization_id=organization_id,
                facility_id=facility_id,
                practitioner_id=practitioner_id,
                start=app.scheduled_start,
                end=app.scheduled_end,
                resource_id=getattr(app, "resource_id", None),
                ignore_appointment_id=app.id,
            )
        except ValueError as exc:
            code, _, _ = str(exc).partition("|")
            conflicts.append({
                "appointment_uuid": str(app.id),
                "patient_uuid": str(app.patient_id),
                "scheduled_start": app.scheduled_start.isoformat(),
                "scheduled_end": app.scheduled_end.isoformat(),
                "conflict_reason": code or "AVAILABILITY_CHANGED",
            })

    return conflicts
