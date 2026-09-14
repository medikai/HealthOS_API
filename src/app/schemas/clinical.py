from typing import Any
from pydantic import BaseModel, Field



class SoapInput(BaseModel):
    subjective: str | None = None
    objective: str | None = None
    assessment: str | None = None
    plan: str | None = None


class DiagnosisInput(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    is_primary: bool = False


class PrescriptionItemInput(BaseModel):
    medicine_name: str = Field(min_length=1, max_length=255)
    dosage: str = Field(min_length=1, max_length=128)
    frequency: str = Field(min_length=1, max_length=128)
    duration: str = Field(min_length=1, max_length=128)


class PrescriptionInput(BaseModel):
    advice: str | None = None
    items: list[PrescriptionItemInput] = []


class CustomSectionConfig(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=255)
    group: str = Field(default="plan", max_length=50)
    helper_text: str | None = None
    required: bool = False
    expanded: bool = False


class DocumentationSettingInput(BaseModel):
    facility_uuid: str | None = None
    triage_expanded_default: bool = True
    vitals_config: dict[str, Any] = Field(default_factory=dict)
    soap_config: dict[str, Any] = Field(default_factory=dict)
    custom_sections: list[CustomSectionConfig] = Field(default_factory=list)


class DocumentationSettingResponse(BaseModel):
    id: str | None = None
    organization_id: str | None = None
    facility_id: str | None = None
    triage_expanded_default: bool = True
    vitals_config: dict[str, Any]
    soap_config: dict[str, Any]
    custom_sections: list[dict[str, Any]]
    active_modifications_count: int = 0
    summary: list[str] = []

