import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.app.api.v1.billing import (
    _billing_read_context,
    list_billing_transactions,
    outstanding_snapshot,
)
from src.app.api.v1.reports import my_practice_report, my_visits_report


class Result:
    def __init__(self, *, first=None, rows=()):
        self.first_value = first
        self.rows = rows

    def first(self):
        return self.first_value

    def all(self):
        return self.rows

    def mappings(self):
        return self


class BusinessReportingReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_billing_requires_scoped_assignment_before_facility_read(self):
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[SimpleNamespace(id=UUID(int=1)), None]),
                             execute=AsyncMock())
        with self.assertRaises(HTTPException) as denied:
            await _billing_read_context(db, SimpleNamespace(id=UUID(int=2)), UUID(int=3))
        self.assertEqual(denied.exception.status_code, 403)
        db.execute.assert_not_awaited()
        sql = str(db.scalar.await_args_list[-1].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("staff_assignment.facility_id", sql)
        self.assertIn("billing_staff", str(db.scalar.await_args_list[-1].args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))

    async def test_old_invoice_payment_today_and_snapshot_ledger(self):
        facility = SimpleNamespace(id=UUID(int=4))
        with patch("src.app.api.v1.billing._billing_read_context", new_callable=AsyncMock,
                   return_value=(UUID(int=3), facility, "Asia/Kolkata")):
            db = SimpleNamespace(execute=AsyncMock(side_effect=[Result(rows=[("INR", 700)]),
                                                     Result(rows=[("INR", "payment", 300)]),
                                                     Result(rows=[])]),
                                 scalar=AsyncMock(return_value=1))
            snapshot = await outstanding_snapshot(facility.id, SimpleNamespace(), db)
            result = await list_billing_transactions(
                facility.id, SimpleNamespace(), db, date_from=date(2026, 9, 23),
                date_to=date(2026, 9, 23), patient_query=None, page=2, page_size=1)
        self.assertEqual(snapshot["data"]["balances"], [{"currency": "INR", "amount_minor": 700}])
        self.assertEqual(result["data"]["totals"][0]["amount_minor"], 300)
        self.assertEqual(result["meta"]["total"], 1)
        ledger_sql = str(db.execute.await_args_list[0].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("care.payment.status", ledger_sql)
        self.assertIn("care.refund.status", ledger_sql)
        self.assertIn("care.invoice.amount_minor -", ledger_sql)
        self.assertIn("greatest", ledger_sql.lower())
        self.assertNotIn("issued_at >=", ledger_sql)
        totals_sql = str(db.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect()))
        page_sql = str(db.execute.await_args_list[2].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("care.payment.received_at >=", totals_sql)
        self.assertIn("care.refund.refunded_at >=", totals_sql)
        self.assertNotIn("LIMIT", totals_sql)
        self.assertIn("LIMIT", page_sql)

    async def test_self_scope_rejects_other_practitioner_before_aggregation(self):
        own = SimpleNamespace(id=UUID(int=6))
        with (
            patch("src.app.api.v1.reports._self_report_context", new_callable=AsyncMock,
                  return_value=(SimpleNamespace(), SimpleNamespace(), "Asia/Kolkata", own)),
            patch("src.app.api.v1.reports._range_summary", new_callable=AsyncMock) as summary,
            self.assertRaises(HTTPException) as denied,
        ):
            await my_practice_report(UUID(int=4), date(2026, 9, 23), date(2026, 9, 23),
                                     SimpleNamespace(), SimpleNamespace(), UUID(int=7))
        self.assertEqual(denied.exception.status_code, 403)
        summary.assert_not_awaited()

    async def test_my_visits_query_keeps_own_practitioner_and_facility_scope(self):
        own = SimpleNamespace(id=UUID(int=6), person_name="Doctor")
        with patch("src.app.api.v1.reports._self_report_context", new_callable=AsyncMock,
                   return_value=(SimpleNamespace(id=UUID(int=3)), SimpleNamespace(id=UUID(int=4)),
                                 "Asia/Kolkata", own)):
            db = SimpleNamespace(scalar=AsyncMock(return_value=0), execute=AsyncMock(return_value=Result()))
            result = await my_visits_report(UUID(int=4), date(2026, 9, 23), date(2026, 9, 23),
                                            SimpleNamespace(), db, practitioner_uuid=None,
                                            patient_query=None, page=2, page_size=1)
        self.assertEqual(result["meta"]["total"], 0)
        sql = str(db.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.assertIn(str(own.id), sql)
        self.assertIn(str(UUID(int=4)), sql)
        self.assertIn("LIMIT 1", sql)


if __name__ == "__main__":
    unittest.main()
