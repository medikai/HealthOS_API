"""Realtime config and scoped token endpoints (implemented in BE01)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ....api.dependencies import get_current_identity_account
from ....core.db.database import async_get_db
from ....models.identity import UserAccount
from ....schemas.common import SuccessResponse
from ....schemas.communication import RealtimeFeatureConfig, RealtimeTokenResponse
from ..dependencies import (
    CommunicationStaffContext,
    get_communication_staff_context,
    get_realtime_service,
)
from ..shared.errors import ProviderUnavailable
from .service import RealtimeService

router = APIRouter(prefix="/realtime", tags=["communication"])


def _unavailable(exc: ProviderUnavailable) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "PROVIDER_UNAVAILABLE",
            "message": "Realtime transport is unavailable.",
            "details": [{"reason": exc.reason}],
        },
    )


@router.get("/config", response_model=SuccessResponse[RealtimeFeatureConfig])
async def realtime_config(
    service: Annotated[RealtimeService, Depends(get_realtime_service)],
) -> dict:
    """Feature availability and channel/token policy. No credentials returned."""
    return {"success": True, "data": service.feature_config(), "meta": {}}


@router.post("/token", response_model=SuccessResponse[RealtimeTokenResponse])
async def realtime_token(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    context: Annotated[CommunicationStaffContext, Depends(get_communication_staff_context)],
    service: Annotated[RealtimeService, Depends(get_realtime_service)],
) -> dict:
    """Issue a short-lived, subscribe-only signed token request for the caller.

    The server derives tenant, staff and exact channels from membership. The
    body is empty by design: no client-supplied sender/recipient/channel.
    """
    try:
        data = await service.issue_token(db, context)
    except ProviderUnavailable as exc:
        raise _unavailable(exc) from exc
    return {"success": True, "data": data, "meta": {}}
