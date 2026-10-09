import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from ..core.db.database import Base


class Salutation(Base):
    """Person salutation master (e.g. Doctor, Mister, Ms).

    Salutation is person identity, independent of profession, designation,
    RBAC role or specialty.
    """

    __tablename__ = "salutation"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    abbreviation: Mapped[str] = mapped_column(String(16))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class UserAccount(Base):
    """MedikAI-owned profile mapped to an immutable Logto subject."""

    __tablename__ = "user_account"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    logto_user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(2_048), default=None)
    password_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    # Bumped on password reset to invalidate already-issued local access tokens.
    credentials_version: Mapped[int] = mapped_column(Integer, default=1)
    registration_specialty: Mapped[str | None] = mapped_column(String(255), default=None)
    registration_specialty_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.specialty.id"), default=None)
    registration_medical_council_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("platform.medical_council.id"), default=None)
    registration_medical_council_reg_no: Mapped[str | None] = mapped_column(String(100), default=None)
    person_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.person.id"), index=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class LoginTransaction(Base):
    """Short-lived, single-use OAuth authorization-code transaction."""

    __tablename__ = "login_transaction"
    __table_args__ = {"schema": "identity"}

    state: Mapped[str] = mapped_column(String(255), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(255))
    code_verifier: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class AuthSession(Base):
    """Server-side BFF session. The browser receives only the opaque session ID."""

    __tablename__ = "auth_session"
    __table_args__ = {"schema": "identity"}

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_account_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"), index=True
    )
    id_token: Mapped[str] = mapped_column(Text)
    csrf_token: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class Person(Base):
    __tablename__ = "person"
    __table_args__ = (UniqueConstraint("organization_id", "phone", name="uq_identity_person_org_phone"), {"schema": "identity"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    date_of_birth: Mapped[str | None] = mapped_column(String(10), default=None)
    gender: Mapped[str | None] = mapped_column(String(32), default=None)
    salutation_id: Mapped[uuid_pkg.UUID | None] = mapped_column(ForeignKey("identity.salutation.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Patient(Base):
    __tablename__ = "patient"
    __table_args__ = (UniqueConstraint("organization_id", "mrn", name="uq_identity_patient_org_mrn"), {"schema": "identity"})

    id: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False)
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("organization.organization.id"), index=True)
    person_id: Mapped[uuid_pkg.UUID] = mapped_column(ForeignKey("identity.person.id"), unique=True, index=True)
    mrn: Mapped[str] = mapped_column(String(32), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default_factory=lambda: datetime.now(UTC))


class PasswordRecoveryChallenge(Base):
    """Local password-recovery challenge (auth-owned).

    The OTP is never stored in plain text: ``code_verifier`` is a keyed HMAC
    over purpose/account/challenge/code. Challenges are single-use and
    generation-bound; a resend consumes prior challenges.
    """

    __tablename__ = "password_recovery_challenge"
    __table_args__ = (
        Index("ix_identity_recovery_challenge_account", "user_account_id", "created_at"),
        Index("ix_identity_recovery_challenge_expires", "expires_at"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    user_account_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"), index=True
    )
    email_hash: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str] = mapped_column(String(32), default="password_reset")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    ip_hash: Mapped[str | None] = mapped_column(String(128), default=None)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class PasswordRecoveryGrant(Base):
    """Single-use, short-lived reset grant issued only after OTP verification."""

    __tablename__ = "password_recovery_grant"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_identity_recovery_grant_token"),
        Index("ix_identity_recovery_grant_expires", "expires_at"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    challenge_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.password_recovery_challenge.id", ondelete="CASCADE"), index=True
    )
    user_account_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class PasswordRecoveryThrottle(Base):
    """Aggregate per-account / per-IP recovery request throttling (no plaintext key)."""

    __tablename__ = "password_recovery_throttle"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", name="uq_identity_recovery_throttle"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    scope: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(128))
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class PatientPortalAccount(Base):
    """Minimal patient portal principal.

    Deliberately separate from ``identity.user_account``: a patient principal
    must never inherit staff scope. The account is keyed by verified phone only;
    clinical-record access comes exclusively from ``patient_record_link`` rows.
    One account may hold links to several organization-scoped clinical patients.
    """

    __tablename__ = "patient_portal_account"
    __table_args__ = (
        UniqueConstraint("phone", name="uq_identity_patient_portal_account_phone"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    phone: Mapped[str] = mapped_column(String(20), index=True)
    first_name: Mapped[str | None] = mapped_column(String(100), default=None)
    last_name: Mapped[str | None] = mapped_column(String(100), default=None)
    date_of_birth: Mapped[str | None] = mapped_column(String(10), default=None)
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PatientPortalSession(Base):
    """Server-side patient session; the browser receives only the opaque token.

    Only a SHA-256 digest of the token is stored. Sessions are independently
    named/configured from the staff ``healthos_session`` cookie.
    """

    __tablename__ = "patient_portal_session"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_identity_patient_portal_session_token"),
        Index("ix_identity_patient_portal_session_account", "patient_account_id", "created_at"),
        Index("ix_identity_patient_portal_session_expires", "expires_at"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    patient_account_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoke_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class PatientOtpChallenge(Base):
    """Single-use patient phone-OTP challenge.

    The code is never stored in plain text: ``code_verifier`` is a keyed HMAC
    over purpose/phone/challenge/code. ``patient_account_id`` is nullable so an
    unknown phone still yields a real, attempt-limited challenge and an
    enumeration-neutral response; verification of an unknown phone can never
    produce a session.
    """

    __tablename__ = "patient_otp_challenge"
    __table_args__ = (
        Index("ix_identity_patient_otp_challenge_phone", "phone_hash", "created_at"),
        Index("ix_identity_patient_otp_challenge_expires", "expires_at"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    phone_hash: Mapped[str] = mapped_column(String(128), index=True)
    code_verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    patient_account_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
        index=True,
        default=None,
    )
    purpose: Mapped[str] = mapped_column(String(32), default="login")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    ip_hash: Mapped[str | None] = mapped_column(String(128), default=None)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class PatientOtpThrottle(Base):
    """Aggregate per-phone / per-IP patient OTP request throttling (no plaintext key)."""

    __tablename__ = "patient_otp_throttle"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", name="uq_identity_patient_otp_throttle"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    scope: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(128))
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class PatientRecordLink(Base):
    """Audited portal-account → clinical-patient link, scoped to an organization.

    ``patient_id`` is null only while a clinic-assisted/self-start request is
    pending reconciliation; staff approval must set it through the validated
    staff workflow. Verification source/time, reviewer, permitted facility and
    revocation are recorded. Partial unique indexes keep at most one active
    (pending/verified) link per account+organization and per clinical patient.
    """

    __tablename__ = "patient_record_link"
    __table_args__ = (
        Index(
            "uq_identity_patient_link_account_org_active",
            "patient_account_id",
            "organization_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'verified')"),
        ),
        Index(
            "uq_identity_patient_link_patient_active",
            "patient_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'verified')"),
        ),
        Index("ix_identity_patient_link_account_status", "patient_account_id", "status"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    patient_account_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
        index=True,
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.organization.id"), index=True
    )
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("organization.facility.id"), index=True, default=None
    )
    patient_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.patient.id"), index=True, default=None
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    request_kind: Mapped[str] = mapped_column(String(32), default="clinic_assisted")
    verification_source: Mapped[str | None] = mapped_column(String(32), default=None)
    claimed_name: Mapped[str | None] = mapped_column(String(255), default=None)
    claimed_date_of_birth: Mapped[str | None] = mapped_column(String(10), default=None)
    reviewed_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id"), default=None
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    review_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    revoked_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id"), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoke_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PatientLinkInvitation(Base):
    """Staff-authorized, expiring, single-use portal-link invitation.

    Bound to the intended recipient phone and to an existing organization-scoped
    clinical patient. Only a keyed hash of the activation token is stored.
    """

    __tablename__ = "patient_link_invitation"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_identity_patient_link_invitation_token"),
        Index("ix_identity_patient_link_invitation_org_status", "organization_id", "status"),
        {"schema": "identity"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        ForeignKey("organization.organization.id"), index=True
    )
    patient_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.patient.id"), index=True
    )
    phone: Mapped[str] = mapped_column(String(20), index=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        ForeignKey("organization.facility.id"), index=True, default=None
    )
    purpose: Mapped[str] = mapped_column(String(32), default="portal_link")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id"), default=None
    )
    accepted_by_account_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.patient_portal_account.id", ondelete="SET NULL"),
        default=None,
    )
    accepted_link_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("identity.patient_record_link.id", ondelete="SET NULL"),
        default=None,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_by_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
