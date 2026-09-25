import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from ..core.db.database import Base

# Durable delivery channels. Chat/notifications/push/email stay separate
# services on top of one lease-based dispatch table; this is not a generic util.
JOB_CHANNEL_REALTIME = "realtime"
JOB_CHANNEL_EMAIL = "email"
JOB_CHANNEL_PUSH = "push"

JOB_STATUS_PENDING = "pending"
JOB_STATUS_LEASED = "leased"
JOB_STATUS_DELIVERED = "delivered"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_DEAD = "dead"
JOB_STATUS_CANCELLED = "cancelled"

JOB_STATUSES = (
    JOB_STATUS_PENDING,
    JOB_STATUS_LEASED,
    JOB_STATUS_DELIVERED,
    JOB_STATUS_FAILED,
    JOB_STATUS_DEAD,
    JOB_STATUS_CANCELLED,
)


class DeliveryJob(Base):
    """Durable outbox/delivery job.

    One row is a single unit of external delivery work. Chat, notifications,
    email and push services enqueue rows inside the same business transaction;
    the lease worker claims, dispatches and retries without holding the
    transaction across provider calls.
    """

    __tablename__ = "delivery_job"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_communication_delivery_job_dedup"),
        Index("ix_communication_delivery_job_claim", "status", "due_at"),
        Index("ix_communication_delivery_job_lease", "status", "lease_expires_at"),
        Index(
            "ix_communication_delivery_job_recipient",
            "organization_id",
            "recipient_staff_id",
        ),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    # Delivery channel: realtime | email | push.
    channel: Mapped[str] = mapped_column(String(32), index=True)
    # Business event type, e.g. notification.created.
    event_type: Mapped[str] = mapped_column(String(128))
    # Idempotency key scoped by the producer (tenant/recipient/event/business id).
    dedup_key: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(32), default=JOB_STATUS_PENDING)
    # Minimal, non-secret payload. Never store OTPs, reset URLs or provider tokens.
    payload: Mapped[dict] = mapped_column(JSONB, default_factory=dict)
    organization_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True, default=None
    )
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.facility.id"), index=True, default=None
    )
    recipient_staff_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True, default=None
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    # Redacted provider acknowledgement metadata only (accepted rejected etc).
    result: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Data expiry for retention sweeps; never holds credential material.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


EMAIL_STATUS_QUEUED = "queued"
EMAIL_STATUS_ACCEPTED = "accepted"
EMAIL_STATUS_FAILED = "failed"
EMAIL_STATUS_UNKNOWN = "unknown"
EMAIL_STATUS_DEAD = "dead"

EMAIL_STATUSES = (
    EMAIL_STATUS_QUEUED,
    EMAIL_STATUS_ACCEPTED,
    EMAIL_STATUS_FAILED,
    EMAIL_STATUS_UNKNOWN,
    EMAIL_STATUS_DEAD,
)


class EmailMessage(Base):
    """Durable email request plus per-message delivery metadata.

    Rendering happens at dispatch time. Secret-bearing context (OTP/reset URLs)
    is stored only as ``encrypted_context`` and erased once a terminal outcome
    is reached; ordinary outbox payloads reference this row by id and never
    carry the secret. ``queued``/``accepted``/``failed``/``unknown``/``dead``
    are tracked separately from the durable job's own retry status.
    """

    __tablename__ = "email_message"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_communication_email_message_dedup"),
        Index("ix_communication_email_message_status", "status", "created_at"),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    dedup_key: Mapped[str] = mapped_column(String(320))
    template_code: Mapped[str] = mapped_column(String(64))
    recipient_email: Mapped[str] = mapped_column(String(320))
    from_email: Mapped[str] = mapped_column(String(320))
    organization_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True, default=None
    )
    recipient_name: Mapped[str | None] = mapped_column(String(255), default=None)
    from_name: Mapped[str | None] = mapped_column(String(255), default=None)
    subject: Mapped[str | None] = mapped_column(String(512), default=None)
    status: Mapped[str] = mapped_column(String(32), default=EMAIL_STATUS_QUEUED)
    provider: Mapped[str | None] = mapped_column(String(32), default=None)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), default=None)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    # Non-secret rendering context.
    context: Mapped[dict] = mapped_column(JSONB, default_factory=dict)
    # Fernet-encrypted rendering context for secret fields; null for non-secret.
    encrypted_context: Mapped[str | None] = mapped_column(Text, default=None)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Secret context must not outlive this instant.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


NOTIFICATION_TASK_STATES = (
    "none",
    "awaiting",
    "accepted",
    "declined",
    "resolved",
    "superseded",
    "reassigned",
    "cancelled",
)


class NotificationEvent(Base):
    """One logical notification (event) with an immutable recipient set.

    Read state lives on ``NotificationRecipient``; task/action state lives here
    and is never changed by marking read. ``dedup_key`` makes recording
    idempotent inside the owning workflow transaction.
    """

    __tablename__ = "notification_event"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_communication_notification_event_dedup"),
        Index("ix_communication_notification_event_feed", "organization_id", "updated_at"),
        Index("ix_communication_notification_event_coalesce", "coalesce_key"),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(32), index=True)
    priority: Mapped[str] = mapped_column(String(2), index=True)
    dedup_key: Mapped[str] = mapped_column(String(320))
    kind: Mapped[str] = mapped_column(String(16), default="update")
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.facility.id"), index=True, default=None
    )
    context: Mapped[str | None] = mapped_column(String(512), default=None)
    location: Mapped[str | None] = mapped_column(String(255), default=None)
    actor_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id"), default=None
    )
    actor_name: Mapped[str | None] = mapped_column(String(255), default=None)
    actor_role: Mapped[str | None] = mapped_column(String(64), default=None)
    task_type: Mapped[str | None] = mapped_column(String(32), default=None)
    task_id: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    task_state: Mapped[str] = mapped_column(String(32), default="none")
    action_kind: Mapped[str | None] = mapped_column(String(32), default=None)
    action_label: Mapped[str | None] = mapped_column(String(64), default=None)
    resource_type: Mapped[str | None] = mapped_column(String(64), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(128), default=None)
    coalesce_key: Mapped[str | None] = mapped_column(String(160), default=None)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("communication.notification_event.id"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class NotificationRecipient(Base):
    """Per-recipient inbox/read state for a notification event."""

    __tablename__ = "notification_recipient"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", "staff_member_id",
            name="uq_communication_notification_recipient",
        ),
        Index("ix_communication_notification_recipient_inbox", "staff_member_id", "read_at"),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    notification_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("communication.notification_event.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True
    )
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.facility.id"), default=None
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class NotificationPreference(Base):
    """Per-staff per-category delivery preference (inbox is always stored)."""

    __tablename__ = "notification_preference"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "staff_member_id", "category",
            name="uq_communication_notification_preference",
        ),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True
    )
    category: Mapped[str] = mapped_column(String(32))
    in_app: Mapped[bool] = mapped_column(Boolean, default=True)
    browser: Mapped[bool] = mapped_column(Boolean, default=True)
    sound: Mapped[bool] = mapped_column(Boolean, default=False)
    desktop_alerts: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_start: Mapped[str] = mapped_column(String(5), default="22:00")
    quiet_end: Mapped[str] = mapped_column(String(5), default="07:00")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


CONVERSATION_KIND_DIRECT = "direct"
CONVERSATION_KIND_TEAM = "team"

MESSAGE_KIND_TEXT = "text"
MESSAGE_KIND_HANDOVER = "handover"

WORK_STATES = ("available", "with-patient", "busy", "on-break", "away")
DUTY_STATES = ("on-duty", "off-duty")
WORK_STATUS_SOURCE_STAFF = "staff"
WORK_STATUS_SOURCE_CONSULTATION = "consultation"


class Conversation(Base):
    """Staff conversation (direct pair or bounded facility/team group).

    ``direct_key`` is the deterministic sorted staff pair per organization so
    concurrent first direct conversations deduplicate. ``next_sequence`` is the
    per-conversation message order, assigned under a row lock.
    """

    __tablename__ = "conversation"
    __table_args__ = (
        UniqueConstraint("organization_id", "direct_key", name="uq_communication_conversation_direct"),
        Index("ix_communication_conversation_org_updated", "organization_id", "updated_at"),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    direct_key: Mapped[str | None] = mapped_column(String(160), default=None)
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.facility.id"), index=True, default=None
    )
    title: Mapped[str | None] = mapped_column(String(255), default=None)
    created_by_staff_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), default=None
    )
    next_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ConversationMember(Base):
    __tablename__ = "conversation_member"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "staff_member_id",
            name="uq_communication_conversation_member",
        ),
        Index("ix_communication_conversation_member_staff", "staff_member_id", "conversation_id"),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    conversation_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("communication.conversation.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), default="member")
    join_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    last_read_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    last_read_message_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), default=None
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sender_staff_id",
            "client_message_id",
            name="uq_communication_message_client_id",
        ),
        Index("ix_communication_message_conversation_sequence", "conversation_id", "sequence"),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    conversation_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("communication.conversation.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    sender_staff_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True
    )
    body: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    sender_user_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id"), default=None
    )
    facility_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.facility.id"), default=None
    )
    client_message_id: Mapped[str | None] = mapped_column(String(128), default=None)
    kind: Mapped[str] = mapped_column(String(16), default="text")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )


class WorkStatus(Base):
    """Explicit work availability per staff + facility (separate from presence).

    ``source`` distinguishes a staff override from consultation-derived state so
    a derived update never silently overwrites a user's own choice.
    """

    __tablename__ = "work_status"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "facility_id", "staff_member_id",
            name="uq_communication_work_status",
        ),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    facility_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.facility.id"), index=True
    )
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True
    )
    work_state: Mapped[str] = mapped_column(String(32), default="available")
    duty: Mapped[str | None] = mapped_column(String(16), default=None)
    source: Mapped[str] = mapped_column(String(32), default="staff")
    return_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class PushDevice(Base):
    """FCM web-push device binding.

    Bound to the authenticated account/staff/tenant plus a browser installation
    id. ``token`` is unique so a token cannot remain bound to a previous
    account; registration rebinds it to the current caller, and logout/revoke
    deactivates bindings. No private key is ever stored here.
    """

    __tablename__ = "push_device"
    __table_args__ = (
        UniqueConstraint("token", name="uq_communication_push_device_token"),
        UniqueConstraint(
            "organization_id",
            "staff_member_id",
            "installation_id",
            name="uq_communication_push_device_installation",
        ),
        Index("ix_communication_push_device_staff_active", "staff_member_id", "is_active"),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True
    )
    user_account_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity.user_account.id"), index=True
    )
    installation_id: Mapped[str] = mapped_column(String(128))
    token: Mapped[str] = mapped_column(String(512))
    platform: Mapped[str] = mapped_column(String(32), default="web")
    environment: Mapped[str] = mapped_column(String(32), default="dev")
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class RealtimeChannelState(Base):
    """Server-controlled realtime channel generation per tenant+staff.

    Incrementing generation retires every previously issued channel/token for
    that principal without waiting for token TTL. Tokens are additionally
    short-lived. Provider-side revocation is not available in the installed
    Ably Python SDK, so generation is the authoritative revocation mechanism.
    """

    __tablename__ = "realtime_channel_state"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "staff_member_id",
            name="uq_communication_realtime_channel_state_principal",
        ),
        {"schema": "communication"},
    )

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    organization_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.organization.id"), index=True
    )
    staff_member_id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization.staff_member.id"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default_factory=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
