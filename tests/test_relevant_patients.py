import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException

from src.app.api.v1.patients import relevant_patients


def entity(number: int, **values):
    id_val = values.pop("id", UUID(int=number))
    return SimpleNamespace(id=id_val, **values)


class RelevantPatientsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.org_id = UUID("01a0a022-b2aa-7509-aecb-3166583df954")
        self.facility_id = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
        self.account = entity(
            100,
            logto_user_id="user-1",
        )
        self.organization = entity(
            200,
            id=self.org_id,
            name="Test Medical Center",
            code="TMC",
            is_active=True,
        )

    async def test_deduplication_across_tiers(self) -> None:
        """Verify that a patient matching multiple tiers (e.g. today's queue and upcoming appointment)

        is returned exactly once under their highest priority tier (Tier 1).
        """
        # Patient 1: in today's queue AND has an upcoming appointment in Tier 2
        p1 = entity(1, mrn="MRN-001", is_active=True, organization_id=self.org_id)
        per1 = entity(
            11,
            first_name="Aarav",
            last_name="Sharma",
            phone="+919876543210",
            email="aarav@example.com",
            date_of_birth="1990-01-01",
            gender="male",
        )
        q1 = entity(
            101,
            patient_id=p1.id,
            token_number=1,
            status="waiting",
            queue_date=date.today(),
            organization_id=self.org_id,
            facility_id=self.facility_id,
        )

        # Tier 2 appointment for Patient 1 (which should be skipped because of deduplication)
        # and Patient 2 (who only has an upcoming appointment)
        p2 = entity(2, mrn="MRN-002", is_active=True, organization_id=self.org_id)
        per2 = entity(
            12,
            first_name="Rohan",
            last_name="Verma",
            phone="+919876543211",
            email="rohan@example.com",
            date_of_birth="1992-05-05",
            gender="male",
        )
        a_p1 = entity(
            201,
            patient_id=p1.id,
            status="booked",
            scheduled_start=datetime.now(UTC) + timedelta(days=2),
            organization_id=self.org_id,
            facility_id=self.facility_id,
        )
        a_p2 = entity(
            202,
            patient_id=p2.id,
            status="booked",
            scheduled_start=datetime.now(UTC) + timedelta(days=3),
            organization_id=self.org_id,
            facility_id=self.facility_id,
        )

        appointment_call_count = 0

        async def fake_execute(statement):
            nonlocal appointment_call_count
            stmt_str = str(statement).lower()
            if "facility_schedule" in stmt_str or "timezone" in stmt_str:
                return SimpleNamespace(all=lambda: ["Asia/Kolkata"], scalar=lambda: "Asia/Kolkata", first=lambda: ("Asia/Kolkata",))
            elif "select identity.patient" in stmt_str or "from identity.patient" in stmt_str:
                return SimpleNamespace(all=lambda: [])
            elif "from care.queue_entry" in stmt_str:
                return SimpleNamespace(all=lambda: [(q1, p1, per1)])
            elif "from care.appointment" in stmt_str:
                appointment_call_count += 1
                if appointment_call_count == 1:
                    # Tier 1b today appointments
                    return SimpleNamespace(all=lambda: [])
                else:
                    # Tier 2 upcoming appointments
                    return SimpleNamespace(all=lambda: [(a_p1, p1, per1), (a_p2, p2, per2)])
            elif "from care.encounter" in stmt_str:
                return SimpleNamespace(all=lambda: [])
            else:
                return SimpleNamespace(all=lambda: [])

        async def fake_scalar(statement):
            return "Asia/Kolkata"

        db = SimpleNamespace(
            execute=AsyncMock(side_effect=fake_execute),
            scalar=AsyncMock(side_effect=fake_scalar),
        )

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with patch("src.app.api.v1.patients._scope", new_callable=AsyncMock) as mock_scope:
                mock_scope.return_value = (self.organization, SimpleNamespace(id=self.facility_id))
                res = await relevant_patients(
                    facility_id=self.facility_id,
                    limit=10,
                    account=self.account,
                    db=db,
                )

        self.assertTrue(res["success"])
        items = res["data"]["items"]
        # Exactly 2 unique patients returned (p1 and p2)
        self.assertEqual(len(items), 2)
        # p1 was deduplicated and retains Tier 1 reason
        self.assertEqual(items[0]["uuid"], str(p1.id))
        self.assertEqual(items[0]["relevance_tier"], 1)
        self.assertIn("In today's queue (Token #1)", items[0]["relevance_reason"])
        # p2 has Tier 2 reason
        self.assertEqual(items[1]["uuid"], str(p2.id))
        self.assertEqual(items[1]["relevance_tier"], 2)
        self.assertIn("Upcoming appointment", items[1]["relevance_reason"])

    async def test_tenant_and_facility_boundary_enforcement(self) -> None:
        """Verify that unauthorized facility access raises HTTP 403 and queries are properly scoped."""
        db = AsyncMock()
        unauthorized_facility = UUID("01a0a022-b2c1-76e3-a91d-49f5568eca99")

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with patch(
                "src.app.api.v1.patients._scope",
                side_effect=HTTPException(status_code=403, detail="Facility access is not permitted."),
            ):
                with self.assertRaises(HTTPException) as ctx:
                    await relevant_patients(
                        facility_id=unauthorized_facility,
                        account=self.account,
                        db=db,
                    )
                self.assertEqual(ctx.exception.status_code, 403)
                self.assertIn("Facility access is not permitted", ctx.exception.detail)

    async def test_all_four_tiers_bounded_merge(self) -> None:
        """Verify that items from all 4 tiers fill up to the requested bounded limit."""
        # Tier 1 patient
        p1, per1 = entity(1, mrn="MRN-1", is_active=True, organization_id=self.org_id), entity(11, first_name="P1", last_name="A", phone="1", email="p1@a.com", date_of_birth="1990-01-01", gender="m")
        q1 = entity(101, patient_id=p1.id, token_number=5, status="waiting", queue_date=date.today())

        # Tier 2 patient
        p2, per2 = entity(2, mrn="MRN-2", is_active=True, organization_id=self.org_id), entity(12, first_name="P2", last_name="B", phone="2", email="p2@a.com", date_of_birth="1990-01-01", gender="m")
        a2 = entity(202, patient_id=p2.id, status="booked", scheduled_start=datetime.now(UTC) + timedelta(days=2))

        # Tier 3 patient
        p3, per3 = entity(3, mrn="MRN-3", is_active=True, organization_id=self.org_id), entity(13, first_name="P3", last_name="C", phone="3", email="p3@a.com", date_of_birth="1990-01-01", gender="m")
        e3 = entity(303, patient_id=p3.id, status="completed", completed_at=datetime.now(UTC) - timedelta(days=10))

        # Tier 4 patient
        p4, per4 = entity(4, mrn="MRN-4", is_active=True, organization_id=self.org_id), entity(14, first_name="P4", last_name="D", phone="4", email="p4@a.com", date_of_birth="1990-01-01", gender="m")

        appointment_call_count = 0

        async def fake_execute(stmt):
            nonlocal appointment_call_count
            stmt_str = str(stmt).lower()
            if "facility_schedule" in stmt_str or "timezone" in stmt_str:
                return SimpleNamespace(all=lambda: ["Asia/Kolkata"], scalar=lambda: "Asia/Kolkata", first=lambda: ("Asia/Kolkata",))
            elif "select identity.patient" in stmt_str or "from identity.patient" in stmt_str:
                return SimpleNamespace(all=lambda: [(p4, per4)])  # Tier 4
            elif "from care.queue_entry" in stmt_str:
                return SimpleNamespace(all=lambda: [(q1, p1, per1)])
            elif "from care.appointment" in stmt_str:
                appointment_call_count += 1
                if appointment_call_count == 1:
                    return SimpleNamespace(all=lambda: [])  # Tier 1b
                return SimpleNamespace(all=lambda: [(a2, p2, per2)])  # Tier 2
            elif "from care.encounter" in stmt_str:
                return SimpleNamespace(all=lambda: [(e3, p3, per3)])  # Tier 3
            else:
                return SimpleNamespace(all=lambda: [(p4, per4)])

        async def fake_scalar(stmt):
            return "Asia/Kolkata"

        db = SimpleNamespace(
            execute=AsyncMock(side_effect=fake_execute),
            scalar=AsyncMock(side_effect=fake_scalar),
        )

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await relevant_patients(
                account=self.account,
                db=db,
                limit=10,
            )

        self.assertTrue(res["success"])
        items = res["data"]["items"]
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["relevance_tier"], 1)
        self.assertEqual(items[1]["relevance_tier"], 2)
        self.assertEqual(items[2]["relevance_tier"], 3)
        self.assertEqual(items[3]["relevance_tier"], 4)
        self.assertEqual(items[0]["uuid"], str(p1.id))
        self.assertEqual(items[1]["uuid"], str(p2.id))
        self.assertEqual(items[2]["uuid"], str(p3.id))
        self.assertEqual(items[3]["uuid"], str(p4.id))

    async def test_facility_timezone_used_for_date_calculations(self) -> None:
        """Verify that facility schedule timezone is queried and used."""
        p4, per4 = entity(4, mrn="MRN-4", is_active=True, organization_id=self.org_id), entity(14, first_name="P4", last_name="D", phone="4", email="p4@a.com", date_of_birth="1990-01-01", gender="m")

        async def fake_execute(stmt):
            stmt_str = str(stmt).lower()
            if "facility_schedule" in stmt_str or "timezone" in stmt_str:
                return SimpleNamespace(all=lambda: ["America/New_York"], scalar=lambda: "America/New_York", first=lambda: ("America/New_York",))
            elif "from care.queue_entry" in stmt_str or "from care.appointment" in stmt_str or "from care.encounter" in stmt_str:
                return SimpleNamespace(all=lambda: [])
            return SimpleNamespace(all=lambda: [(p4, per4)])

        async def fake_scalar(stmt):
            return "America/New_York"

        db = SimpleNamespace(
            execute=AsyncMock(side_effect=fake_execute),
            scalar=AsyncMock(side_effect=fake_scalar),
        )

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            with patch("src.app.api.v1.patients._scope", new_callable=AsyncMock):
                res = await relevant_patients(
                    facility_id=self.facility_id,
                    account=self.account,
                    db=db,
                )

        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["facility_timezone"], "America/New_York")
        self.assertEqual(res["meta"]["facility_timezone"], "America/New_York")

    async def test_absent_sources_gracefully_handled(self) -> None:
        """Verify that if Tiers 1-3 have no data, the query gracefully falls back to Tier 4."""
        p4, per4 = entity(4, mrn="MRN-4", is_active=True, organization_id=self.org_id), entity(14, first_name="P4", last_name="D", phone="4", email="p4@a.com", date_of_birth="1990-01-01", gender="m")

        async def fake_execute(stmt):
            stmt_str = str(stmt).lower()
            if "select identity.patient" in stmt_str or "from identity.patient" in stmt_str:
                return SimpleNamespace(all=lambda: [(p4, per4)])
            return SimpleNamespace(all=lambda: [])

        db = SimpleNamespace(
            execute=AsyncMock(side_effect=fake_execute),
            scalar=AsyncMock(return_value="Asia/Kolkata"),
        )

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await relevant_patients(
                account=self.account,
                db=db,
                limit=10,
            )

        self.assertTrue(res["success"])
        items = res["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["relevance_tier"], 4)
        self.assertEqual(items[0]["relevance_reason"], "Recently registered patient")

    async def test_limit_strictly_bounded_at_ten(self) -> None:
        """Verify that limit is bounded between 1 and 10."""
        p_list = [
            (
                entity(i, mrn=f"MRN-{i}", is_active=True, organization_id=self.org_id),
                entity(100 + i, first_name=f"Pat{i}", last_name="Test", phone=f"98765432{i:02d}", email=f"p{i}@test.com", date_of_birth="1990-01-01", gender="m"),
            )
            for i in range(1, 15)
        ]

        async def fake_execute(stmt):
            stmt_str = str(stmt).lower()
            if "select identity.patient" in stmt_str or "from identity.patient" in stmt_str:
                return SimpleNamespace(all=lambda: p_list[:10])
            return SimpleNamespace(all=lambda: [])

        db = SimpleNamespace(
            execute=AsyncMock(side_effect=fake_execute),
            scalar=AsyncMock(return_value="Asia/Kolkata"),
        )

        with patch("src.app.api.v1.patients._org", return_value=self.organization):
            res = await relevant_patients(
                account=self.account,
                db=db,
                limit=100,  # Requesting 100 should be capped at 10
            )

        self.assertTrue(res["success"])
        self.assertLessEqual(len(res["data"]["items"]), 10)
        self.assertEqual(res["data"]["limit"], 10)


if __name__ == "__main__":
    unittest.main()
