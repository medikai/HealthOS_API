from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class PatientCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    date_of_birth: str | None = None
    gender: str | None = None
    abha_number: str | None = Field(default=None, max_length=32)
    abha_address: str | None = Field(default=None, max_length=64)
    duplicate_override_reason: str | None = Field(default=None, max_length=500)
    salutation_id: UUID | None = None
    person: "PatientCreate | None" = None

    @model_validator(mode="before")
    @classmethod
    def accept_nested_person(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("person"), dict):
            merged = dict(value["person"])
            if value.get("duplicate_override_reason") and not merged.get("duplicate_override_reason"):
                merged["duplicate_override_reason"] = value["duplicate_override_reason"]
            return merged
        return value

    def normalized(self) -> "PatientCreate":
        return self.person or self


class PatientUpdate(PatientCreate):
    pass


class PatientViewRequest(BaseModel):
    facility_uuid: UUID | None = None
