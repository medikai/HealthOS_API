from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
    medical_council_id: UUID | None = None
    medical_council_reg_no: str | None = None
    has_prescription_authority: bool = True
    prescription_authority_status: str = "authorized"
    user_account_id: UUID | None = None
    facility_ids: list[UUID] = Field(default_factory=list)


class PractitionerUpdate(BaseModel):
    person_name: str | None = None
    specialty: str | None = None
    specialty_id: UUID | None = None
    sub_specialty_id: UUID | None = None
    designation_id: UUID | None = None
    medical_council_id: UUID | None = None
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
    medical_council_id: UUID | None = None
    medical_council_name: str | None = None
    medical_council_reg_no: str | None = None
    has_prescription_authority: bool = True
    prescription_authority_status: str = "authorized"
    organization_uuid: UUID | None = None
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)


class CityCreate(BaseModel):
    state_id: UUID
    district_id: UUID | None = None
    name: str = Field(min_length=1, max_length=255)


class MedicineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    manufacturer_name: str | None = Field(default=None, max_length=500)
    medicine_type: str | None = Field(default=None, max_length=64)
    pack_size_label: str | None = Field(default=None, max_length=255)
    composition1: str | None = Field(default=None, max_length=500)
    composition2: str | None = Field(default=None, max_length=500)
    price: Decimal | None = Field(default=None, ge=0)
    is_discontinued: bool = False
    is_active: bool = True


class MedicineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    manufacturer_name: str | None = Field(default=None, max_length=500)
    medicine_type: str | None = Field(default=None, max_length=64)
    pack_size_label: str | None = Field(default=None, max_length=255)
    composition1: str | None = Field(default=None, max_length=500)
    composition2: str | None = Field(default=None, max_length=500)
    price: Decimal | None = Field(default=None, ge=0)
    is_discontinued: bool | None = None
    is_active: bool | None = None


PrescriptionOptionCategory = Literal["dosage", "route", "frequency", "timing", "duration", "instructions"]


class PrescriptionOptionCreate(BaseModel):
    country_code: str = Field(default="IN", min_length=2, max_length=2)
    category: PrescriptionOptionCategory
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)
    sort_order: int = Field(default=0, ge=0)
    is_active: bool = True


class PrescriptionOptionUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    value: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)
    sort_order: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
