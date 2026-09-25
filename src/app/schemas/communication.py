"""Pydantic schemas for the implemented communication endpoints (BE01).

Only realtime config/token are implemented. Chat/notifications/preferences/
recovery/push schemas are intentionally absent until their steps land.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RealtimeChannels(BaseModel):
    own: str
    presence: list[str]


class RealtimeFeatureConfig(BaseModel):
    available: bool
    provider: Literal["ably"] = "ably"
    reason: str | None = None
    namespace: str
    token_type: Literal["token_request"] = "token_request"
    token_ttl_seconds: int
    renew_after_seconds: int
    presence_enabled: bool
    browser_capabilities: list[str]
    browser_publish: bool = False


class PreferenceCategoryPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    code: str
    in_app: bool | None = Field(default=None, alias="inApp")
    browser: bool | None = None
    sound: bool | None = None


class QuietHoursPatch(BaseModel):
    enabled: bool
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "Asia/Kolkata"


class NotificationPreferencesPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    categories: list[PreferenceCategoryPatch] = Field(default_factory=list)
    quiet_hours: QuietHoursPatch | None = Field(default=None, alias="quietHours")
    desktop_alerts: bool | None = Field(default=None, alias="desktopAlerts")


class ReadAllScope(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    tab: Literal["all", "unread", "action"] = "unread"
    priority: Literal["P1", "P2", "P3"] | None = None
    facility_uuid: str | None = Field(default=None, alias="facilityUuid")


class ReadAllRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cutoff: datetime | None = None
    scope: ReadAllScope | None = None


class ConversationCreate(BaseModel):
    kind: Literal["direct", "team"] = "direct"
    member_uuids: list[UUID] = Field(min_length=1)
    title: str | None = Field(default=None, max_length=255)
    facility_uuid: UUID | None = None


class MessageCreate(BaseModel):
    body: str = Field(min_length=1)
    client_message_id: str | None = Field(default=None, max_length=128)
    kind: Literal["text"] = "text"


class ReadCursorRequest(BaseModel):
    message_id: UUID


class MemberAddRequest(BaseModel):
    staff_uuid: UUID


class PushDeviceRegister(BaseModel):
    token: str = Field(min_length=8, max_length=512)
    installation_id: str = Field(min_length=1, max_length=128)
    platform: Literal["web"] = "web"
    environment: str | None = Field(default=None, max_length=32)
    user_agent: str | None = Field(default=None, max_length=512)


class WorkStatusPatch(BaseModel):
    facility_uuid: UUID
    work_state: Literal["available", "with-patient", "busy", "on-break", "away"]
    duty: Literal["on-duty", "off-duty"] | None = None
    return_at: datetime | None = None


class RealtimeTokenResponse(BaseModel):
    available: bool
    provider: Literal["ably"] = "ably"
    namespace: str
    client_id: str
    generation: int
    channels: RealtimeChannels
    capabilities: dict[str, list[str]]
    # Signed Ably TokenRequest. Contains no server API key.
    token_request: dict[str, Any]
    token_type: Literal["token_request"] = "token_request"
    expires_at: datetime
    renew_after_seconds: int
    issued_at: datetime
