from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class ConsultationFeeInput(BaseModel):
    scope_type: Literal["facility", "specialty", "practitioner"]
    amount_minor: int = Field(ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    effective_from: date
    practitioner_uuid: UUID | None = None
    specialty_uuid: UUID | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        if not value.isalpha():
            raise ValueError("currency must be a three-letter code")
        return value.upper()

    @model_validator(mode="after")
    def validate_scope(self):
        if self.scope_type == "facility" and (
            self.practitioner_uuid or self.specialty_uuid
        ):
            raise ValueError(
                "facility scope cannot include practitioner_uuid or specialty_uuid"
            )
        if self.scope_type == "specialty" and (
            not self.specialty_uuid or self.practitioner_uuid
        ):
            raise ValueError("specialty scope requires only specialty_uuid")
        if self.scope_type == "practitioner" and (
            not self.practitioner_uuid or self.specialty_uuid
        ):
            raise ValueError("practitioner scope requires only practitioner_uuid")
        return self


class PaymentInput(BaseModel):
    amount_minor: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=128)
    received_at: datetime | None = None

    @field_validator("received_at")
    @classmethod
    def require_received_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("received_at must include a timezone offset")
        return value


class RefundInput(BaseModel):
    amount_minor: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=128)
    reason: str | None = Field(default=None, max_length=500)
    refunded_at: datetime | None = None

    @field_validator("refunded_at")
    @classmethod
    def require_refunded_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("refunded_at must include a timezone offset")
        return value
