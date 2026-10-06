from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class PatientOtpRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=8, max_length=24)
    # Explicit local-dev opt-in; ignored unless the environment and phone allowlist permit it.
    sandbox: bool = False


class PatientOtpResendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: UUID
    phone: str = Field(min_length=8, max_length=24)
    sandbox: bool = False


class PatientOtpVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(min_length=8, max_length=24)
    code: str = Field(min_length=4, max_length=12)
    challenge_id: UUID = Field(
        validation_alias=AliasChoices("challenge_id", "request_id")
    )


class PatientProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    date_of_birth: str | None = Field(default=None, max_length=10)
    email: str | None = Field(default=None, max_length=320)

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("date_of_birth must be an ISO date (YYYY-MM-DD).") from None
        if parsed > datetime.now(UTC).date():
            raise ValueError("date_of_birth cannot be in the future.")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if "@" not in cleaned or len(cleaned) < 5:
            raise ValueError("email is not valid.")
        return cleaned


class PatientPhoneChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_phone: str = Field(min_length=8, max_length=24)


class PatientPhoneChangeVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_phone: str = Field(min_length=8, max_length=24)
    code: str = Field(min_length=4, max_length=12)
    challenge_id: UUID


class PatientLinkActivationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Annotated[str, Field(min_length=16, max_length=256)]


class PatientLinkRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_uuid: UUID
    facility_uuid: UUID | None = None
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    date_of_birth: str | None = Field(default=None, max_length=10)

    @field_validator("date_of_birth")
    @classmethod
    def validate_date_of_birth(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("date_of_birth must be an ISO date (YYYY-MM-DD).") from None
        if parsed > datetime.now(UTC).date():
            raise ValueError("date_of_birth cannot be in the future.")
        return value


class StaffPatientInvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_uuid: UUID
    phone: str | None = Field(default=None, min_length=8, max_length=24)
    facility_uuid: UUID | None = None
    expires_in_hours: int | None = Field(default=None, ge=1, le=720)


class StaffPatientLinkApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_uuid: UUID
    facility_uuid: UUID | None = None
    review_reason: str | None = Field(default=None, max_length=255)


class StaffPatientLinkReject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=255)


class StaffPatientLinkRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=255)
