from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict


class SpecialtyBase(BaseModel):
    code: str
    name: str
    description: str | None = None
    is_active: bool = True


class SpecialtyCreate(SpecialtyBase):
    pass


class SpecialtyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class SpecialtyRead(SpecialtyBase):
    uuid: UUID
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SubSpecialtyBase(BaseModel):
    code: str
    name: str
    specialty_id: UUID | None = None
    description: str | None = None
    is_active: bool = True


class SubSpecialtyCreate(SubSpecialtyBase):
    pass


class SubSpecialtyUpdate(BaseModel):
    name: str | None = None
    specialty_id: UUID | None = None
    description: str | None = None
    is_active: bool | None = None


class SubSpecialtyRead(SubSpecialtyBase):
    uuid: UUID
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class StaffDesignationBase(BaseModel):
    code: str
    name: str
    category: str | None = "clinical"
    is_active: bool = True


class StaffDesignationCreate(StaffDesignationBase):
    pass


class StaffDesignationUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    is_active: bool | None = None


class StaffDesignationRead(StaffDesignationBase):
    uuid: UUID
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PractitionerCreate(BaseModel):
    person_name: str
    organization_id: UUID | None = None
    specialty: str | None = None
    specialty_id: UUID | None = None
    sub_specialty_id: UUID | None = None
    designation_id: UUID | None = None
    medical_council_reg_no: str | None = None
    has_prescription_authority: bool = True
    prescription_authority_status: str = "authorized"
    user_account_id: UUID | None = None


class PractitionerUpdate(BaseModel):
    person_name: str | None = None
    specialty: str | None = None
    specialty_id: UUID | None = None
    sub_specialty_id: UUID | None = None
    designation_id: UUID | None = None
    medical_council_reg_no: str | None = None
    has_prescription_authority: bool | None = None
    prescription_authority_status: str | None = None
    is_active: bool | None = None


class PractitionerRead(BaseModel):
    uuid: UUID
    name: str
    specialty: str | None = None
    specialty_id: UUID | None = None
    specialty_name: str | None = None
    sub_specialty_id: UUID | None = None
    sub_specialty_name: str | None = None
    designation_id: UUID | None = None
    designation_name: str | None = None
    medical_council_reg_no: str | None = None
    has_prescription_authority: bool = True
    prescription_authority_status: str = "authorized"
    organization_uuid: UUID | None = None
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)
