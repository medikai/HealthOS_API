import uuid as uuid_pkg
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
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


class Country(Base):
    __tablename__ = "country"
    __table_args__ = {"schema": "platform"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    code: Mapped[str] = mapped_column(String(2), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    iso3_code: Mapped[str | None] = mapped_column(String(3), unique=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class State(Base):
    __tablename__ = "state"
    __table_args__ = (
        UniqueConstraint("country_id", "code", name="uq_platform_state_country_code"),
        {"schema": "platform"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    country_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("platform.country.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    external_code: Mapped[str | None] = mapped_column(String(64), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class District(Base):
    __tablename__ = "district"
    __table_args__ = (
        UniqueConstraint("state_id", "code", name="uq_platform_district_state_code"),
        {"schema": "platform"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    state_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("platform.state.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class City(Base):
    __tablename__ = "city"
    __table_args__ = (
        UniqueConstraint("district_id", "normalized_name", name="uq_platform_city_district_name"),
        {"schema": "platform"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    state_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("platform.state.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))
    district_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.district.id"), index=True, default=None)
    external_code: Mapped[str | None] = mapped_column(String(64), default=None)
    is_user_added: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class Medicine(Base):
    __tablename__ = "medicine"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_platform_medicine_source_id"),
        {"schema": "platform"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    name: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(64), default="manual")
    source_id: Mapped[str | None] = mapped_column(String(128), default=None)
    manufacturer_name: Mapped[str | None] = mapped_column(String(500), default=None)
    medicine_type: Mapped[str | None] = mapped_column(String(64), default=None)
    pack_size_label: Mapped[str | None] = mapped_column(String(255), default=None)
    composition1: Mapped[str | None] = mapped_column(String(500), default=None)
    composition2: Mapped[str | None] = mapped_column(String(500), default=None)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=None)
    is_discontinued: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PrescriptionOption(Base):
    __tablename__ = "prescription_option"
    __table_args__ = (
        CheckConstraint(
            "category IN ('dosage', 'route', 'frequency', 'timing', 'duration', 'instructions')",
            name="ck_platform_prescription_option_category",
        ),
        UniqueConstraint(
            "country_code", "category", "code",
            name="uq_platform_prescription_option_country_category_code",
        ),
        {"schema": "platform"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    category: Mapped[str] = mapped_column(String(32), index=True)
    code: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(String(255))
    country_code: Mapped[str] = mapped_column(String(2), default="IN")
    description: Mapped[str | None] = mapped_column(String(255), default=None)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
