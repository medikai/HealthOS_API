from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


class FacilityScheduleInput(BaseModel):
    operating_start: str = Field(pattern=r"^\d{2}:\d{2}$")
    operating_end: str = Field(pattern=r"^\d{2}:\d{2}$")
    slot_interval_minutes: int = Field(default=30, ge=5, le=240)
    days_of_week: list[str]
    timezone: str = "Asia/Kolkata"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("timezone must be a valid IANA timezone") from None
        return value

    @field_validator("days_of_week")
    @classmethod
    def validate_days(cls, value: list[str]) -> list[str]:
        if not value or any(day not in WEEKDAYS for day in value):
            raise ValueError("days_of_week must contain valid lowercase weekday names")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_hours(self):
        try:
            start, end = time.fromisoformat(self.operating_start), time.fromisoformat(self.operating_end)
        except ValueError:
            raise ValueError("operating hours must be valid HH:MM values") from None
        if end <= start:
            raise ValueError("operating_end must be after operating_start")
        return self


class ProtectedPeriodInput(BaseModel):
    title: str
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    period_type: str = "protected"
    days_of_week: list[str]
    is_recurring: bool = True

    @field_validator("days_of_week")
    @classmethod
    def validate_days(cls, value: list[str]) -> list[str]:
        if not value or any(day not in WEEKDAYS for day in value):
            raise ValueError("days_of_week must contain valid lowercase weekday names")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_hours(self):
        try:
            start, end = time.fromisoformat(self.start_time), time.fromisoformat(self.end_time)
        except ValueError:
            raise ValueError("protected period times must be valid HH:MM values") from None
        if end <= start:
            raise ValueError("end_time must be after start_time")
        return self


class FacilityResourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    resource_type: str = Field(default="room", min_length=1, max_length=64)
