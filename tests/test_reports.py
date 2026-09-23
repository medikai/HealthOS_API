import sqlite3
import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from src.app.api.v1.reports import visits_report
from src.app.models.care import Encounter
from src.app.models.identity import Person


class _Result:
    def __init__(self, *, first=None, one=None, rows=()):
        self._first = first
        self._one = one
        self._rows = list(rows)

    def first(self):
        return self._first

    def one(self):
        return self._one

    def all(self):
        return self._rows


class ReportTests(unittest.IsolatedAsyncioTestCase):
    account = SimpleNamespace(id=UUID(int=1))
    organization = SimpleNamespace(id=UUID(int=2))
    facility = SimpleNamespace(id=UUID(int=3))

    async def test_range_summary_and_page_are_independent(self) -> None:
        encounter = SimpleNamespace(
            id=UUID(int=10),
            started_at=datetime(2026, 9, 23, 4, 30, tzinfo=UTC),
            completed_at=datetime(2026, 9, 23, 5, 0, tzinfo=UTC),
            status="completed",
        )
        patient = SimpleNamespace(id=UUID(int=11), mrn="MRN-001")
        person = SimpleNamespace(first_name="Example", last_name="Patient")
        practitioner = SimpleNamespace(id=UUID(int=12), person_name="Example Doctor")
        appointment = SimpleNamespace(id=UUID(int=13), status="completed")
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[self.organization, "INR", 3]),
            execute=AsyncMock(
                side_effect=[
                    _Result(first=(self.facility, "Asia/Kolkata")),
                    _Result(
                        one=SimpleNamespace(
                            visits_total=3,
                            completed_visits=2,
                            unique_visited_patients=2,
                            unique_patients_all_visits=3,
                        )
                    ),
                    _Result(
                        one=SimpleNamespace(
                            scheduled_appointments=4,
                            cancelled_appointments=1,
                            no_show_appointments=1,
                        )
                    ),
                    _Result(rows=[("INR", 100_000)]),
                    _Result(rows=[("INR", 60_000)]),
                    _Result(rows=[("INR", 10_000)]),
                    _Result(rows=[("INR", 100_000)]),
                    _Result(rows=[("INR", 60_000)]),
                    _Result(rows=[("INR", 10_000)]),
                    _Result(
                        rows=[(encounter, patient, person, practitioner, appointment)]
                    ),
                ]
            ),
        )

        response = await visits_report(
            facility_uuid=self.facility.id,
            date_from=date(2026, 9, 23),
            date_to=date(2026, 9, 23),
            account=self.account,
            db=db,
            patient_query=None,
            page=2,
            page_size=1,
        )

        self.assertEqual(response["data"]["metrics"]["visits_total"], 3)
        self.assertEqual(response["data"]["metrics"]["unique_patients_all_visits"], 3)
        self.assertEqual(
            response["meta"], {"page": 2, "page_size": 1, "total": 3, "total_pages": 3}
        )
        self.assertEqual(len(response["data"]["items"]), 1)
        self.assertEqual(
            response["data"]["range"]["utc_from"], "2026-09-22T18:30:00+00:00"
        )
        self.assertEqual(
            response["data"]["range"]["utc_to_exclusive"], "2026-09-23T18:30:00+00:00"
        )
        self.assertTrue(response["data"]["availability"]["money"])
        self.assertEqual(response["data"]["availability"]["amount_unit"], "minor")
        self.assertEqual(response["data"]["metrics"]["payments_received"], 60_000)
        self.assertEqual(response["data"]["metrics"]["refunds"], 10_000)
        self.assertEqual(response["data"]["metrics"]["net_collections"], 50_000)

        sql = "\n".join(
            str(call.args[0].compile(dialect=postgresql.dialect()))
            for call in db.execute.await_args_list[1:]
        )
        self.assertNotIn("CAST(care.encounter.started_at", sql)
        self.assertIn("care.encounter.started_at >=", sql)
        self.assertIn("care.encounter.started_at <", sql)
        self.assertIn(" LIMIT ", sql)

        payment_sql = str(
            db.execute.await_args_list[4].args[0].compile(dialect=postgresql.dialect())
        )
        refund_sql = str(
            db.execute.await_args_list[5].args[0].compile(dialect=postgresql.dialect())
        )
        self.assertIn("sum(care.payment.amount_minor)", payment_sql)
        self.assertNotIn("JOIN care.refund", payment_sql)
        self.assertIn("care.refund.refunded_at >=", refund_sql)
        self.assertIn("care.refund.refunded_at <", refund_sql)

        # Changing the page changes only OFFSET; ordering keeps a UUID tie-breaker.
        page_two_query = db.execute.await_args_list[-1].args[0]
        page_two_sql = str(page_two_query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        page_one_sql = str(page_two_query.offset(0).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.assertIn("ORDER BY care.encounter.started_at DESC NULLS LAST, care.encounter.id DESC", page_two_sql)
        self.assertIn("OFFSET 1", page_two_sql)
        self.assertIn("ORDER BY care.encounter.started_at DESC NULLS LAST, care.encounter.id DESC", page_one_sql)
        self.assertIn("OFFSET 0", page_one_sql)

    async def test_daily_range_ignores_page(self) -> None:
        aggregate = {"days": {date(2026, 9, 22): {"visits_total": 2, "unique_patients_all_visits": 2},
                              date(2026, 9, 23): {"visits_total": 1, "unique_patients_all_visits": 1}},
                     "metrics": {"visits_total": 3}, "money_unavailable_reason": None}
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[self.organization, "INR", 3]),
            execute=AsyncMock(side_effect=[
                _Result(first=(self.facility, "Asia/Kolkata")),
                _Result(one=SimpleNamespace(visits_total=3, completed_visits=2,
                                            unique_visited_patients=1, unique_patients_all_visits=2)),
                _Result(one=SimpleNamespace(scheduled_appointments=0, cancelled_appointments=0, no_show_appointments=0)),
                *[_Result(rows=[]) for _ in range(7)],
            ]),
        )
        with patch("src.app.api.v1.reports._weekly_metrics", new_callable=AsyncMock, return_value=aggregate) as weekly:
            response = await visits_report(self.facility.id, date(2026, 9, 22), date(2026, 9, 23),
                                           self.account, db, patient_query=None, page=2, page_size=1,
                                           include_daily=True, sort_by="patient_name", sort_order="asc")
        self.assertEqual([day["metrics"]["visits_total"] for day in response["data"]["daily"]], [2, 1])
        self.assertEqual(sum(day["metrics"]["visits_total"] for day in response["data"]["daily"]), response["data"]["metrics"]["visits_total"])
        self.assertEqual(response["data"]["metrics"]["unique_patients_all_visits"], 2)
        self.assertNotEqual(sum(day["metrics"]["unique_patients_all_visits"] for day in response["data"]["daily"]), response["data"]["metrics"]["unique_patients_all_visits"])
        self.assertEqual(response["meta"]["total"], 3)
        self.assertEqual(weekly.await_args.kwargs["breakdown"], True)
        sql = str(db.execute.await_args_list[-1].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("lower(identity.person.first_name", sql)
        self.assertIn("NULLS LAST, care.encounter.id DESC", sql)

        # Execute the endpoint's sort clauses across two pages with a tie at the boundary.
        order = db.execute.await_args_list[-1].args[0]._order_by_clauses
        with sqlite3.connect(":memory:") as connection:
            connection.execute("ATTACH DATABASE ':memory:' AS care")
            connection.execute("ATTACH DATABASE ':memory:' AS identity")
            connection.execute("CREATE TABLE care.encounter (id TEXT, patient_id TEXT)")
            connection.execute("CREATE TABLE identity.person (id TEXT, first_name TEXT, last_name TEXT)")
            for number in (1, 2, 3):
                connection.execute("INSERT INTO care.encounter VALUES (?, ?)", (str(UUID(int=number)), str(UUID(int=number))))
                connection.execute("INSERT INTO identity.person VALUES (?, 'Amy', '')", (str(UUID(int=number)),))
            query = select(Encounter.id).join(Person, Person.id == Encounter.patient_id).order_by(*order)
            def page(offset):
                statement = str(query.limit(2).offset(offset).compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
                return [value for (value,) in connection.execute(statement)]
            self.assertEqual(page(0) + page(2), [str(UUID(int=n)) for n in (3, 2, 1)])

    async def test_non_admin_is_denied_before_report_queries(self) -> None:
        db = SimpleNamespace(scalar=AsyncMock(return_value=None), execute=AsyncMock())

        with self.assertRaises(HTTPException) as raised:
            await visits_report(
                facility_uuid=self.facility.id,
                date_from=date(2026, 9, 23),
                date_to=date(2026, 9, 23),
                account=self.account,
                db=db,
                patient_query=None,
                page=1,
                page_size=25,
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail["code"], "REPORT_ACCESS_REQUIRED")
        db.execute.assert_not_awaited()

    async def test_rejects_unbounded_range(self) -> None:
        db = SimpleNamespace(scalar=AsyncMock(), execute=AsyncMock())

        with self.assertRaises(HTTPException) as raised:
            await visits_report(
                facility_uuid=self.facility.id,
                date_from=date(2025, 1, 1),
                date_to=date(2026, 9, 23),
                account=self.account,
                db=db,
                patient_query=None,
                page=1,
                page_size=25,
            )

        self.assertEqual(raised.exception.detail["code"], "REPORT_RANGE_TOO_LARGE")
        db.scalar.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class WeeklyTests(unittest.IsolatedAsyncioTestCase):
    async def test_week_boundaries_and_equivalent_previous_period(self) -> None:
        from src.app.api.v1.reports import weekly_overview

        organization = SimpleNamespace(id=UUID(int=2))
        facility = SimpleNamespace(id=UUID(int=3))
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[organization, "INR"]),
                             execute=AsyncMock(return_value=_Result(first=(facility, "Asia/Kolkata"))))
        metrics = {"visits_total": 2, "completed_visits": 2,
                   "unique_visited_patients": 1, "unique_patients_all_visits": 2, "scheduled_appointments": 2,
                   "cancelled_appointments": 0, "no_show_appointments": 0,
                   "billed_amount": 100, "payments_received": 80,
                   "refunds": 10, "net_collections": 70, "currency": "INR"}
        result = {"metrics": metrics, "money_unavailable_reason": None,
                  "days": {}, "practitioners": {}}
        with patch("src.app.api.v1.reports._weekly_metrics", new_callable=AsyncMock, return_value=result) as aggregate:
            response = await weekly_overview(
                facility_uuid=facility.id, week_of=date(2026, 9, 21),
                account=SimpleNamespace(id=UUID(int=1)), db=db,
                practitioner_uuid=None, patient_query=None,
            )
        data = response["data"]
        self.assertEqual(data["week"]["local_from"], "2026-09-21")
        self.assertEqual(data["week"]["utc_from"], "2026-09-20T18:30:00+00:00")
        self.assertEqual(data["previous_period"]["local_from"], "2026-09-14")
        self.assertEqual(len(data["daily"]), 7)
        self.assertEqual(aggregate.await_count, 2)
        self.assertEqual(aggregate.await_args_list[0].args[4],
                         datetime.fromisoformat(data["week"]["as_of_utc"]))
        self.assertEqual(aggregate.await_args_list[1].args[3],
                         datetime.fromisoformat(data["previous_period"]["utc_from"]))


class WeeklyAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_additive_totals_and_distinct_patients(self) -> None:
        from src.app.api.v1.reports import _weekly_metrics

        monday = date(2026, 9, 21)
        tuesday = date(2026, 9, 22)
        doctor_a, doctor_b = UUID(int=21), UUID(int=22)
        def row(**values):
            return SimpleNamespace(**values)
        db = SimpleNamespace(execute=AsyncMock(side_effect=[
            _Result(one=row(visits_total=3, completed_visits=2, unique_visited_patients=1, unique_patients_all_visits=2)),
            _Result(rows=[row(day=monday, practitioner_id=doctor_a, visits_total=2, completed_visits=1, unique_visited_patients=1, unique_patients_all_visits=2),
                          row(day=tuesday, practitioner_id=doctor_b, visits_total=1, completed_visits=1, unique_visited_patients=1, unique_patients_all_visits=1)]),
            _Result(rows=[row(day=monday, unique_visited_patients=1, unique_patients_all_visits=2),
                          row(day=tuesday, unique_visited_patients=1, unique_patients_all_visits=1)]),
            _Result(rows=[row(practitioner_id=doctor_a, unique_visited_patients=1, unique_patients_all_visits=2),
                          row(practitioner_id=doctor_b, unique_visited_patients=1, unique_patients_all_visits=1)]),
            _Result(one=row(scheduled_appointments=3, cancelled_appointments=0, no_show_appointments=0)),
            _Result(rows=[row(day=monday, practitioner_id=doctor_a, scheduled_appointments=2, cancelled_appointments=0, no_show_appointments=0),
                          row(day=tuesday, practitioner_id=doctor_b, scheduled_appointments=1, cancelled_appointments=0, no_show_appointments=0)]),
            _Result(rows=[row(currency="INR", amount=300, day=monday, practitioner_id=doctor_a),
                          row(currency="INR", amount=100, day=tuesday, practitioner_id=doctor_b)]),
            _Result(rows=[row(currency="INR", amount=200, day=monday, practitioner_id=doctor_a),
                          row(currency="INR", amount=80, day=tuesday, practitioner_id=doctor_b)]),
            _Result(rows=[row(currency="INR", amount=50, day=monday, practitioner_id=doctor_a),
                          row(currency="INR", amount=20, day=tuesday, practitioner_id=doctor_b)]),
        ]))
        result = await _weekly_metrics(db, UUID(int=2), UUID(int=3),
            datetime(2026, 9, 20, 18, 30, tzinfo=UTC), datetime(2026, 9, 27, 18, 30, tzinfo=UTC),
            "Asia/Kolkata", None, None, "INR", breakdown=True)
        self.assertEqual(result["metrics"]["unique_visited_patients"], 1)
        self.assertEqual(result["metrics"]["unique_patients_all_visits"], 2)
        self.assertEqual(result["days"][monday]["unique_patients_all_visits"], 2)
        self.assertEqual(result["days"][tuesday]["unique_patients_all_visits"], 1)
        self.assertNotEqual(result["metrics"]["unique_patients_all_visits"], sum(day["unique_patients_all_visits"] for day in result["days"].values()))
        self.assertEqual(result["days"][monday]["unique_visited_patients"], 1)
        for name in ("visits_total", "completed_visits", "scheduled_appointments",
                     "billed_amount", "payments_received", "refunds", "net_collections"):
            self.assertEqual(result["metrics"][name], sum(day[name] for day in result["days"].values()))
        self.assertEqual(result["metrics"]["net_collections"], 210)
        self.assertEqual(db.execute.await_count, 9)
        sql = "\n".join(str(call.args[0].compile(dialect=postgresql.dialect()))
                        for call in db.execute.await_args_list)
        self.assertIn("count(distinct(care.encounter.patient_id))", sql)
        self.assertIn("timezone(", sql)
