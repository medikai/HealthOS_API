import uuid as uuid_pkg
from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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


class ConsultationFee(Base):
    __tablename__ = "consultation_fee"
    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="ck_consultation_fee_nonnegative"),
        CheckConstraint(
            "char_length(currency) = 3", name="ck_consultation_fee_currency"
        ),
        CheckConstraint(
            "(scope_type = 'facility' AND practitioner_id IS NULL AND specialty_id IS NULL) OR "
            "(scope_type = 'specialty' AND practitioner_id IS NULL AND specialty_id IS NOT NULL) OR "
            "(scope_type = 'practitioner' AND practitioner_id IS NOT NULL AND specialty_id IS NULL)",
            name="ck_consultation_fee_scope",
        ),
        Index(
            "ix_care_consultation_fee_resolution",
            "facility_id",
            "scope_type",
            "effective_from",
            "effective_to",
        ),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.organization.id"), index=True
    )
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.facility.id"), index=True
    )
    scope_type: Mapped[str] = mapped_column(String(24))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    effective_from: Mapped[date] = mapped_column(Date)
    created_by_user_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("identity.user_account.id"), index=True
    )
    practitioner_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("identity.practitioner.id"), index=True, default=None
    )
    specialty_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("platform.specialty.id"), index=True, default=None
    )
    effective_to: Mapped[date | None] = mapped_column(Date, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class Invoice(Base):
    __tablename__ = "invoice"
    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="ck_invoice_nonnegative"),
        CheckConstraint("char_length(currency) = 3", name="ck_invoice_currency"),
        CheckConstraint(
            "status IN ('issued', 'partially_paid', 'paid', 'voided')",
            name="ck_invoice_status",
        ),
        UniqueConstraint("encounter_id", name="uq_care_invoice_encounter"),
        Index("ix_care_invoice_facility_issued", "facility_id", "issued_at"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.organization.id"), index=True
    )
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.facility.id"), index=True
    )
    encounter_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("care.encounter.id"), index=True
    )
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("identity.patient.id"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    created_by_user_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("identity.user_account.id"), index=True
    )
    practitioner_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("identity.practitioner.id"), index=True, default=None
    )
    consultation_fee_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("care.consultation_fee.id"), default=None
    )
    status: Mapped[str] = mapped_column(String(24), default="issued", index=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    voided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class Payment(Base):
    __tablename__ = "payment"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_payment_positive"),
        CheckConstraint("char_length(currency) = 3", name="ck_payment_currency"),
        CheckConstraint("status IN ('captured', 'voided')", name="ck_payment_status"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_care_payment_org_idempotency"
        ),
        Index("ix_care_payment_facility_received", "facility_id", "received_at"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.organization.id"), index=True
    )
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.facility.id"), index=True
    )
    invoice_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("care.invoice.id"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_by_user_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("identity.user_account.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="captured", index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    voided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class Refund(Base):
    __tablename__ = "refund"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_refund_positive"),
        CheckConstraint("char_length(currency) = 3", name="ck_refund_currency"),
        CheckConstraint("status IN ('completed', 'voided')", name="ck_refund_status"),
        UniqueConstraint(
            "organization_id", "idempotency_key", name="uq_care_refund_org_idempotency"
        ),
        Index("ix_care_refund_facility_refunded", "facility_id", "refunded_at"),
        {"schema": "care"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.organization.id"), index=True
    )
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.facility.id"), index=True
    )
    invoice_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("care.invoice.id"), index=True
    )
    payment_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("care.payment.id"), index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_by_user_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("identity.user_account.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="completed", index=True)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    refunded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    voided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
