"""Typed internal request/result contracts for the email subsystem.

Email HTTP details stay in the provider; routers/auth depend on these types
only. Header-injection characters are rejected at the boundary.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


def reject_header_injection(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("Header injection characters are not allowed.")
    return value


class EmailMessageRequest(BaseModel):
    """Internal, template-based send request (not a public API payload)."""

    template_code: str = Field(min_length=1, max_length=64)
    to_email: EmailStr
    to_name: str | None = Field(default=None, max_length=255)
    context: dict[str, Any] = Field(default_factory=dict)
    secret_context: dict[str, Any] = Field(default_factory=dict)
    organization_id: UUID | None = None
    dedup_key: str = Field(min_length=1, max_length=300)
    expires_at: datetime | None = None

    @field_validator("to_name")
    @classmethod
    def _safe_to_name(cls, value: str | None) -> str | None:
        return None if value is None else reject_header_injection(value)


class EmailProviderRequest(BaseModel):
    """Fully rendered message handed to a transport provider."""

    to_email: EmailStr
    to_name: str | None = None
    from_email: EmailStr
    from_name: str | None = None
    subject: str = Field(min_length=1, max_length=512)
    html_body: str
    text_body: str
    provider: str = "zeptomail"

    @field_validator("to_name", "from_name")
    @classmethod
    def _safe_names(cls, value: str | None) -> str | None:
        return None if value is None else reject_header_injection(value)

    @field_validator("subject")
    @classmethod
    def _safe_subject(cls, value: str) -> str:
        return reject_header_injection(value)


class EmailProviderResult(BaseModel):
    provider: str
    accepted: bool
    status_code: int | None = None
    provider_request_id: str | None = None
    provider_message_id: str | None = None
