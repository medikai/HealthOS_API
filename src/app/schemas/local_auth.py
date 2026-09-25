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


class ForgotPasswordPayload(BaseModel):
    email: EmailStr


class VerifyRecoveryCodePayload(BaseModel):
    challenge_id: UUID
    code: str = Field(min_length=4, max_length=12)


class ResetPasswordPayload(BaseModel):
    grant: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=6, max_length=128)
