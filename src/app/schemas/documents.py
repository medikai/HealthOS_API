from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PatientDocumentCategory = Literal[
    "lab_report",
    "imaging_report",
    "prescription",
    "discharge_summary",
    "other",
]
PatientDocumentMime = Literal["application/pdf", "image/jpeg", "image/png"]


class PatientDocumentUploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    content_type: PatientDocumentMime
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    category: PatientDocumentCategory
    title: str = Field(min_length=1, max_length=120)
    document_date: date

    @field_validator("filename", "title")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.lower()

