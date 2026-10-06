from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class AppointmentRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_uuid: UUID
    facility_uuid: UUID
    practitioner_uuid: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    reason_code: str | None = Field(default=None, max_length=64)
    reason_text: str | None = Field(default=None, max_length=2000)


class RescheduleRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    facility_uuid: UUID | None = None
    scheduled_start: datetime
    scheduled_end: datetime
    version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class CancelRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class AlternativeResponseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept: bool


class AppointmentStatusQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appointment_uuids: list[UUID] = Field(default_factory=list, max_length=50)
    request_uuids: list[UUID] = Field(default_factory=list, max_length=50)


class StaffAlternativeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scheduled_start: datetime
    scheduled_end: datetime
    resource_uuid: UUID | None = None
    note: str | None = Field(default=None, max_length=500)


class StaffDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class RecordReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_type: Literal["encounter_summary", "vitals", "prescription", "patient_document"]
    resource_uuid: UUID
    summary: str | None = Field(default=None, max_length=4000)
    include_vitals: bool = False

    def require_summary(self) -> None:
        if self.resource_type == "encounter_summary" and not (self.summary or "").strip():
            raise ValueError("summary is required for encounter_summary releases.")


class RecordReleaseRevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class PreferenceUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_app: bool | None = None
    push: bool | None = None


class PushDeviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_id: str = Field(min_length=8, max_length=128)
    token: str = Field(min_length=16, max_length=512)
    platform: Literal["web", "android", "ios"] = "web"


class InvoiceDiscountBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["percentage", "fixed"]
    value_bp: int | None = Field(
        default=None,
        validation_alias=AliasChoices("value_bp", "basis_points"),
        ge=1,
        le=10000,
    )
    amount_minor: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=500)


class ClinicPortalPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    portal_auto_confirm: bool
