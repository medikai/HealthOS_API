import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FacilityAddress(BaseModel):
    clinic_name: str | None = Field(default=None, min_length=2, max_length=255)
    classification: str | None = Field(default=None, max_length=128)
    street_address: str | None = Field(default=None, max_length=2000)
    country_id: uuid.UUID | None = None
    state_id: uuid.UUID | None = None
    district_id: uuid.UUID | None = None
    city_id: uuid.UUID | None = None
    postal_code: str | None = Field(default=None, max_length=32)
    phone: str | None = Field(default=None, max_length=32)


class OrganizationCreate(FacilityAddress):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=255)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")
    specialty_id: uuid.UUID | None = None
    medical_council_id: uuid.UUID | None = None
    medical_council_reg_no: str | None = Field(default=None, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def ignore_legacy_timezone(cls, value: object) -> object:
        return {key: item for key, item in value.items() if key != "timezone"} if isinstance(value, dict) else value


class StaffRoleAssign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    logto_user_id: str = Field(min_length=1, max_length=255)
    role_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z_]+$")


class OrganizationFeatureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=2, max_length=128, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=5000)


class OrganizationRead(BaseModel):
    id: uuid.UUID
    name: str
    code: str


class FacilityCreate(FacilityAddress):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=255)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")
    timezone: str = "Asia/Kolkata"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("timezone must be a valid IANA timezone") from None
        return value


class DepartmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=255)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")
    facility_id: uuid.UUID | None = None
