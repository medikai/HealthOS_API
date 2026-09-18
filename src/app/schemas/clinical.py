from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .common import SuccessResponse


class SoapInput(BaseModel):
    subjective: str | None = None
    objective: str | None = None
    assessment: str | None = None
    plan: str | None = None
    custom_fields: dict[str, str | None] | None = None


class SoapNoteRead(BaseModel):
    uuid: UUID | None
    encounter_uuid: UUID
    subjective: str
    objective: str
    assessment: str
    plan: str
    custom_fields: dict[str, str | None]
    status: str
    signed_at: datetime | None


class SoapNoteResponse(SuccessResponse[SoapNoteRead]):
    pass


class DiagnosisInput(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    is_primary: bool = False


class PrescriptionItemInput(BaseModel):
    medicine_name: str = Field(
        min_length=1,
        max_length=255,
        validation_alias=AliasChoices("medicine_name", "name"),
    )
    dosage: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("dosage", "dose"),
    )
    frequency: str = Field(min_length=1, max_length=128)
    duration: str = Field(min_length=1, max_length=128)
    strength: str | None = Field(default=None, max_length=128)
    brand: str | None = Field(default=None, max_length=255)
    route: str | None = Field(default=None, max_length=64)
    timing: str | None = Field(default=None, max_length=128)
    instructions: str | None = None


class PrescriptionInput(BaseModel):
    advice: str | None = None
    status: Literal["draft", "signed"] = "draft"
    items: list[PrescriptionItemInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("items", "medications"),
    )


class VitalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool = True
    label: str | None = Field(default=None, min_length=1, max_length=255)
    unit: str | None = Field(default=None, max_length=32)


class SoapFieldConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool = True
    mandatory: bool = False
    default_state: Literal["expanded", "collapsed"] = "expanded"

    @model_validator(mode="after")
    def hidden_fields_cannot_be_mandatory(self):
        if not self.visible and self.mandatory:
            raise ValueError("a hidden SOAP field cannot be mandatory")
        return self


class CustomSectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=255)
    group: Literal["subjective", "objective", "assessment", "plan"] = "plan"
    helper_text: str | None = Field(default=None, max_length=1000)
    input_type: Literal["multiline_text"] = "multiline_text"
    visible: bool = True
    required: bool = False
    expanded: bool = False

    @model_validator(mode="after")
    def hidden_sections_cannot_be_required(self):
        if not self.visible and self.required:
            raise ValueError("a hidden custom section cannot be required")
        return self


class DocumentationSettingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facility_uuid: UUID | Literal["all"] | None = None
    triage_expanded_default: bool = True
    vitals_config: dict[
        Literal[
            "blood_pressure",
            "pulse_rate",
            "spo2",
            "respiratory_rate",
            "temperature",
            "height",
            "weight",
            "bmi",
        ],
        VitalConfig,
    ] = Field(default_factory=dict)
    soap_config: dict[
        Literal["subjective", "objective", "assessment", "plan"],
        dict[str, SoapFieldConfig],
    ] = Field(default_factory=dict)
    custom_sections: list[CustomSectionConfig] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_configuration(self):
        allowed_fields = {
            "subjective": {"general_notes", "chief_complaints", "hpi", "family_social"},
            "objective": {"general_exam", "systemic_exam", "additional_obs"},
            "assessment": {"diagnoses"},
            "plan": {"general_plan", "prescription_summary"},
        }
        for group, fields in self.soap_config.items():
            unsupported = set(fields) - allowed_fields[group]
            if unsupported:
                raise ValueError(f"unsupported {group} SOAP fields: {', '.join(sorted(unsupported))}")

        keys = [section.key for section in self.custom_sections]
        if len(keys) != len(set(keys)):
            raise ValueError("custom section keys must be unique")
        reserved = set().union(*allowed_fields.values())
        conflicts = set(keys) & reserved
        if conflicts:
            raise ValueError(f"custom section keys are reserved: {', '.join(sorted(conflicts))}")

        bmi = self.vitals_config.get("bmi")
        if bmi and bmi.visible:
            for dependency in ("height", "weight"):
                config = self.vitals_config.get(dependency)
                if config is not None and not config.visible:
                    raise ValueError("visible BMI requires visible height and weight")
        return self


class DocumentationSettingResponse(BaseModel):
    id: str | None = None
    organization_id: str
    facility_id: str | None = None
    source: Literal["facility", "organization", "default"]
    triage_expanded_default: bool = True
    vitals_config: dict[str, Any]
    soap_config: dict[str, Any]
    custom_sections: list[dict[str, Any]]
    active_modifications_count: int = 0
    summary: list[str] = Field(default_factory=list)


class DocumentationSettingsResponse(SuccessResponse[DocumentationSettingResponse]):
    pass
