import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select

from ...core import events
from ...core.db.database import local_session
from ...models.care import Practitioner
from ...models.organization import Facility, StaffAssignment, StaffMember
from ..dependencies import get_current_identity_account
from .bootstrap import ADMIN_ROLES, _staff_context

router = APIRouter(tags=["events"])


async def _authorized_scope(request: Request, facility_uuid: UUID | None, practitioner_uuid: UUID | None) -> dict:
    async with local_session() as db:
        account = await get_current_identity_account(request, db)
        _, organization = await _staff_context(db, account)
        facilities = (
            select(Facility.id)
            .join(StaffMember, StaffMember.organization_id == Facility.organization_id)
            .join(StaffAssignment, StaffAssignment.staff_member_id == StaffMember.id)
            .where(
                Facility.organization_id == organization.id,
                Facility.is_active.is_(True),
                StaffMember.user_account_id == account.id,
                StaffMember.is_active.is_(True),
                StaffAssignment.is_active.is_(True),
                or_(StaffAssignment.facility_id == Facility.id, StaffAssignment.role_code.in_(ADMIN_ROLES)),
            )
            .distinct()
        )
        allowed_facilities = {row[0] for row in (await db.execute(facilities)).all()}
        if facility_uuid is not None and facility_uuid not in allowed_facilities:
            raise HTTPException(status_code=403, detail="Facility access is not permitted.")
        if practitioner_uuid is not None:
            practitioner = await db.scalar(
                select(Practitioner.id).where(
                    Practitioner.id == practitioner_uuid,
                    Practitioner.organization_id == organization.id,
                    Practitioner.is_active.is_(True),
                )
            )
            if practitioner is None:
                raise HTTPException(status_code=403, detail="Practitioner access is not permitted.")
        return {
            "account_id": account.id,
            "organization_id": organization.id,
            "facilities": {facility_uuid} if facility_uuid else allowed_facilities,
            "practitioner_id": practitioner_uuid,
        }


async def _still_authorized(request: Request, scope: dict) -> bool:
    try:
        current = await _authorized_scope(request, None, scope["practitioner_id"])
        return scope["organization_id"] == current["organization_id"] and scope["facilities"].issubset(current["facilities"])
    except HTTPException:
        return False


def _frame(event_name: str, payload: dict, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.get("/events/stream")
async def stream(
    request: Request,
    facility_uuid: UUID | None = None,
    practitioner_uuid: UUID | None = None,
) -> StreamingResponse:
    scope = await _authorized_scope(request, facility_uuid, practitioner_uuid)
    try:
        await events.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Event stream is unavailable.") from exc

    async def body() -> AsyncIterator[str]:
        # Last-Event-ID is intentionally ignored: this stream is invalidation-only, not replay.
        yield _frame("reset", {"reason": "subscription_active"})
        async for event in events.subscribe():
            if await request.is_disconnected():
                break
            if event.get("_heartbeat"):
                if not await _still_authorized(request, scope):
                    yield _frame("error", {"code": "ACCESS_REVOKED"})
                    break
                yield ": heartbeat\n\n"
                continue
            if event.get("organization_id") != str(scope["organization_id"]):
                continue
            if UUID(event["facility_id"]) not in scope["facilities"]:
                continue
            if scope["practitioner_id"] and event.get("practitioner_id") not in {None, str(scope["practitioner_id"])}:
                continue
            yield _frame(event["event_type"], event, event["event_id"])

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
