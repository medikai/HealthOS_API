from uuid import UUID
from pydantic import BaseModel


class WalkInCreate(BaseModel):
    facility_uuid: UUID
    patient_uuid: UUID
    practitioner_uuid: UUID | None = None
    reason_code: str | None = None
    reason_text: str | None = None
