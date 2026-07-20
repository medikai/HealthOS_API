import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


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
    __table_args__ = (UniqueConstraint("staff_member_id", "role_code", name="uq_staff_assignment_member_role"), {"schema": "organization"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.staff_member.id"), index=True)
    role_code: Mapped[str] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
