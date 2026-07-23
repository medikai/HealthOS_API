import uuid

from pydantic import BaseModel, ConfigDict, Field


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=255)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")


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


class FacilityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=255)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")


class DepartmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=255)
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_]+$")
    facility_id: uuid.UUID | None = None
