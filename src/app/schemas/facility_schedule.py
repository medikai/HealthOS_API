from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator


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


class ProtectedPeriodInput(BaseModel):
    title: str
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    period_type: str = "protected"
    days_of_week: list[str]
    is_recurring: bool = True
