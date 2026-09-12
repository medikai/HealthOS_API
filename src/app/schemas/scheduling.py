from datetime import date, datetime, time
from uuid import UUID
from pydantic import BaseModel, Field


class AvailabilityRuleInput(BaseModel):
    facility_uuid: UUID
    practitioner_uuid: UUID
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    slot_duration_minutes: int = Field(ge=5, le=240)
    valid_from: date
    valid_until: date | None = None


class AppointmentCreate(BaseModel):
    facility_uuid: UUID
    practitioner_uuid: UUID
    patient_uuid: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    reason_code: str | None = None
    reason_text: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)


class AvailabilityExceptionInput(BaseModel):
    facility_uuid: UUID
    practitioner_uuid: UUID
    exception_date: date
    start_time: time | None = None
    end_time: time | None = None
    exception_type: str
    reason: str | None = None


class AppointmentReschedule(BaseModel):
    scheduled_start: datetime
    scheduled_end: datetime
