from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class AvailabilityRuleInput(BaseModel):
    facility_uuid: UUID
    practitioner_uuid: UUID
    weekday: int = Field(ge=0, le=6)
    start_time: time
    end_time: time
    slot_duration_minutes: int = Field(ge=5, le=240)
    valid_from: date
    valid_until: date | None = None
    resource_uuid: UUID | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from")
        return self


class AppointmentCreate(BaseModel):
    facility_uuid: UUID
    practitioner_uuid: UUID
    patient_uuid: UUID
    resource_uuid: UUID | None = None
    room_uuid: UUID | None = None
    scheduled_start: datetime
    scheduled_end: datetime
    reason_code: str | None = None
    reason_text: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def populate_resource_uuid(self):
        if self.resource_uuid is None and self.room_uuid is not None:
            self.resource_uuid = self.room_uuid
        return self


class AvailabilityExceptionInput(BaseModel):
    facility_uuid: UUID
    practitioner_uuid: UUID
    exception_date: date
    start_time: time | None = None
    end_time: time | None = None
    exception_type: str
    reason: str | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time and end_time must both be provided or both omitted")
        if self.start_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class AppointmentReschedule(BaseModel):
    scheduled_start: datetime
    scheduled_end: datetime
    resource_uuid: UUID | None = None
    room_uuid: UUID | None = None

    @model_validator(mode="after")
    def populate_resource_uuid(self):
        if self.resource_uuid is None and self.room_uuid is not None:
            self.resource_uuid = self.room_uuid
        return self


class ExceptionBookingCreate(AppointmentCreate):
    override_types: list[Literal["facility_closed", "practitioner_off_hours"]] = Field(min_length=1, max_length=2)
    reason: str = Field(min_length=1, max_length=2000)
    doctor_agreement_recorded: Literal[True]
    confirmation_acknowledged: bool | None = None
