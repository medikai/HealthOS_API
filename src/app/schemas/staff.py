from uuid import UUID
from pydantic import BaseModel


class StaffAssignmentsInput(BaseModel):
    assignments: list[UUID]


class StaffRolesInput(BaseModel):
    role_keys: list[str]


class StaffCreate(BaseModel):
    user_account_uuid: UUID
    role_code: str = "facility_operator"
    facility_uuids: list[UUID] = []


class StaffUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None


class StaffInviteCreate(BaseModel):
    email: str
    full_name: str
    role_code: str = "practitioner"
    facility_uuid: UUID | None = None
    specialty: str | None = None
    specialty_id: UUID | None = None
    sub_specialty_id: UUID | None = None
    designation_id: UUID | None = None
    medical_council_reg_no: str | None = None
    has_prescription_authority: bool = True



class StaffInviteAccept(BaseModel):
    token: str
    password: str
    full_name: str | None = None

