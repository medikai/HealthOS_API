import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from ..core.db.database import Base


class Organization(Base):
    __tablename__ = "organization"
    __table_args__ = {"schema": "organization"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    logto_organization_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Facility(Base):
    __tablename__ = "facility"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_facility_organization_code"),
        Index("ix_organization_facility_org_active", "organization_id", "is_active"),
        {"schema": "organization"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(String(64), index=True)
    classification: Mapped[str | None] = mapped_column(String(128), default=None)
    street_address: Mapped[str | None] = mapped_column(Text, default=None)
    country_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.country.id"), index=True, default=None)
    state_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.state.id"), index=True, default=None)
    district_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.district.id"), index=True, default=None)
    city_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.city.id"), index=True, default=None)
    postal_code: Mapped[str | None] = mapped_column(String(32), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class Department(Base):
    __tablename__ = "department"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_department_organization_code"),
        {"schema": "organization"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(String(64), index=True)
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("organization.facility.id"), index=True, default=None
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class StaffMember(Base):
    __tablename__ = "staff_member"
    __table_args__ = (UniqueConstraint("organization_id", "user_account_id", name="uq_staff_member_organization_user"), {"schema": "organization"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    user_account_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.user_account.id"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class StaffAssignment(Base):
    __tablename__ = "staff_assignment"
    __table_args__ = (
        UniqueConstraint("staff_member_id", "role_code", name="uq_staff_assignment_member_role"),
        Index("ix_organization_staff_assignment_facility_active", "facility_id", "is_active", "role_code"),
        {"schema": "organization"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.staff_member.id"), index=True)
    role_code: Mapped[str] = mapped_column(String(64), index=True)
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("organization.facility.id"), index=True, default=None
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class FacilitySchedule(Base):
    __tablename__ = "facility_schedule"
    __table_args__ = (UniqueConstraint("facility_id", name="uq_organization_facility_schedule"), {"schema": "organization"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    operating_start: Mapped[str] = mapped_column(String(5), default="08:00")
    operating_end: Mapped[str] = mapped_column(String(5), default="20:00")
    slot_interval_minutes: Mapped[int] = mapped_column(default=30)
    days_of_week: Mapped[str] = mapped_column(Text, default='["monday","tuesday","wednesday","thursday","friday","saturday"]')
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")


class FacilityResource(Base):
    __tablename__ = "facility_resource"
    __table_args__ = (
        UniqueConstraint("facility_id", "name", name="uq_organization_facility_resource"),
        {"schema": "organization"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    resource_type: Mapped[str] = mapped_column(String(64), default="room")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProtectedPeriod(Base):
    __tablename__ = "protected_period"
    __table_args__ = {"schema": "organization"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    start_time: Mapped[str] = mapped_column(String(5))
    end_time: Mapped[str] = mapped_column(String(5))
    period_type: Mapped[str] = mapped_column(String(64), default="protected")
    days_of_week: Mapped[str] = mapped_column(Text, default='["monday","tuesday","wednesday","thursday","friday","saturday"]')
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=True)


class StaffInvitation(Base):
    __tablename__ = "staff_invitation"
    __table_args__ = {"schema": "organization"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    role_code: Mapped[str] = mapped_column(String(64), index=True)
    token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("organization.facility.id"), index=True, default=None
    )
    specialty_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("platform.specialty.id"), index=True, default=None
    )
    sub_specialty_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("platform.sub_specialty.id"), index=True, default=None
    )
    designation_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("platform.staff_designation.id"), index=True, default=None
    )
    medical_council_reg_no: Mapped[str | None] = mapped_column(String(100), default=None)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
