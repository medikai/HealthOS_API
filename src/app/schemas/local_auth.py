from pydantic import BaseModel, EmailStr, Field


class LocalRegisterPayload(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(min_length=6)
    phone: str | None = None
    specialty: str | None = None


class LocalLoginPayload(BaseModel):
    email: EmailStr
    password: str

