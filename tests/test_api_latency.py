import unittest
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy.dialects import postgresql

from src.app.api.v1.clinical import (
    get_clinical_documentation_settings,
    get_encounter_prescription,
    get_soap,
)
from src.app.api.v1.dashboard import facility_stats


class _Result:
    def __init__(self, *, first=None, rows=None, one=None):
        self._first = first
        self._rows = rows or []
        self._one = one

    def first(self):
        return self._first

    def all(self):
        return self._rows

    def one(self):
        return self._one


class _Database:
    def __init__(self, execute_results, scalar_results=()):
        self.execute_results = list(execute_results)
        self.scalar_results = list(scalar_results)
        self.query_count = 0

    async def execute(self, query):
        query.compile(dialect=postgresql.dialect())
        self.query_count += 1
        return self.execute_results.pop(0)

    async def scalar(self, query):
        query.compile(dialect=postgresql.dialect())
        self.query_count += 1
        return self.scalar_results.pop(0)


class ApiLatencyQueryCountTests(unittest.IsolatedAsyncioTestCase):
    account = SimpleNamespace(id=UUID(int=10))
    encounter = SimpleNamespace(id=UUID(int=20), status="in_progress")

    async def test_affected_endpoints_use_at_most_three_queries_including_auth(self):
        soap_db = _Database([_Result(first=(self.encounter, None, True))])
        await get_soap(self.encounter.id, self.account, soap_db)
        self.assertLessEqual(soap_db.query_count + 1, 3)

        prescription = SimpleNamespace(
            id=UUID(int=30),
            encounter_id=self.encounter.id,
            status="draft",
            advice=None,
            signed_at=None,
        )
        item = SimpleNamespace(
            id=UUID(int=40),
            medicine_name="Paracetamol",
            dosage="500 mg",
            frequency="BID",
            duration="3 days",
            strength=None,
            brand=None,
            route=None,
            timing=None,
            instructions=None,
        )
        prescription_db = _Database(
            [
                _Result(first=(self.encounter, True)),
                _Result(rows=[(prescription, item)]),
            ]
        )
        await get_encounter_prescription(self.encounter.id, self.account, prescription_db)
        self.assertLessEqual(prescription_db.query_count + 1, 3)

        facility_id, organization_id = UUID(int=50), UUID(int=60)
        settings_db = _Database(
            [_Result(rows=[(organization_id, "organization_admin", facility_id, facility_id)])],
            [None],
        )
        await get_clinical_documentation_settings(self.account, settings_db, facility_id)
        self.assertLessEqual(settings_db.query_count + 1, 3)

        metrics = SimpleNamespace(
            scheduled_today=0,
            confirmed_count=0,
            pending_count=0,
            completed_today=0,
            active_doctors_count=0,
            in_waiting_room=0,
            in_consultation=0,
            walk_ins_today=0,
            walk_ins_waiting=0,
        )
        stats_db = _Database(
            [
                _Result(
                    first=(
                        SimpleNamespace(id=organization_id),
                        SimpleNamespace(id=facility_id),
                        "Asia/Kolkata",
                    )
                ),
                _Result(one=metrics),
            ]
        )
        await facility_stats(str(facility_id), self.account, stats_db)
        self.assertLessEqual(stats_db.query_count + 1, 3)


if __name__ == "__main__":
    unittest.main()
