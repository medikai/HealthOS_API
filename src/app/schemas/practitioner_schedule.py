from datetime import date, time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WEEKDAY_SET = set(WEEKDAYS)


def parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid HH:MM time format: {value}") from None


class BreakInterval(BaseModel):
    title: str = Field(default="Break", max_length=100)
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")

    @model_validator(mode="after")
    def validate_interval(self):
        start = parse_clock(self.start_time)
        end = parse_clock(self.end_time)
        if end <= start:
            raise ValueError(
                "UNSUPPORTED_OVERNIGHT_INTERVAL|Break end_time must be after start_time. Overnight intervals are not supported."
            )
        return self


class DaySchedule(BaseModel):
    day_of_week: str
    is_working: bool = True
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    breaks: list[BreakInterval] = Field(default_factory=list)

    @field_validator("day_of_week")
    @classmethod
    def validate_day_of_week(cls, value: str) -> str:
        norm = value.strip().lower()
        if norm not in WEEKDAY_SET:
            raise ValueError(f"day_of_week must be one of: {', '.join(WEEKDAYS)}")
        return norm

    @model_validator(mode="after")
    def validate_day(self):
        if self.is_working:
            if not self.start_time or not self.end_time:
                raise ValueError(f"Working day '{self.day_of_week}' must have start_time and end_time configured.")
            day_start = parse_clock(self.start_time)
            day_end = parse_clock(self.end_time)
            if day_end <= day_start:
                raise ValueError(
                    f"UNSUPPORTED_OVERNIGHT_INTERVAL|Operating end_time must be after start_time for '{self.day_of_week}'. Overnight schedule intervals crossing midnight are not currently supported."
                )

            # Validate breaks are contained and non-overlapping
            sorted_breaks = sorted(self.breaks, key=lambda b: parse_clock(b.start_time))
            for i, brk in enumerate(sorted_breaks):
                b_start = parse_clock(brk.start_time)
                b_end = parse_clock(brk.end_time)
                if b_start < day_start or b_end > day_end:
                    raise ValueError(
                        f"Break '{brk.title}' ({brk.start_time}-{brk.end_time}) falls outside working hours ({self.start_time}-{self.end_time}) on '{self.day_of_week}'."
                    )
                if i > 0:
                    prev_end = parse_clock(sorted_breaks[i - 1].end_time)
                    if b_start < prev_end:
                        raise ValueError(
                            f"Overlapping breaks detected on '{self.day_of_week}': '{sorted_breaks[i - 1].title}' and '{brk.title}'."
                        )
        return self


class ScheduleDateException(BaseModel):
    date: date
    exception_type: str = Field(default="leave", min_length=1, max_length=64)
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_exception_times(self):
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must either both be provided (partial day) or both omitted (full day).")
        if self.start_time and self.end_time:
            start = parse_clock(self.start_time)
            end = parse_clock(self.end_time)
            if end <= start:
                raise ValueError(
                    "UNSUPPORTED_OVERNIGHT_INTERVAL|Exception end_time must be after start_time. Overnight intervals are not supported."
                )
        return self


class PractitionerScheduleInput(BaseModel):
    facility_uuid: UUID
    timezone: str = "Asia/Kolkata"
    slot_interval_minutes: int = Field(default=30, ge=5, le=240)
    effective_from: date | None = None
    effective_to: date | None = None
    version: int | None = None
    weekly_hours: list[DaySchedule]
    date_exceptions: list[ScheduleDateException] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("timezone must be a valid IANA timezone (e.g. 'Asia/Kolkata', 'America/New_York')") from None
        return value

    @model_validator(mode="after")
    def validate_schedule_structure(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from.")

        seen_days: set[str] = set()
        for day in self.weekly_hours:
            if day.day_of_week in seen_days:
                raise ValueError(f"Duplicate day of week '{day.day_of_week}' in weekly_hours.")
            seen_days.add(day.day_of_week)

        # Check overlapping exceptions on same date
        exceptions_by_date: dict[date, list[ScheduleDateException]] = {}
        for exc in self.date_exceptions:
            exceptions_by_date.setdefault(exc.date, []).append(exc)

        for exc_date, exc_list in exceptions_by_date.items():
            if len(exc_list) > 1:
                # If any is full day
                if any(e.start_time is None for e in exc_list):
                    raise ValueError(f"Multiple overlapping date exceptions on {exc_date.isoformat()}.")
                # Check partial day overlaps
                sorted_partial = sorted(exc_list, key=lambda e: parse_clock(e.start_time))
                for i in range(1, len(sorted_partial)):
                    prev_end = parse_clock(sorted_partial[i - 1].end_time)
                    curr_start = parse_clock(sorted_partial[i].start_time)
                    if curr_start < prev_end:
                        raise ValueError(f"Overlapping date exceptions on {exc_date.isoformat()}.")
        return self


class PractitionerScheduleResetInput(BaseModel):
    effective_date: date | None = None


class PractitionerScheduleResponseData(BaseModel):
    practitioner_uuid: str
    facility_uuid: str
    organization_uuid: str
    timezone: str
    slot_interval_minutes: int
    effective_from: str | None = None
    effective_to: str | None = None
    is_override: bool
    inherited: bool
    version: int
    weekly_hours: list[dict[str, Any]]
    date_exceptions: list[dict[str, Any]]
    updated_at: str | None = None


class PractitionerScheduleResponse(BaseModel):
    success: bool = True
    data: PractitionerScheduleResponseData
    meta: dict[str, Any] = Field(default_factory=dict)
