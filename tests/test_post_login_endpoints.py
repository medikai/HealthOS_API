import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.app.api.v1.bootstrap import facilities, me
from src.app.api.v1.queue import _queue_query, queue_list
from src.app.api.v1.scheduling import (
    _appointment_query,
    _scope_query,
    list_appointments,
    practitioners,
)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _Database:
    def __init__(self, rows=()):
        self.rows = rows
        self.query_count = 0
        self.sql = ""

    async def execute(self, query):
        self.sql = str(query.compile(dialect=postgresql.dialect()))
        self.query_count += 1
        return _Result(self.rows)

    async def scalars(self, query):
        return await self.execute(query)


class PostLoginEndpointTests(unittest.IsolatedAsyncioTestCase):
    account = SimpleNamespace(
        id=UUID(int=1),
        logto_user_id="subject",
        display_name="Doctor",
        email="doctor@example.test",
    )

    async def test_me_uses_one_query_after_authentication(self):
        staff = SimpleNamespace(id=UUID(int=2))
        organization = SimpleNamespace(
            id=UUID(int=3), name="Clinic", logto_organization_id=None
        )
        practitioner = SimpleNamespace(
            id=UUID(int=4), person_name="Doctor", specialty="Medicine"
        )
        db = _Database([(staff, organization, practitioner, "practitioner")])

        result = await me(self.account, db)

        self.assertEqual(db.query_count + 1, 2)
        self.assertEqual(result["data"]["user"]["email"], "doctor@example.test")
        self.assertEqual(result["data"]["practitioner"]["specialization"], "Medicine")
        self.assertIn("patient:read", result["data"]["scopes"])
        # Verify strictly minimal frontend required fields
        self.assertEqual(
            set(result["data"]["user"].keys()), {"uuid", "display_name", "email"}
        )
        self.assertEqual(
            set(result["data"]["organization"].keys()),
            {"uuid", "name", "plan_code"},
        )
        self.assertEqual(
            set(result["data"]["staff"].keys()),
            {"uuid", "designation", "status", "role_codes"},
        )
        self.assertEqual(
            set(result["data"]["practitioner"].keys()),
            {"uuid", "name", "specialization"},
        )
        self.assertIn("scopes", result["data"])

    async def test_me_prevents_cross_tenant_role_leakage(self):
        staff_a = SimpleNamespace(id=UUID(int=2))
        org_a = SimpleNamespace(id=UUID(int=3), name="Clinic A")
        staff_b = SimpleNamespace(id=UUID(int=5))
        org_b = SimpleNamespace(id=UUID(int=6), name="Clinic B")
        practitioner = SimpleNamespace(
            id=UUID(int=4), person_name="Doctor", specialty="Medicine"
        )
        # Rows from hypothetical multi-org query
        db = _Database(
            [
                (staff_a, org_a, practitioner, "doctor"),
                (staff_b, org_b, practitioner, "organization_admin"),
            ]
        )

        result = await me(self.account, db)
        # Should only contain roles for org_a (the selected org), not org_b
        self.assertEqual(result["data"]["organization"]["uuid"], str(org_a.id))
        self.assertEqual(result["data"]["staff"]["role_codes"], ["doctor"])
        self.assertNotIn("organization_admin", result["data"]["staff"]["role_codes"])

    async def test_facilities_query_count_is_constant_as_facilities_grow(self):
        def row(number):
            return SimpleNamespace(
                facility_id=UUID(int=number + 10),
                organization_id=UUID(int=3),
                code=f"F{number}",
                name=f"Facility {number}",
                timezone="Asia/Kolkata",
                booked=number,
                queue=1,
                duty=2,
            )

        db = _Database([row(number) for number in range(50)])
        result = await facilities(self.account, db)

        self.assertEqual(db.query_count + 1, 2)
        self.assertEqual(result["meta"]["count"], 50)
        self.assertEqual(
            result["data"]["items"][0]["telemetry"]["capacity"], "0% Capacity"
        )
        self.assertIn("organization.staff_member.user_account_id", db.sql)
        # Verify single-tenant isolation anchoring subquery
        self.assertIn("organization.facility.organization_id =", db.sql)

    async def test_all_queries_are_tenant_and_assignment_scoped(self):
        organization_id, staff_id = UUID(int=20), UUID(int=21)
        for query in (
            _appointment_query(organization_id, staff_id),
            _queue_query(organization_id, staff_id),
        ):
            sql = str(
                query.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
            self.assertIn(str(organization_id), sql)
            self.assertIn(str(staff_id), sql)
            self.assertIn("EXISTS", sql)

    async def test_all_collection_endpoints_use_one_query_after_authentication(self):
        appointments_db = _Database()
        await list_appointments("all", self.account, appointments_db)
        self.assertEqual(appointments_db.query_count + 1, 2)

        queue_db = _Database()
        await queue_list("all", date.today(), self.account, queue_db)
        self.assertEqual(queue_db.query_count + 1, 2)

        practitioners_db = _Database()
        await practitioners(self.account, practitioners_db, "all")
        self.assertEqual(practitioners_db.query_count + 1, 2)

    async def test_explicit_cross_tenant_facility_is_rejected(self):
        denied = AsyncMock(
            side_effect=HTTPException(
                status_code=403, detail="Facility access is not permitted."
            )
        )
        facility_id = str(UUID(int=99))
        db = _Database()

        with (
            patch("src.app.api.v1.scheduling._scope", denied),
            self.assertRaises(HTTPException),
        ):
            await list_appointments(facility_id, self.account, db)
        with (
            patch("src.app.api.v1.queue._scope", denied),
            self.assertRaises(HTTPException),
        ):
            await queue_list(facility_id, account=self.account, db=db)
        with (
            patch("src.app.api.v1.scheduling._scope", denied),
            self.assertRaises(HTTPException),
        ):
            await practitioners(self.account, db, facility_id)

    async def test_scope_rejects_foreign_facility_uuid(self):
        # Database returns no match because facility does not belong to user's org/assignment
        empty_db = _Database(rows=[])
        foreign_facility_id = UUID(int=999)
        from src.app.api.v1.scheduling import _scope

        with self.assertRaises(HTTPException) as ctx:
            await _scope(empty_db, self.account, foreign_facility_id)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Facility access is not permitted.")

    async def test_all_endpoints_reject_unauthorized_facility_with_real_scope(self):
        empty_db = _Database(rows=[])
        foreign_facility_id = str(UUID(int=999))

        with self.assertRaises(HTTPException) as ctx:
            await list_appointments(foreign_facility_id, self.account, empty_db)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Facility access is not permitted.")

        with self.assertRaises(HTTPException) as ctx:
            await queue_list(foreign_facility_id, date.today(), self.account, empty_db)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Facility access is not permitted.")

        with self.assertRaises(HTTPException) as ctx:
            await practitioners(self.account, empty_db, foreign_facility_id)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "Facility access is not permitted.")

    def test_admin_scope_cannot_substitute_an_owned_facility_for_foreign_uuid(self):
        target = UUID(int=100)
        sql = str(
            _scope_query(self.account.id, target).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        self.assertIn(f"organization.facility.id = '{target}'", sql)
        self.assertNotIn(
            f"organization.facility.id = '{target}' OR organization.staff_assignment",
            sql,
        )


if __name__ == "__main__":
    unittest.main()
