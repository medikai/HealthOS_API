"""Database-backed patient portal checks (OTP/session/link semantics).

Runs only when ``TEST_POSTGRES_ASYNC_URL`` points at a distinct disposable
database, or when ``HEALTHOS_ALLOW_LOCAL_DB_TESTS=1`` explicitly allows the
configured non-production local database. All rows use unique synthetic
identifiers and are removed in a ``finally`` cleanup; no clinical data is read
or modified.
"""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app.core.config import settings
from src.app.domains.patient_auth.links import (
    ClinicalPatientInvalid,
    InvitationExpired,
    InvitationInvalid,
    LinkAlreadyVerified,
    PatientLinkService,
)
from src.app.domains.patient_auth.otp import (
    ExpiredOtpCode,
    InvalidOtpCode,
    OtpPolicy,
    OtpRateLimited,
    PatientOtpService,
)
from src.app.domains.patient_auth.phone import normalize_phone
from src.app.domains.patient_auth.service import PatientAuthService
from src.app.domains.patient_auth.sessions import PatientSessionService
from src.app.models.care import AuditLog
from src.app.models.identity import (
    AuthSession,
    Patient,
    PatientLinkInvitation,
    PatientOtpChallenge,
    PatientOtpThrottle,
    PatientPortalAccount,
    PatientRecordLink,
    Person,
    UserAccount,
)
from src.app.models.organization import Facility, Organization

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
TEST_DB_URL = os.environ.get("TEST_POSTGRES_ASYNC_URL")
_ALLOW_LOCAL = os.environ.get("HEALTHOS_ALLOW_LOCAL_DB_TESTS") == "1"
ACTIVE_DB_URL = TEST_DB_URL or (
    settings.POSTGRES_ASYNC_URL
    if _ALLOW_LOCAL and settings.ENVIRONMENT.value != "production"
    else None
)
requires_db = pytest.mark.skipif(
    not ACTIVE_DB_URL,
    reason=(
        "Set a distinct TEST_POSTGRES_ASYNC_URL, or HEALTHOS_ALLOW_LOCAL_DB_TESTS=1 "
        "for the configured non-production local database."
    ),
)


class CapturingOtpProvider:
    name = "capturing"
    configured = True

    def __init__(self):
        self.sent: dict[UUID, str] = {}

    async def send_code(self, *, phone, code, challenge_id, purpose, expires_at):
        self.sent[challenge_id] = code

    def exposed_code(self, challenge_id):
        return self.sent.get(challenge_id)


def _upgrade_schema(url: str) -> None:
    sync_url = url.replace("+asyncpg", "+psycopg2")
    env = {**os.environ, "POSTGRES_ASYNC_URL": url, "POSTGRES_SYNC_URL": sync_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=SRC_DIR,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _policy() -> OtpPolicy:
    return OtpPolicy.from_settings(settings)


async def _create_clinic(db, *, prefix: str):
    org = Organization(
        name=f"{prefix} {uuid4().hex[:8]}",
        code=f"{prefix}-{uuid4().hex[:10]}",
        portal_enabled=True,
        is_active=True,
    )
    db.add(org)
    await db.flush()
    facility = Facility(
        organization_id=org.id,
        name=f"{prefix} Main",
        code="MAIN",
        is_active=True,
    )
    db.add(facility)
    await db.flush()
    person = Person(
        organization_id=org.id,
        first_name="Synthetic",
        last_name="Portal",
        phone=f"+91{uuid4().int % 10**10:010d}",
    )
    db.add(person)
    await db.flush()
    patient = Patient(
        organization_id=org.id,
        person_id=person.id,
        mrn=f"MRN-SYN-{uuid4().hex[:8].upper()}",
    )
    db.add(patient)
    await db.flush()
    return org, facility, person, patient


async def _cleanup(db, *, org_ids, account_ids, patient_ids, person_ids, phone_hashes):
    try:
        for table in (PatientRecordLink, PatientLinkInvitation):
            await db.execute(delete(table).where(table.organization_id.in_(org_ids)))
        await db.execute(delete(AuditLog).where(AuditLog.organization_id.in_(org_ids)))
        await db.execute(delete(Patient).where(Patient.id.in_(patient_ids)))
        await db.execute(delete(Person).where(Person.id.in_(person_ids)))
        for table in (PatientPortalAccount,):
            await db.execute(delete(table).where(table.id.in_(account_ids)))
        await db.execute(
            delete(PatientOtpChallenge).where(
                PatientOtpChallenge.phone_hash.in_(phone_hashes)
            )
        )
        await db.execute(
            delete(PatientOtpThrottle).where(
                PatientOtpThrottle.key_hash.in_(phone_hashes)
            )
        )
        await db.execute(delete(Facility).where(Facility.organization_id.in_(org_ids)))
        await db.execute(
            delete(Organization).where(Organization.id.in_(org_ids))
        )
        await db.commit()
    except Exception:  # noqa: BLE001 - cleanup is best effort after assertions
        await db.rollback()


@requires_db
def test_patient_otp_and_session_db_semantics():
    assert ACTIVE_DB_URL is not None
    _upgrade_schema(ACTIVE_DB_URL)
    policy = _policy()
    provider = CapturingOtpProvider()

    async def _exercise() -> None:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        otp_service = PatientOtpService(policy, provider=provider)
        session_service = PatientSessionService()
        auth_service = PatientAuthService(
            otp_service=otp_service,
            session_service=session_service,
            session_ttl_seconds=settings.PATIENT_SESSION_TTL_SECONDS,
        )
        phones = [
            f"+91987{uuid4().int % 10**8:08d}" for _ in range(4)
        ]
        account_ids: list[UUID] = []
        phone_hashes = {policy.phone_hash(normalize_phone(p)) for p in phones}
        try:
            async with factory() as db:
                # First-time phone: the code is delivered and verification
                # creates the portal account (no clinical record is touched).
                req = await auth_service.request_login_otp(
                    db, phone=phones[0], client_ip=None
                )
                challenge_id = UUID(req["challenge_id"])
                assert challenge_id in provider.sent
                with pytest.raises(InvalidOtpCode):
                    await auth_service.verify_login(
                        db,
                        phone=phones[0],
                        challenge_id=challenge_id,
                        code="000000",
                        rotate_token=None,
                    )
                account0, token0 = await auth_service.verify_login(
                    db,
                    phone=phones[0],
                    challenge_id=challenge_id,
                    code=provider.sent[challenge_id],
                    rotate_token=None,
                )
                account_ids.append(account0.id)
                assert account0.phone == normalize_phone(phones[0])
                assert await session_service.resolve(db, token0) is not None

                # Known flow: first verify creates the account and a session.
                req = await auth_service.request_login_otp(
                    db, phone=phones[1], client_ip=None
                )
                first_challenge = UUID(req["challenge_id"])
                with pytest.raises(OtpRateLimited):
                    await auth_service.request_login_otp(
                        db, phone=phones[1], client_ip=None
                    )
                challenge = await db.get(PatientOtpChallenge, first_challenge)
                challenge.created_at = datetime.now(UTC) - timedelta(
                    seconds=policy.resend_cooldown_seconds + 1
                )
                await db.commit()
                req = await auth_service.request_login_otp(
                    db, phone=phones[1], client_ip=None
                )
                challenge_id = UUID(req["challenge_id"])
                code = provider.sent[challenge_id]
                account, token_a = await auth_service.verify_login(
                    db,
                    phone=phones[1],
                    challenge_id=challenge_id,
                    code=code,
                    rotate_token=None,
                )
                account_ids.append(account.id)
                assert account.phone == normalize_phone(phones[1])
                assert await session_service.resolve(db, token_a) is not None

                # Replay of the consumed challenge fails closed.
                with pytest.raises(ExpiredOtpCode):
                    await auth_service.verify_login(
                        db,
                        phone=phones[1],
                        challenge_id=challenge_id,
                        code=code,
                        rotate_token=None,
                    )

                # Session fixation: rotating invalidates the presented token.
                latest = await otp_service.repository.latest_challenge_for_phone(
                    db, policy.phone_hash(normalize_phone(phones[1]))
                )
                latest.created_at = datetime.now(UTC) - timedelta(
                    seconds=policy.resend_cooldown_seconds + 1
                )
                await db.commit()
                req = await auth_service.request_login_otp(
                    db, phone=phones[1], client_ip=None
                )
                challenge_id = UUID(req["challenge_id"])
                code = provider.sent[challenge_id]
                _, token_b = await auth_service.verify_login(
                    db,
                    phone=phones[1],
                    challenge_id=challenge_id,
                    code=code,
                    rotate_token=token_a,
                )
                assert await session_service.resolve(db, token_a) is None
                assert await session_service.resolve(db, token_b) is not None
                await session_service.revoke(db, token_b, reason="logout")
                await db.commit()
                assert await session_service.resolve(db, token_b) is None

                # Expiry is enforced server-side.
                req = await auth_service.request_login_otp(
                    db, phone=phones[2], client_ip=None
                )
                challenge_id = UUID(req["challenge_id"])
                challenge = await db.get(PatientOtpChallenge, challenge_id)
                challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                challenge.created_at = datetime.now(UTC) - timedelta(
                    seconds=policy.resend_cooldown_seconds + 1
                )
                await db.commit()
                with pytest.raises(ExpiredOtpCode):
                    await auth_service.verify_login(
                        db,
                        phone=phones[2],
                        challenge_id=challenge_id,
                        code=provider.sent.get(challenge_id, "000000"),
                        rotate_token=None,
                    )
        finally:
            async with factory() as db:
                await _cleanup(
                    db,
                    org_ids=[],
                    account_ids=account_ids,
                    patient_ids=[],
                    person_ids=[],
                    phone_hashes=phone_hashes,
                )
            await engine.dispose()

    asyncio.run(_exercise())


@requires_db
def test_patient_http_auth_cookie_and_csrf_db_semantics():
    assert ACTIVE_DB_URL is not None
    _upgrade_schema(ACTIVE_DB_URL)
    from fastapi.testclient import TestClient

    from src.app.api.v1 import patient as patient_module
    from src.app.core.db.database import async_engine as app_engine
    from src.app.main import app

    asyncio.run(app_engine.dispose())
    policy = _policy()
    provider = CapturingOtpProvider()
    phone = f"+91989{uuid4().int % 10**8:08d}"
    phone_hash = policy.phone_hash(normalize_phone(phone))
    # TestClient requests appear as client host "testclient"; include its IP
    # throttle key so cleanup leaves no synthetic rows behind.
    cleanup_hashes = {phone_hash, policy.key_hash("ip", "testclient")}

    original_provider = patient_module.patient_otp_service.provider
    original_secure = settings.PATIENT_COOKIE_SECURE
    original_bypass = settings.AUTH_LOCAL_DEV_BYPASS
    patient_module.patient_otp_service.provider = provider
    settings.PATIENT_COOKIE_SECURE = False
    settings.AUTH_LOCAL_DEV_BYPASS = False
    account_ids: list[UUID] = []

    async def _find_account_id() -> UUID | None:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            account = await db.scalar(
                select(PatientPortalAccount).where(
                    PatientPortalAccount.phone == normalize_phone(phone)
                )
            )
            account_id = account.id if account else None
        await engine.dispose()
        return account_id

    async def _cleanup_account():
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await _cleanup(
                db,
                org_ids=[],
                account_ids=account_ids,
                patient_ids=[],
                person_ids=[],
                phone_hashes=cleanup_hashes,
            )
        await engine.dispose()

    try:
        with TestClient(app) as client:
            requested = client.post(
                "/api/v1/patient/auth/otp/request", json={"phone": phone}
            )
            assert requested.status_code == 200, requested.text
            challenge_id = requested.json()["data"]["challenge_id"]
            code = provider.sent[UUID(challenge_id)]

            # A bogus bearer never yields a session by itself.
            denied = client.get(
                "/api/v1/patient/me",
                headers={"Authorization": "Bearer totally-bogus"},
            )
            assert denied.status_code == 401

            verified = client.post(
                "/api/v1/patient/auth/otp/verify",
                json={"phone": phone, "code": code, "challenge_id": challenge_id},
            )
            assert verified.status_code == 200, verified.text
            payload = verified.json()["data"]
            csrf = payload["session"]["csrf_token"]
            assert payload["patient"]["phone_masked"].endswith(phone[-4:])
            assert payload["link_status"] == "unlinked"
            cookie_name = payload["session"]["cookie_name"]
            assert cookie_name == settings.PATIENT_SESSION_COOKIE_NAME

            # Staff routes reject the patient cookie (dev bypass disabled).
            staff_denied = client.get("/api/v1/patient-links")
            assert staff_denied.status_code == 401

            me = client.get("/api/v1/patient/me")
            assert me.status_code == 200
            assert me.json()["data"]["link_status"] == "unlinked"

            # Cookie-authorized writes require CSRF; a bogus bearer cannot skip it.
            no_csrf = client.patch(
                "/api/v1/patient/profile",
                json={"first_name": "Synthetic"},
                headers={"Authorization": "Bearer totally-bogus"},
            )
            assert no_csrf.status_code == 403
            assert no_csrf.json()["error"]["code"] == "CSRF_VALIDATION_FAILED"

            updated = client.patch(
                "/api/v1/patient/profile",
                json={"first_name": "Synthetic", "date_of_birth": "1990-01-01"},
                headers={settings.PATIENT_CSRF_HEADER_NAME: csrf},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["patient"]["profile_complete"] is True

            logout = client.post(
                "/api/v1/patient/auth/logout",
                headers={settings.PATIENT_CSRF_HEADER_NAME: csrf},
            )
            assert logout.status_code == 204
            after = client.get("/api/v1/patient/me")
            assert after.status_code == 401
    finally:
        account_id = asyncio.run(_find_account_id())
        if account_id is not None:
            account_ids.append(account_id)
        asyncio.run(_cleanup_account())
        patient_module.patient_otp_service.provider = original_provider
        settings.PATIENT_COOKIE_SECURE = original_secure
        settings.AUTH_LOCAL_DEV_BYPASS = original_bypass
        asyncio.run(app_engine.dispose())


@requires_db
def test_staff_and_patient_cookie_coexistence_with_overlapping_identity():
    """Same browser, both cookies, same phone on a staff person and a portal account.

    Neither session authenticates the other namespace, and the shared phone never
    creates a record link automatically.
    """
    assert ACTIVE_DB_URL is not None
    _upgrade_schema(ACTIVE_DB_URL)
    from fastapi.testclient import TestClient

    from src.app.api.v1 import patient as patient_module
    from src.app.core.db.database import async_engine as app_engine
    from src.app.main import app

    asyncio.run(app_engine.dispose())
    policy = _policy()
    provider = CapturingOtpProvider()
    phone = f"+91986{uuid4().int % 10**8:08d}"
    phone_hash = policy.phone_hash(normalize_phone(phone))
    cleanup_hashes = {phone_hash, policy.key_hash("ip", "testclient")}
    original_provider = patient_module.patient_otp_service.provider
    original_secure = settings.PATIENT_COOKIE_SECURE
    original_bypass = settings.AUTH_LOCAL_DEV_BYPASS
    patient_module.patient_otp_service.provider = provider
    settings.PATIENT_COOKIE_SECURE = False
    settings.AUTH_LOCAL_DEV_BYPASS = False
    account_ids: list[UUID] = []
    org_ids: list[UUID] = []
    patient_ids: list[UUID] = []
    person_ids: list[UUID] = []
    staff_user_ids: list[UUID] = []
    staff_session_ids: list[str] = []

    async def _setup() -> str:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            org, _facility, person, patient = await _create_clinic(db, prefix="SYNTOVL")
            person.phone = normalize_phone(phone)
            staff_account = UserAccount(
                logto_user_id=f"local:overlap-{uuid4().hex}",
                email=f"overlap-{uuid4().hex[:8]}@example.com",
                display_name="Overlap Staff",
            )
            db.add(staff_account)
            await db.flush()
            session = AuthSession(
                id=f"overlap-session-{uuid4().hex}",
                user_account_id=staff_account.id,
                id_token="overlap-id-token",
                csrf_token="overlap-staff-csrf",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            db.add(session)
            await db.commit()
            org_ids.append(org.id)
            patient_ids.append(patient.id)
            person_ids.append(person.id)
            staff_user_ids.append(staff_account.id)
            staff_session_ids.append(session.id)
        await engine.dispose()
        return staff_session_ids[0]

    async def _find_account_id() -> UUID | None:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            account = await db.scalar(
                select(PatientPortalAccount).where(
                    PatientPortalAccount.phone == normalize_phone(phone)
                )
            )
            account_id = account.id if account else None
        await engine.dispose()
        return account_id

    async def _cleanup_all() -> None:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            if staff_session_ids:
                await db.execute(
                    delete(AuthSession).where(AuthSession.id.in_(staff_session_ids))
                )
            if staff_user_ids:
                await db.execute(
                    delete(UserAccount).where(UserAccount.id.in_(staff_user_ids))
                )
            await db.commit()
            await _cleanup(
                db,
                org_ids=org_ids,
                account_ids=account_ids,
                patient_ids=patient_ids,
                person_ids=person_ids,
                phone_hashes=cleanup_hashes,
            )
        await engine.dispose()

    try:
        staff_session_id = asyncio.run(_setup())
        with TestClient(app) as client:
            requested = client.post(
                "/api/v1/patient/auth/otp/request", json={"phone": phone}
            )
            assert requested.status_code == 200, requested.text
            challenge_id = requested.json()["data"]["challenge_id"]
            verified = client.post(
                "/api/v1/patient/auth/otp/verify",
                json={
                    "phone": phone,
                    "code": provider.sent[UUID(challenge_id)],
                    "challenge_id": challenge_id,
                },
            )
            assert verified.status_code == 200, verified.text
            # The staff person sharing this phone does not create a link.
            assert verified.json()["data"]["link_status"] == "unlinked"
            assert client.get("/api/v1/patient/me").status_code == 200

            client.cookies.clear()
            # A valid staff session works on staff routes only.
            staff_me = client.get(
                "/api/v1/auth/me", cookies={settings.AUTH_SESSION_COOKIE_NAME: staff_session_id}
            )
            assert staff_me.status_code == 200, staff_me.text
            client.cookies.clear()
            # Patient cookie never authenticates staff routes.
            client.cookies.set(settings.PATIENT_SESSION_COOKIE_NAME, "opaque")
            assert client.get("/api/v1/auth/me").status_code == 401
            client.cookies.clear()
            # Staff cookie never authenticates patient routes.
            client.cookies.set(settings.AUTH_SESSION_COOKIE_NAME, staff_session_id)
            assert client.get("/api/v1/patient/me").status_code == 401
    finally:
        account_id = asyncio.run(_find_account_id())
        if account_id is not None:
            account_ids.append(account_id)
        asyncio.run(_cleanup_all())
        patient_module.patient_otp_service.provider = original_provider
        settings.PATIENT_COOKIE_SECURE = original_secure
        settings.AUTH_LOCAL_DEV_BYPASS = original_bypass
        asyncio.run(app_engine.dispose())


@requires_db
def test_patient_invitation_and_link_db_semantics():
    assert ACTIVE_DB_URL is not None
    _upgrade_schema(ACTIVE_DB_URL)
    policy = _policy()
    link_service = PatientLinkService(
        pepper=policy.pepper,
        invitation_ttl_hours=settings.PATIENT_LINK_INVITATION_TTL_HOURS,
        invitation_max_ttl_hours=settings.PATIENT_LINK_INVITATION_MAX_TTL_HOURS,
    )

    async def _exercise() -> None:
        engine = create_async_engine(ACTIVE_DB_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        org_ids: list[UUID] = []
        account_ids: list[UUID] = []
        patient_ids: list[UUID] = []
        person_ids: list[UUID] = []
        phones = [f"+91988{uuid4().int % 10**8:08d}" for _ in range(4)]
        phone_hashes = {policy.phone_hash(normalize_phone(p)) for p in phones}
        try:
            async with factory() as db:
                org_a, facility_a, person_a, patient_a = await _create_clinic(
                    db, prefix="SYNTA"
                )
                org_b, facility_b, person_b, patient_b = await _create_clinic(
                    db, prefix="SYNTB"
                )
                org_ids += [org_a.id, org_b.id]
                patient_ids += [patient_a.id, patient_b.id]
                person_ids += [person_a.id, person_b.id]
                account_a = PatientPortalAccount(
                    phone=normalize_phone(phones[0]), first_name="Synthetic"
                )
                account_b = PatientPortalAccount(phone=normalize_phone(phones[1]))
                account_c = PatientPortalAccount(phone=normalize_phone(phones[2]))
                db.add_all([account_a, account_b, account_c])
                await db.flush()
                account_ids += [account_a.id, account_b.id, account_c.id]
                await db.commit()

                # Invitation is bound to the intended recipient phone.
                invitation, code = await link_service.create_invitation(
                    db,
                    organization=org_a,
                    patient=patient_a,
                    facility_id=facility_a.id,
                    phone=phones[0],
                    created_by_user_id=None,
                )
                assert invitation.status == "pending"
                with pytest.raises(InvitationInvalid):
                    await link_service.activate(db, account=account_b, token=code)
                link = await link_service.activate(db, account=account_a, token=code)
                assert link.status == "verified"
                assert link.patient_id == patient_a.id
                assert link.verification_source == "clinic_invitation"
                # Replay by the same account returns the same link (idempotent).
                replay = await link_service.activate(db, account=account_a, token=code)
                assert replay.id == link.id

                # Expired invitation cannot be activated.
                expired, expired_code = await link_service.create_invitation(
                    db,
                    organization=org_a,
                    patient=patient_a,
                    facility_id=None,
                    phone=phones[2],
                    created_by_user_id=None,
                )
                expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.commit()
                with pytest.raises(InvitationExpired):
                    await link_service.activate(
                        db, account=account_c, token=expired_code
                    )

                # Revoked invitation cannot be activated.
                revoked, revoked_code = await link_service.create_invitation(
                    db,
                    organization=org_a,
                    patient=patient_a,
                    facility_id=None,
                    phone=phones[2],
                    created_by_user_id=None,
                )
                await link_service.revoke_invitation(
                    db, invitation=revoked, actor_user_id=None
                )
                with pytest.raises(InvitationInvalid):
                    await link_service.activate(
                        db, account=account_c, token=revoked_code
                    )

                # Self-start request to a portal-enabled clinic.
                request_b = await link_service.request_link(
                    db,
                    account=account_b,
                    organization=org_b,
                    facility_id=facility_b.id,
                )
                assert request_b.status == "pending"
                assert request_b.patient_id is None
                # Name/DOB alone never resolves a patient; the staff choice does.
                with pytest.raises(ClinicalPatientInvalid):
                    await link_service.approve_request(
                        db,
                        link=request_b,
                        patient=patient_a,
                        actor_user_id=None,
                    )
                approved = await link_service.approve_request(
                    db,
                    link=request_b,
                    patient=patient_b,
                    actor_user_id=None,
                )
                assert approved.status == "verified"
                assert approved.patient_id == patient_b.id
                with pytest.raises(LinkAlreadyVerified):
                    await link_service.request_link(
                        db,
                        account=account_b,
                        organization=org_b,
                        facility_id=None,
                    )

                # Cross-organization scope: the same account is not linked into A.
                links_a = await link_service.list_links_for_organization(
                    db, organization_id=org_a.id
                )
                assert all(link.patient_account_id == account_a.id for link in links_a)
                assert await link_service.link_status(
                    db, account_id=account_c.id
                ) == "unlinked"

                await link_service.revoke_link(
                    db, link=approved, actor_user_id=None, reason="test"
                )
                assert await link_service.link_status(
                    db, account_id=account_b.id
                ) == "revoked"
                assert await link_service.link_status(
                    db, account_id=account_c.id
                ) == "unlinked"
        finally:
            async with factory() as db:
                await _cleanup(
                    db,
                    org_ids=org_ids,
                    account_ids=account_ids,
                    patient_ids=patient_ids,
                    person_ids=person_ids,
                    phone_hashes=phone_hashes,
                )
            await engine.dispose()

    asyncio.run(_exercise())
