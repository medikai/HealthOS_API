import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from ..core.db.database import Base


class UserAccount(Base):
    """HealthOS-owned profile mapped to an immutable Logto subject."""

    __tablename__ = "user_account"
    __table_args__ = {"schema": "identity"}

    id: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default_factory=uuid7, init=False
    )
    logto_user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(2_048), default=None)
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
