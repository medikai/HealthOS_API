"""Notification inbox, read state, preferences and dispatch recheck."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ....models.care import Appointment, QueueEntry
from ....models.communication import (
    Conversation,
    ConversationMember,
    NotificationEvent,
    NotificationRecipient,
)
from ....models.organization import Facility, StaffAssignment, StaffMember
from .constants import (
    CATEGORY_CATALOG,
    EVENT_QUEUE_READY,
    LOCKED_CATEGORIES,
    PRIORITIES,
    REPLAY_WINDOW_DAYS,
)
from .recipients import ADMIN_ROLES
from .repository import NotificationRepository
from .serialization import (
    decode_cursor,
    encode_cursor,
    notification_item,
    preference_defaults,
)

if TYPE_CHECKING:
    from ..dependencies import CommunicationStaffContext

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
STAFF_SETTINGS_CATEGORY = "all"
VALID_TABS = ("all", "unread", "action")


def _parse_uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid UUID.") from None


class NotificationService:
    def __init__(self, repository: NotificationRepository | None = None) -> None:
        self.repository = repository or NotificationRepository()

    def _authorize_facility(
        self, context: CommunicationStaffContext, facility_uuid: str | UUID | None
    ) -> UUID | None:
        facility_id = _parse_uuid(facility_uuid)
        if facility_id is not None and facility_id not in context.facility_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Facility access is not permitted.",
            )
        return facility_id

    async def _facilities(self, db: AsyncSession, context: CommunicationStaffContext) -> list[dict]:
        if not context.facility_ids:
            return []
        rows = await db.execute(
            select(Facility.id, Facility.name).where(Facility.id.in_(context.facility_ids))
        )
        return [{"uuid": str(fid), "name": name} for fid, name in rows.all()]

    def _validate_filters(self, tab: str, priority: str | None) -> str | None:
        if tab not in VALID_TABS:
            raise HTTPException(status_code=422, detail="Invalid tab.")
        if priority is not None and priority not in PRIORITIES:
            raise HTTPException(status_code=422, detail="Invalid priority.")
        return priority

    async def list_notifications(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        *,
        tab: str = "all",
        priority: str | None = None,
        facility_uuid: str | UUID | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        priority = self._validate_filters(tab, priority)
        facility_id = self._authorize_facility(context, facility_uuid)
        decoded = decode_cursor(cursor) if cursor else None
        if cursor and decoded is None:
            raise HTTPException(status_code=422, detail="Invalid cursor.")
        limit = max(1, min(limit, MAX_LIMIT))
        rows = await self.repository.list_for_staff(
            db,
            organization_id=context.organization_id,
            staff_member_id=context.staff_member_id,
            limit=limit,
            cursor=decoded,
            tab=tab,
            priority=priority,
            facility_id=facility_id,
            query=query,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [notification_item(event, recipient, facility_name=name) for event, recipient, name in rows]
        next_cursor = None
        if has_more and rows:
            last_event, _last_recipient, _name = rows[-1]
            next_cursor = encode_cursor(last_event.updated_at, last_event.id)
        counts = await self.repository.counts(
            db,
            staff_member_id=context.staff_member_id,
            organization_id=context.organization_id,
            facility_id=facility_id,
        )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "counts": counts,
        }

    async def get_notification(
        self, db: AsyncSession, context: CommunicationStaffContext, notification_id: UUID
    ) -> dict[str, Any]:
        row = await self.repository.get_for_staff(
            db, staff_member_id=context.staff_member_id, notification_id=notification_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Notification not found.")
        event, recipient, facility_name = row
        return notification_item(event, recipient, facility_name=facility_name)

    async def mark_read(
        self, db: AsyncSession, context: CommunicationStaffContext, notification_id: UUID
    ) -> dict[str, Any]:
        found = await self.repository.mark_read(
            db,
            staff_member_id=context.staff_member_id,
            notification_id=notification_id,
        )
        if not found:
            raise HTTPException(status_code=404, detail="Notification not found.")
        # read is independent of task state: return both to make that explicit.
        return await self.get_notification(db, context, notification_id)

    async def mark_all_read(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        *,
        cutoff: datetime | None = None,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = scope or {}
        tab = scope.get("tab", "unread")
        if tab not in ("unread", "action", "all"):
            raise HTTPException(status_code=422, detail="Invalid read-all scope.")
        priority = self._validate_filters("all", scope.get("priority"))
        facility_id = self._authorize_facility(context, scope.get("facility_uuid"))
        snapshot = cutoff or datetime.now(UTC)
        updated = await self.repository.mark_all_read(
            db,
            staff_member_id=context.staff_member_id,
            organization_id=context.organization_id,
            cutoff=snapshot,
            tab=tab,
            priority=priority,
            facility_id=facility_id,
            now=snapshot,
        )
        return {
            "updated": updated,
            "cutoff": snapshot.isoformat(),
            "scope": {"tab": tab, "priority": priority, "facility_uuid": str(facility_id) if facility_id else None},
        }

    async def counts(
        self, db: AsyncSession, context: CommunicationStaffContext, *, facility_uuid=None
    ) -> dict[str, int]:
        facility_id = self._authorize_facility(context, facility_uuid)
        return await self.repository.counts(
            db,
            staff_member_id=context.staff_member_id,
            organization_id=context.organization_id,
            facility_id=facility_id,
        )

    async def changes(
        self,
        db: AsyncSession,
        context: CommunicationStaffContext,
        *,
        since: datetime | None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        if since is None or since < now - timedelta(days=REPLAY_WINDOW_DAYS):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={
                    "code": "CURSOR_EXPIRED",
                    "message": "Replay cursor is too old; resync the inbox.",
                    "details": [{"resync": True}],
                },
            )
        limit = max(1, min(limit, MAX_LIMIT))
        # inclusive overlap: duplicates are deduplicated by the client via id.
        rows = await self.repository.changes_since(
            db,
            organization_id=context.organization_id,
            staff_member_id=context.staff_member_id,
            since=since,
            limit=limit,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [notification_item(event, recipient, facility_name=name) for event, recipient, name in rows]
        next_cursor = None
        if rows:
            last_event = rows[-1][0]
            next_cursor = encode_cursor(last_event.updated_at, last_event.id)
        counts = await self.repository.counts(
            db,
            staff_member_id=context.staff_member_id,
            organization_id=context.organization_id,
        )
        return {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "server_time": now.isoformat(),
            "counts": counts,
            "resync_required": False,
        }

    async def get_preferences(
        self, db: AsyncSession, context: CommunicationStaffContext
    ) -> dict[str, Any]:
        stored = {
            row.category: row
            for row in await self.repository.get_preferences(
                db,
                organization_id=context.organization_id,
                staff_member_id=context.staff_member_id,
            )
        }
        categories = []
        for code in CATEGORY_CATALOG:
            defaults = preference_defaults(code)
            row = stored.get(code)
            if row is not None:
                defaults["inApp"] = row.in_app
                defaults["browser"] = row.browser
                defaults["sound"] = row.sound
                if code in LOCKED_CATEGORIES:
                    defaults["inApp"] = True
                    defaults["browser"] = True
            categories.append(defaults)
        staff_row = stored.get(STAFF_SETTINGS_CATEGORY)
        return {
            "categories": categories,
            "quietHours": {
                "enabled": staff_row.quiet_hours_enabled if staff_row else False,
                "start": staff_row.quiet_start if staff_row else "22:00",
                "end": staff_row.quiet_end if staff_row else "07:00",
                "timezone": staff_row.timezone if staff_row else "Asia/Kolkata",
            },
            "desktopAlerts": bool(staff_row.desktop_alerts) if staff_row else False,
            "facilities": await self._facilities(db, context),
        }

    async def patch_preferences(
        self, db: AsyncSession, context: CommunicationStaffContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        for category_patch in payload.get("categories") or []:
            code = category_patch.get("code")
            if code not in CATEGORY_CATALOG:
                raise HTTPException(status_code=422, detail=f"Unknown preference category '{code}'.")
            values: dict[str, Any] = {}
            for key in ("inApp", "browser", "sound"):
                if key in category_patch and category_patch[key] is not None:
                    values[_snake(key)] = bool(category_patch[key])
            if code in LOCKED_CATEGORIES and (
                values.get("in_app") is False or values.get("browser") is False
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"Category '{code}' is locked by policy.",
                )
            if values:
                await self.repository.upsert_preference(
                    db,
                    organization_id=context.organization_id,
                    staff_member_id=context.staff_member_id,
                    category=code,
                    values=values,
                )
        staff_values: dict[str, Any] = {}
        if "desktopAlerts" in payload and payload["desktopAlerts"] is not None:
            staff_values["desktop_alerts"] = bool(payload["desktopAlerts"])
        quiet = payload.get("quietHours")
        if quiet:
            staff_values.update(
                {
                    "quiet_hours_enabled": bool(quiet.get("enabled")),
                    "quiet_start": quiet.get("start") or "22:00",
                    "quiet_end": quiet.get("end") or "07:00",
                    "timezone": quiet.get("timezone") or "Asia/Kolkata",
                }
            )
        if staff_values:
            await self.repository.upsert_preference(
                db,
                organization_id=context.organization_id,
                staff_member_id=context.staff_member_id,
                category=STAFF_SETTINGS_CATEGORY,
                values=staff_values,
            )
        await db.commit()
        return await self.get_preferences(db, context)

    async def evaluate_dispatch(
        self, db: AsyncSession, event: NotificationEvent, recipient: NotificationRecipient
    ) -> str:
        """Recheck eligibility/task state before delayed delivery.

        Returns ``publish``, ``superseded``, ``ineligible`` or ``stale``. Never
        trusts the queued snapshot blindly.
        """
        if event.superseded_by_id is not None:
            return "superseded"
        if event.resource_type == "conversation" and event.resource_id:
            # Chat attention is authorized by current conversation membership,
            # not facility assignment: a direct conversation may have no
            # facility, and removed members must not receive future state.
            member = await db.scalar(
                select(ConversationMember.staff_member_id)
                .join(
                    Conversation,
                    Conversation.id == ConversationMember.conversation_id,
                )
                .join(
                    StaffMember,
                    StaffMember.id == ConversationMember.staff_member_id,
                )
                .where(
                    ConversationMember.conversation_id == _parse_uuid(event.resource_id),
                    ConversationMember.staff_member_id == recipient.staff_member_id,
                    ConversationMember.left_at.is_(None),
                    Conversation.organization_id == event.organization_id,
                    Conversation.is_active.is_(True),
                    StaffMember.is_active.is_(True),
                )
                .limit(1)
            )
            if member is None:
                return "ineligible"
        else:
            eligible = await db.scalar(
                select(StaffMember.id)
                .join(StaffAssignment, StaffAssignment.staff_member_id == StaffMember.id)
                .where(
                    StaffMember.id == recipient.staff_member_id,
                    StaffMember.is_active.is_(True),
                    StaffAssignment.is_active.is_(True),
                    or_(
                        StaffAssignment.facility_id == event.facility_id,
                        StaffAssignment.role_code.in_(ADMIN_ROLES),
                    ),
                )
                .limit(1)
            )
            if eligible is None:
                return "ineligible"
        if event.task_type == "queue_entry" and event.task_id:
            queue_status = await db.scalar(
                select(QueueEntry.status).where(QueueEntry.id == _parse_uuid(event.task_id))
            )
            if event.event_type == EVENT_QUEUE_READY and queue_status in {
                "cancelled",
                "completed",
                "in_consultation",
                "skipped",
            }:
                event.task_state = "resolved"
                event.updated_at = datetime.now(UTC)
                await db.flush()
                return "stale"
        if event.task_type == "appointment" and event.task_id:
            appointment_status = await db.scalar(
                select(Appointment.status).where(Appointment.id == _parse_uuid(event.task_id))
            )
            if appointment_status in {"cancelled", "no_show", "completed"}:
                event.task_state = "resolved"
                event.updated_at = datetime.now(UTC)
                await db.flush()
                return "stale"
        return "publish"


def _snake(value: str) -> str:
    return "in_app" if value == "inApp" else value
