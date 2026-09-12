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
