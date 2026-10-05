import importlib
import unittest
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import ValidationError

from src.app.api.v1.masters import FALLBACK_SALUTATIONS, list_salutations
from src.app.api.v1.staff import accept_invitation
from src.app.domains.identity.salutations import (
    SALUTATIONS,
    _salutation_table,
    resolve_active_salutation,
    salutation_uuid,
    seed_salutations,
)
from src.app.domains.organization.service import _split_person_name, access_service
from src.app.models.identity import Person, Salutation, UserAccount
from src.app.models.organization import (
    FacilitySchedule,
    StaffAssignment,
    StaffInvitation,
    StaffMember,
)
from src.app.schemas.access import OrganizationCreate
from src.app.schemas.patients import PatientCreate
from src.app.schemas.staff import StaffInviteAccept, StaffInviteCreate

MIGRATION = importlib.import_module("src.migrations.versions.20260929_41_add_salutation_master")

EXPECTED_CODES = ["DR", "MR", "MS", "MRS", "MX", "PROF"]


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def all(self):
        return self.rows


class _ListDb:
    def __init__(self, rows=(), error=False):
        self.rows = rows
        self.error = error

    async def scalars(self, query):
        if self.error:
            raise RuntimeError("database unavailable")
        return _Rows(self.rows)


class _EntityDb:
    def __init__(self, entities=None):
        self.entities = entities or {}

    async def get(self, entity, ident):
        return self.entities.get((entity, ident))


def _salutation(code="DR", active=True):
    return SimpleNamespace(
        id=salutation_uuid(code),
        code=code,
        display_name="Doctor",
        abbreviation="Dr.",
        sort_order=10,
        is_active=active,
    )


class SeedTests(unittest.TestCase):
    def test_migration_revision_chain_and_codes(self):
        self.assertEqual(MIGRATION.revision, "20260929_41")
        self.assertEqual(MIGRATION.down_revision, "20260925_40")
        self.assertEqual([row[0] for row in MIGRATION.SALUTATIONS], EXPECTED_CODES)

    def test_migration_seed_matches_domain_seed(self):
        self.assertEqual(
            [(r["code"], r["display_name"], r["abbreviation"], r["sort_order"]) for r in SALUTATIONS],
            list(MIGRATION.SALUTATIONS),
        )
        self.assertEqual([row["code"] for row in SALUTATIONS], EXPECTED_CODES)

    def test_deterministic_salutation_uuid(self):
        self.assertEqual(salutation_uuid("DR"), salutation_uuid("DR"))
        self.assertNotEqual(salutation_uuid("DR"), salutation_uuid("MR"))

    def test_seed_is_idempotent_and_creates_expected_codes(self):
        engine = sa.create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                _salutation_table(None).create(connection)
                first = seed_salutations(connection, schema=None)
                second = seed_salutations(connection, schema=None)
                rows = connection.execute(
                    sa.select(_salutation_table(None).c.code).order_by(_salutation_table(None).c.sort_order)
                ).scalars().all()
            self.assertEqual(first, 6)
            self.assertEqual(second, 0)
            self.assertEqual(list(rows), EXPECTED_CODES)
        finally:
            engine.dispose()


class MasterEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_active_salutations_in_sort_order(self):
        rows = [_salutation("DR"), _salutation("MR")]
        result = await list_salutations(db=_ListDb(rows))
        self.assertTrue(result["success"])
        self.assertEqual([item["code"] for item in result["data"]["items"]], ["DR", "MR"])
        self.assertEqual(result["data"]["items"][0]["id"], str(salutation_uuid("DR")))
        self.assertEqual(result["data"]["items"][0]["abbreviation"], "Dr.")
        self.assertEqual(result["meta"]["count"], 2)

    async def test_falls_back_to_reference_list_when_unavailable(self):
        result = await list_salutations(db=_ListDb(error=True))
        self.assertEqual(result["meta"]["count"], 6)
        self.assertIn("DR", {item["code"] for item in FALLBACK_SALUTATIONS})


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_active_salutation(self):
        record = _salutation()
        resolved = await resolve_active_salutation(_EntityDb({(Salutation, record.id): record}), record.id)
        self.assertIs(resolved, record)

    async def test_none_salutation_is_accepted(self):
        self.assertIsNone(await resolve_active_salutation(_EntityDb(), None))

    async def test_missing_salutation_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await resolve_active_salutation(_EntityDb(), uuid_pkg.uuid4())
        self.assertEqual(ctx.exception.status_code, 422)

    async def test_inactive_salutation_rejected(self):
        record = _salutation(active=False)
        with self.assertRaises(HTTPException) as ctx:
            await resolve_active_salutation(_EntityDb({(Salutation, record.id): record}), record.id)
        self.assertEqual(ctx.exception.status_code, 422)


class SchemaContractTests(unittest.TestCase):
    def test_inputs_accept_nullable_salutation(self):
        self.assertIsNone(PatientCreate(first_name="Vikram").salutation_id)
        self.assertIsNone(StaffInviteCreate(email="a@b.com", full_name="Vikram Sen").salutation_id)
        self.assertIsNone(OrganizationCreate(name="Clinic", code="clinic").salutation_id)

    def test_inputs_accept_valid_salutation_uuid(self):
        sid = salutation_uuid("DR")
        self.assertEqual(PatientCreate(first_name="Vikram", salutation_id=sid).salutation_id, sid)
        self.assertEqual(
            StaffInviteCreate(email="a@b.com", full_name="Vikram Sen", salutation_id=sid).salutation_id, sid
        )
        self.assertEqual(OrganizationCreate(name="Clinic", code="clinic", salutation_id=sid).salutation_id, sid)

    def test_malformed_uuid_is_a_validation_error(self):
        for factory in (
            lambda: PatientCreate(first_name="Vikram", salutation_id="not-a-uuid"),
            lambda: StaffInviteCreate(email="a@b.com", full_name="Vikram", salutation_id="not-a-uuid"),
            lambda: OrganizationCreate(name="Clinic", code="clinic", salutation_id="not-a-uuid"),
        ):
            with self.assertRaises(ValidationError):
                factory()


def _split(name):
    return _split_person_name(name)


class NameStorageTests(unittest.TestCase):
    def test_name_is_not_prefixed_or_stripped(self):
        self.assertEqual(_split("Vikram Sen"), ("Vikram", "Sen"))
        # Legacy prefixes are preserved, never silently normalized.
        self.assertEqual(_split("Dr. Vikram Sen"), ("Dr.", "Vikram Sen"))
        self.assertEqual(_split(""), ("Doctor", None))


class _OrgDb:
    def __init__(self, salutation=None):
        self.salutation = salutation
        self.added = []
        self.committed = False
        self._id = uuid_pkg.uuid4()

    async def scalar(self, statement):
        return

    async def get(self, entity, ident):
        if entity is Salutation:
            return self.salutation
        return None

    def _next_id(self):
        self._id = uuid_pkg.uuid4()
        return self._id

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = self._next_id()
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


class OnboardingPersistenceTests(unittest.IsolatedAsyncioTestCase):
    account = SimpleNamespace(
        id=UUID(int=1),
        person_id=None,
        display_name="Vikram Sen",
        logto_user_id="local:v@example.com",
    )

    async def _create(self, salutation_id):
        salutation = _salutation() if salutation_id else None
        db = _OrgDb(salutation)
        with patch(
            "src.app.domains.organization.service.rules_from_facility_schedule",
            lambda *args, **kwargs: [],
        ):
            await access_service.create_organization(
                db,
                self.account,
                "Vikram Clinic",
                "vikram_clinic",
                salutation_id=salutation_id,
            )
        return db

    async def test_onboarding_persists_salutation_on_person(self):
        db = await self._create(salutation_uuid("DR"))
        people = [obj for obj in db.added if isinstance(obj, Person)]
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0].salutation_id, salutation_uuid("DR"))
        self.assertEqual(people[0].first_name, "Vikram")
        self.assertEqual(people[0].last_name, "Sen")

    async def test_onboarding_without_salutation_stays_null(self):
        db = await self._create(None)
        people = [obj for obj in db.added if isinstance(obj, Person)]
        self.assertEqual(len(people), 1)
        self.assertIsNone(people[0].salutation_id)

    async def test_onboarding_never_prefixes_practitioner_name(self):
        db = await self._create(salutation_uuid("DR"))
        from src.app.models.care import Practitioner

        practitioners = [obj for obj in db.added if isinstance(obj, Practitioner)]
        self.assertEqual(practitioners[0].person_name, "Vikram Sen")


class _InvitationDb:
    def __init__(self, invitation):
        self.invitation = invitation
        self.added = []
        self.add = Mock(side_effect=self.added.append)
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is StaffInvitation:
            return self.invitation
        if entity is FacilitySchedule:
            return SimpleNamespace(
                timezone="Asia/Kolkata",
                days_of_week='["monday"]',
                operating_start="08:00",
                operating_end="20:00",
                slot_interval_minutes=30,
            )
        if entity in {
            UserAccount,
            StaffMember,
            StaffAssignment,
            Person,
        }:
            return None
        return None


class StaffActivationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_persists_salutation_on_person(self):
        invitation = StaffInvitation(
            organization_id=uuid_pkg.uuid4(),
            facility_id=uuid_pkg.uuid4(),
            email="new.doctor@example.com",
            full_name="Vikram Sen",
            role_code="practitioner",
            salutation_id=salutation_uuid("DR"),
            token="token",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db = _InvitationDb(invitation)
        with (
            patch("src.app.api.v1.staff.get_password_hash", return_value="hash"),
            patch("src.app.api.v1.staff.record_audit", AsyncMock()),
            patch("src.app.api.v1.staff.create_access_token", AsyncMock(return_value="access")),
        ):
            await accept_invitation(StaffInviteAccept(token="token", password="password"), db)

        people = [obj for obj in db.added if isinstance(obj, Person)]
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0].salutation_id, salutation_uuid("DR"))
        self.assertEqual(invitation.status, "accepted")

    async def test_activation_without_salutation_remains_supported(self):
        invitation = StaffInvitation(
            organization_id=uuid_pkg.uuid4(),
            facility_id=uuid_pkg.uuid4(),
            email="plain.staff@example.com",
            full_name="Plain Staff",
            role_code="nurse",
            token="token2",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db = _InvitationDb(invitation)
        with (
            patch("src.app.api.v1.staff.get_password_hash", return_value="hash"),
            patch("src.app.api.v1.staff.record_audit", AsyncMock()),
            patch("src.app.api.v1.staff.create_access_token", AsyncMock(return_value="access")),
        ):
            await accept_invitation(StaffInviteAccept(token="token2", password="password"), db)

        people = [obj for obj in db.added if isinstance(obj, Person)]
        self.assertIsNone(people[0].salutation_id)


if __name__ == "__main__":
    unittest.main()
