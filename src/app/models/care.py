import uuid as uuid_pkg
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from ..core.db.database import Base


class Practitioner(Base):
    __tablename__ = "practitioner"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    person_name: Mapped[str] = mapped_column(String(255))
    specialty: Mapped[str | None] = mapped_column(String(255), default=None)
    specialty_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.specialty.id"), index=True, default=None)
    sub_specialty_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.sub_specialty.id"), index=True, default=None)
    designation_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.staff_designation.id"), index=True, default=None)
    medical_council_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.medical_council.id"), index=True, default=None)
    medical_council_reg_no: Mapped[str | None] = mapped_column(String(100), default=None)
    prescription_authority_status: Mapped[str] = mapped_column(String(32), default="authorized")
    has_prescription_authority: Mapped[bool] = mapped_column(Boolean, default=True)
    user_account_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), unique=True, default=None, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))



class Appointment(Base):
    __tablename__ = "appointment"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_appointment_org_idempotency"),
        Index("ix_care_appointment_facility_status_start", "facility_id", "status", "scheduled_start"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    practitioner_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.practitioner.id"), index=True)
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resource_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("organization.facility_resource.id"), default=None, index=True)
    status: Mapped[str] = mapped_column(String(32), default="booked", index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), default=None)
    reason_text: Mapped[str | None] = mapped_column(Text, default=None)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), default=None)
    version: Mapped[int] = mapped_column(Integer, default=1)
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
    resource_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("organization.facility_resource.id"), default=None, index=True)
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
    exception_type: Mapped[str] = mapped_column(String(32))
    start_time: Mapped[time | None] = mapped_column(Time, default=None)
    end_time: Mapped[time | None] = mapped_column(Time, default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)


class AppointmentBookingException(Base):
    __tablename__ = "appointment_booking_exception"
    __table_args__ = (
        CheckConstraint("scheduled_end > scheduled_start", name="ck_booking_exception_time_range"),
        CheckConstraint("doctor_agreement_recorded", name="ck_booking_exception_doctor_agreement"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    appointment_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("care.appointment.id"), unique=True)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    practitioner_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.practitioner.id"), index=True)
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    override_types: Mapped[list[str]] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    doctor_agreement_recorded: Mapped[bool] = mapped_column(Boolean)
    actor_user_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.user_account.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class QueueCounter(Base):
    __tablename__ = "queue_counter"
    __table_args__ = (UniqueConstraint("facility_id", "queue_date", name="uq_care_queue_counter_facility_date"), {"schema": "care"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    queue_date: Mapped[date] = mapped_column(Date)
    last_token_number: Mapped[int] = mapped_column(Integer, default=0)


class QueueEntry(Base):
    __tablename__ = "queue_entry"
    __table_args__ = (
        Index("ix_care_queue_entry_facility_date_status", "facility_id", "queue_date", "status"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    queue_date: Mapped[date] = mapped_column(Date, index=True)
    token_number: Mapped[int] = mapped_column(Integer)
    appointment_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("care.appointment.id"), default=None, index=True)
    practitioner_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.practitioner.id"), default=None, index=True)
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
    recording_id: Mapped[uuid_pkg.UUID | None] = mapped_column(UUID(as_uuid=True), default=None, index=True)
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
    custom_fields: Mapped[dict[str, str | None]] = mapped_column(JSON, default_factory=dict)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    signed_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), default=None, index=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PatientDocument(Base):
    __tablename__ = "patient_document"
    __table_args__ = (
        CheckConstraint(
            "category IN ('lab_report', 'imaging_report', 'prescription', "
            "'discharge_summary', 'other')",
            name="ck_care_patient_document_category",
        ),
        CheckConstraint(
            "status IN ('uploading', 'processing', 'available', 'rejected')",
            name="ck_care_patient_document_status",
        ),
        Index(
            "ix_care_patient_document_patient_date",
            "patient_id",
            "document_date",
        ),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.organization.id"), index=True
    )
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("identity.patient.id"), index=True
    )
    encounter_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("care.encounter.id"), index=True
    )
    uploaded_by_user_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("identity.user_account.id"), index=True
    )
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(120))
    document_date: Mapped[date] = mapped_column(Date)
    original_filename: Mapped[str] = mapped_column(String(255))
    declared_mime_type: Mapped[str] = mapped_column(String(64))
    expected_size_bytes: Mapped[int] = mapped_column(Integer)
    expected_sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="uploading", index=True)
    mime_type: Mapped[str | None] = mapped_column(String(64), default=None)
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    storage_generation: Mapped[str | None] = mapped_column(String(64), default=None)
    rejection_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    scanned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    deleted_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("identity.user_account.id"), default=None
    )
    storage_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


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
    signed_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), default=None, index=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PrescriptionItem(Base):
    __tablename__ = "prescription_item"
    __table_args__ = {"schema": "care"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    prescription_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("care.prescription.id"), index=True)
    medicine_name: Mapped[str] = mapped_column(String(500))
    dosage: Mapped[str] = mapped_column(String(128))
    frequency: Mapped[str] = mapped_column(String(128))
    duration: Mapped[str] = mapped_column(String(128))
    medicine_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.medicine.id", ondelete="SET NULL"), default=None, index=True)
    strength: Mapped[str | None] = mapped_column(String(128), default=None)
    brand: Mapped[str | None] = mapped_column(String(500), default=None)
    route: Mapped[str | None] = mapped_column(String(64), default=None)
    timing: Mapped[str | None] = mapped_column(String(128), default=None)
    instructions: Mapped[str | None] = mapped_column(Text, default=None)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = {"schema": "governance"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("organization.facility.id"), default=None, index=True)
    actor_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), default=None, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), default=None)
    patient_id: Mapped[str | None] = mapped_column(String(128), default=None)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC), index=True)


class ClinicalDocumentationSetting(Base):
    __tablename__ = "clinical_documentation_setting"
    __table_args__ = (
        UniqueConstraint("organization_id", "facility_id", name="uq_care_doc_setting_org_facility"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("organization.facility.id"), nullable=True, default=None, index=True)
    triage_expanded_default: Mapped[bool] = mapped_column(Boolean, default=True)
    vitals_config: Mapped[dict[str, Any]] = mapped_column(JSON, default_factory=dict)
    soap_config: Mapped[dict[str, Any]] = mapped_column(JSON, default_factory=dict)
    custom_sections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default_factory=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PractitionerSchedule(Base):
    __tablename__ = "practitioner_schedule"
    __table_args__ = (
        Index("ix_care_practitioner_schedule_facility_practitioner_active", "facility_id", "practitioner_id", "is_active"),
        Index("ix_care_practitioner_schedule_org_practitioner", "organization_id", "practitioner_id"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.facility.id"), index=True)
    practitioner_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.practitioner.id"), index=True)
    effective_from: Mapped[date] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    slot_interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    effective_to: Mapped[date | None] = mapped_column(Date, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    weekly_hours: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default_factory=list)
    date_exceptions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default_factory=list)
    is_override: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.user_account.id"), default=None, index=True)
