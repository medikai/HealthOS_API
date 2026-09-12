import uuid as uuid_pkg
from datetime import UTC, date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, Time, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from ..core.db.database import Base


class Practitioner(Base):
    __tablename__ = "practitioner"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    user_account_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), unique=True, default=None, index=True)
    person_name: Mapped[str] = mapped_column(String(255))
    specialty: Mapped[str | None] = mapped_column(String(255), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class Appointment(Base):
    __tablename__ = "appointment"
    __table_args__ = (UniqueConstraint("organization_id", "idempotency_key", name="uq_appointment_org_idempotency"), {"schema": "care"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    practitioner_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.practitioner.id"), index=True)
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="booked", index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), default=None)
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class PractitionerAvailabilityRule(Base):
    __tablename__ = "practitioner_availability_rule"
    __table_args__ = (UniqueConstraint("facility_id", "practitioner_id", "weekday", "start_time", name="uq_care_availability_rule"), {"schema": "care"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    practitioner_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.practitioner.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    slot_duration_minutes: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date, default=None)
    status: Mapped[str] = mapped_column(String(32), default="active")


class PractitionerAvailabilityException(Base):
    __tablename__ = "practitioner_availability_exception"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    practitioner_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.practitioner.id"), index=True)
    exception_date: Mapped[date] = mapped_column(Date, index=True)
    start_time: Mapped[time | None] = mapped_column(Time, default=None)
    end_time: Mapped[time | None] = mapped_column(Time, default=None)
    exception_type: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, default=None)


class QueueCounter(Base):
    __tablename__ = "queue_counter"
    __table_args__ = (UniqueConstraint("facility_id", "queue_date", name="uq_care_queue_counter_facility_date"), {"schema": "care"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    queue_date: Mapped[date] = mapped_column(Date)
    last_token_number: Mapped[int] = mapped_column(Integer, default=0)


class QueueEntry(Base):
    __tablename__ = "queue_entry"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    appointment_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("care.appointment.id"), default=None, index=True)
    practitioner_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.practitioner.id"), default=None, index=True)
    queue_date: Mapped[date] = mapped_column(Date, index=True)
    token_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), default=None)
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    skip_count: Mapped[int] = mapped_column(Integer, default=0)


class Encounter(Base):
    __tablename__ = "encounter"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    practitioner_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.practitioner.id"), default=None, index=True)
    appointment_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("care.appointment.id"), default=None, unique=True)
    queue_entry_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("care.queue_entry.id"), default=None, unique=True)
    status: Mapped[str] = mapped_column(String(32), default="in_progress", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Vital(Base):
    __tablename__ = "vital"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    encounter_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("care.encounter.id"), index=True)
    recorded_by_user_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.user_account.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(32), default=None)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class SoapNote(Base):
    __tablename__ = "soap_note"
    __table_args__ = (UniqueConstraint("encounter_id", name="uq_care_soap_note_encounter"), {"schema": "care"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    encounter_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("care.encounter.id"), index=True)
    subjective: Mapped[str | None] = mapped_column(Text, default=None)
    objective: Mapped[str | None] = mapped_column(Text, default=None)
    assessment: Mapped[str | None] = mapped_column(Text, default=None)
    plan: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    signed_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), default=None)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Diagnosis(Base):
    __tablename__ = "diagnosis"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    encounter_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("care.encounter.id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


class Prescription(Base):
    __tablename__ = "prescription"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    encounter_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("care.encounter.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    advice: Mapped[str | None] = mapped_column(Text, default=None)
    signed_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), default=None)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PrescriptionItem(Base):
    __tablename__ = "prescription_item"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    prescription_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("care.prescription.id"), index=True)
    medicine_name: Mapped[str] = mapped_column(String(255))
    dosage: Mapped[str] = mapped_column(String(128))
    frequency: Mapped[str] = mapped_column(String(128))
    duration: Mapped[str] = mapped_column(String(128))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = {"schema": "governance"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("organization.facility.id"), default=None, index=True)
    actor_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), default=None, index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128), default=None)
    patient_id: Mapped[str | None] = mapped_column(String(128), default=None)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC), index=True)
