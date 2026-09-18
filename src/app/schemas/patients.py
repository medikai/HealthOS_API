from typing import Any

from pydantic import BaseModel, Field, model_validator


class PatientCreate(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    date_of_birth: str | None = None
    gender: str | None = None
    person: "PatientCreate | None" = None

    @model_validator(mode="before")
    @classmethod
    def accept_nested_person(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("person"), dict):
            return value["person"]
        return value

    def normalized(self) -> "PatientCreate":
        return self.person or self


class PatientUpdate(PatientCreate):
    pass
