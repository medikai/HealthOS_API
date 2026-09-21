import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from ..core.db.database import Base


class Specialty(Base):
    """Clinical Specialty Master (e.g. General Practice, Gynecology, Virology, Cardiology)."""

    __tablename__ = "specialty"
    __table_args__ = {"schema": "platform"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class SubSpecialty(Base):
    """Clinical Sub-Specialty / Scope Master (e.g. Clinical Virology, Maternal-Fetal Medicine)."""

    __tablename__ = "sub_specialty"
    __table_args__ = {"schema": "platform"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    code: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    specialty_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("platform.specialty.id"), index=True, default=None
    )
    description: Mapped[str | None] = mapped_column(Text, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class StaffDesignation(Base):
    """Staff & Specialist Designation / Title Master (e.g. Senior Gynecologist, Consultant Physician)."""

    __tablename__ = "staff_designation"
    __table_args__ = {"schema": "platform"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64), default="clinical")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class MedicalCouncil(Base):
    """Indian medical registration authority master."""

    __tablename__ = "medical_council"
    __table_args__ = {"schema": "platform"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    state_code: Mapped[str | None] = mapped_column(String(2), default=None)
    country_code: Mapped[str] = mapped_column(String(2), default="IN")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
