from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

from .common import SuccessResponse


class EncounterRead(BaseModel):
    uuid: UUID
    facility_uuid: UUID
    patient_uuid: UUID
    practitioner_uuid: UUID | None
    appointment_uuid: UUID | None
    queue_entry_uuid: UUID | None
    status: str
    started_at: datetime
    completed_at: datetime | None


class StartConsultationData(BaseModel):
    status: Literal["in_consultation"]
    encounter: EncounterRead


class StartConsultationResponse(SuccessResponse[StartConsultationData]):
    pass


class FacilitySummary(BaseModel):
    uuid: UUID
    name: str


class PatientSummary(BaseModel):
    uuid: UUID
    display_name: str
    mrn: str | None
    gender: str | None
    date_of_birth: date | None


class PractitionerSummary(BaseModel):
    uuid: UUID
    name: str
    specialty: str | None


class EncounterDetail(EncounterRead):
    token_label: str | None
    facility: FacilitySummary
    patient: PatientSummary
    practitioner: PractitionerSummary


class EncounterDetailResponse(SuccessResponse[EncounterDetail]):
    pass


class VitalSignRecord(BaseModel):
    uuid: UUID
    systolic_bp_mmhg: int | None = None
    diastolic_bp_mmhg: int | None = None
    pulse_bpm: int | None = None
    respiratory_rate_per_min: int | None = None
    spo2_percent: float | None = None
    temperature_c: float | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    recorded_at: datetime
    name: str | None = None
    value: str | None = None
    unit: str | None = None


class VitalsCollection(BaseModel):
    items: list[VitalSignRecord]


class VitalsResponse(SuccessResponse[VitalsCollection]):
    pass


class VitalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=64)
    unit: str | None = Field(default=None, max_length=32)


class VitalSetCreate(BaseModel):
    systolic_bp_mmhg: int | None = Field(
        default=None, validation_alias=AliasChoices("systolic_bp_mmhg", "systolic")
    )
    diastolic_bp_mmhg: int | None = Field(
        default=None, validation_alias=AliasChoices("diastolic_bp_mmhg", "diastolic")
    )
    pulse_bpm: int | None = Field(
        default=None, validation_alias=AliasChoices("pulse_bpm", "heart_rate")
    )
    respiratory_rate_per_min: int | None = Field(
        default=None,
        validation_alias=AliasChoices("respiratory_rate_per_min", "respiratory_rate"),
    )
    spo2_percent: float | None = Field(
        default=None, validation_alias=AliasChoices("spo2_percent", "spo2")
    )
    temperature_c: float | None = None
    height_cm: float | None = None
    weight_kg: float | None = None

    @field_validator("*", mode="before")
    @classmethod
    def empty_string_is_missing(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def require_measurement(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("At least one vital measurement is required.")
        return self


VitalInput = VitalCreate | VitalSetCreate
