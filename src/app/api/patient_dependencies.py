"""Patient authorization dependency, explicit and separate from staff auth.

Patient routes resolve only the dedicated patient session cookie. Staff
``healthos_session`` cookies, staff JWTs and arbitrary ``Authorization`` headers
are never consulted here, so a staff principal cannot satisfy a patient route
and patient tokens never inherit staff scope.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db.database import async_get_db
from ..core.exceptions.http_exceptions import UnauthorizedException
from ..domains.patient_auth.sessions import PatientSessionService
from ..models.identity import PatientPortalAccount, PatientPortalSession

patient_session_service = PatientSessionService()


@dataclass(frozen=True)
class PatientPrincipal:
    account: PatientPortalAccount
    session: PatientPortalSession
    token: str


async def get_current_patient(
    request: Request,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PatientPrincipal:
    token = request.cookies.get(settings.PATIENT_SESSION_COOKIE_NAME)
    if not token:
        raise UnauthorizedException("Authentication required.")
    session = await patient_session_service.resolve(db, token)
    if session is None:
        raise UnauthorizedException("Authentication required.")
    account = await db.get(PatientPortalAccount, session.patient_account_id)
    if account is None or not account.is_active:
        raise UnauthorizedException("Authentication required.")
    return PatientPrincipal(account=account, session=session, token=token)
