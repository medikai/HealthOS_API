"""Patient realtime config/token endpoints.

Patient session cookie only (``get_current_patient``); staff cookies/JWTs and
arbitrary bearer headers are never consulted. POST requires the patient CSRF
header (enforced by ``CSRFMiddleware`` on the patient namespace). The server
returns a signed, subscribe-only Ably TokenRequest; ``ABLY_API_KEY`` never
leaves the backend.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.patient_dependencies import PatientPrincipal, get_current_patient
from ...core.config import settings
from ...core.db.database import async_get_db
from ...domains.communication.realtime.patient_service import PatientRealtimeService
from ...domains.communication.realtime.repository import (
    PatientRealtimeChannelRepository,
)
from ...domains.communication.shared.errors import ProviderUnavailable
from ...schemas.common import SuccessResponse
from ...schemas.communication import (
    PatientRealtimeFeatureConfig,
    PatientRealtimeTokenResponse,
)

router = APIRouter(prefix="/patient/realtime", tags=["patient"])


def get_patient_realtime_service() -> PatientRealtimeService:
    """Build the patient realtime service from backend-only settings."""
    from ...domains.communication.realtime.providers.ably import (
        build_ably_token_provider,
    )

    return PatientRealtimeService(
        provider=build_ably_token_provider(settings),
        repository=PatientRealtimeChannelRepository(),
        namespace=settings.ABLY_CHANNEL_NAMESPACE,
        ttl_seconds=settings.ABLY_TOKEN_TTL_SECONDS,
        renew_after_seconds=settings.ABLY_TOKEN_RENEW_AFTER_SECONDS,
    )


def _unavailable(exc: ProviderUnavailable) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "PROVIDER_UNAVAILABLE",
            "message": "Realtime transport is unavailable.",
            "details": [{"reason": exc.reason}],
        },
    )


@router.get("/config", response_model=SuccessResponse[PatientRealtimeFeatureConfig])
async def patient_realtime_config(
    _principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    service: Annotated[PatientRealtimeService, Depends(get_patient_realtime_service)],
) -> dict:
    """Feature availability and token policy. No credentials returned."""
    return {"success": True, "data": service.feature_config(), "meta": {}}


@router.post("/token", response_model=SuccessResponse[PatientRealtimeTokenResponse])
async def patient_realtime_token(
    principal: Annotated[PatientPrincipal, Depends(get_current_patient)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    service: Annotated[PatientRealtimeService, Depends(get_patient_realtime_service)],
) -> dict:
    """Issue a short-lived, subscribe-only token request for the caller.

    The body is empty by design: no client-supplied account, organization or
    channel is accepted.
    """
    try:
        data = await service.issue_token(db, principal.account)
    except ProviderUnavailable as exc:
        raise _unavailable(exc) from exc
    return {"success": True, "data": data, "meta": {}}
