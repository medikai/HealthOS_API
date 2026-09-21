from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class LocalRegisterPayload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=6)
    phone: str | None = None
    specialty: str | None = None
    specialty_id: UUID | None = None
    medical_council_id: UUID | None = None
    medical_council_reg_no: str | None = Field(default=None, max_length=100)


class LocalLoginPayload(BaseModel):
    email: EmailStr
    password: str
