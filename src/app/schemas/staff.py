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
