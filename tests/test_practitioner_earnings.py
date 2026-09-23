import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.app.api.v1.earnings import (
    PayoutInput,
    ReversalInput,
    _read_statement,
    _refund_entitlement,
    _round_div,
    record_earning_event,
    record_payout,
    reverse_payout,
)


class EarningsCheckpoint(unittest.IsolatedAsyncioTestCase):
    def test_partial_receipt_refund_rounding_and_exact_full_reversal(self):
        earned = _round_div(101 * 3333, 10000)
        first = _refund_entitlement(earned, 101, 50, 0)
        last = _refund_entitlement(earned, 101, 101, first)
        self.assertEqual((earned, first, last, earned + first + last), (34, -17, -17, 0))
        self.assertEqual(_round_div(1 * 5000, 10000), 1)

    async def test_receipt_entry_snapshots_policy_and_source(self):
        invoice = SimpleNamespace(organization_id=UUID(int=1), facility_id=UUID(int=2),
                                  practitioner_id=UUID(int=3), currency="INR")
        payment = SimpleNamespace(id=UUID(int=4), invoice_id=UUID(int=5), amount_minor=101)
        policy = SimpleNamespace(id=UUID(int=6), basis_points=3333)
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[None, UUID(int=3), None,
                                                      "Asia/Kolkata", policy]),
                             add=unittest.mock.Mock(), flush=AsyncMock())
        with patch("src.app.api.v1.earnings._eligible_invoice", new_callable=AsyncMock,
                   return_value=(invoice, None)):
            reason = await record_earning_event(db, payment, "payment", payment.id,
                                                101, datetime(2026, 9, 23, tzinfo=UTC))
        self.assertIsNone(reason)
        entry = db.add.call_args.args[0]
        self.assertEqual((entry.amount_minor, entry.base_minor, entry.basis_points), (34, 101, 3333))
        self.assertEqual((entry.source_type, entry.source_id, entry.policy_id),
                         ("payment", payment.id, policy.id))

    async def test_self_tampering_and_receptionist_denial(self):
        with patch("src.app.api.v1.earnings._self_report_context", new_callable=AsyncMock,
                   return_value=(SimpleNamespace(), SimpleNamespace(), "Asia/Kolkata",
                                 SimpleNamespace(id=UUID(int=1)))), self.assertRaises(HTTPException) as denied:
            await _read_statement(SimpleNamespace(), SimpleNamespace(), UUID(int=2),
                                  UUID(int=3), "INR", date(2026, 9, 1), date(2026, 9, 2), True)
        self.assertEqual(denied.exception.detail["code"], "PRACTITIONER_SCOPE_DENIED")
        with patch("src.app.api.v1.earnings._report_context", new_callable=AsyncMock,
                   side_effect=HTTPException(status_code=403, detail={"code": "REPORT_ACCESS_REQUIRED"})), \
             self.assertRaises(HTTPException) as denied:
            await record_payout(UUID(int=2), PayoutInput(practitioner_uuid=UUID(int=1),
                currency="INR", amount_minor=1, paid_at=datetime.now(UTC), method="cash",
                reference="receipt", idempotency_key="payout-001"), SimpleNamespace(), SimpleNamespace())
        self.assertEqual(denied.exception.status_code, 403)

    async def test_duplicate_payout_and_reversal_are_idempotent_after_scope_check(self):
        org, facility = SimpleNamespace(id=UUID(int=1)), SimpleNamespace(id=UUID(int=2))
        now = datetime.now(UTC)
        payload = PayoutInput(practitioner_uuid=UUID(int=3), currency="INR", amount_minor=50,
                              paid_at=now, method="cash", reference="r-1", idempotency_key="payout-001")
        existing = SimpleNamespace(id=UUID(int=4), organization_id=org.id, facility_id=facility.id,
            practitioner_id=payload.practitioner_uuid, currency="INR", amount_minor=50,
            paid_at=now, method="cash", reference="r-1", kind="payout", idempotency_key="payout-001")
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[UUID(int=3), existing]), commit=AsyncMock())
        with patch("src.app.api.v1.earnings._admin", new_callable=AsyncMock,
                   return_value=(org, facility, "Asia/Kolkata")), \
             patch("src.app.api.v1.earnings._practitioner", new_callable=AsyncMock):
            response = await record_payout(facility.id, payload, SimpleNamespace(), db)
        self.assertTrue(response["meta"]["idempotent"])
        db.commit.assert_not_awaited()
        lock_sql = str(db.scalar.await_args_list[0].args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("FOR UPDATE", lock_sql)
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[existing, UUID(int=3),
            SimpleNamespace(id=UUID(int=5), idempotency_key="reverse-001")]), commit=AsyncMock())
        with patch("src.app.api.v1.earnings._admin", new_callable=AsyncMock,
                   return_value=(org, facility, "Asia/Kolkata")):
            response = await reverse_payout(existing.id, ReversalInput(reason="entry error",
                idempotency_key="reverse-001"), SimpleNamespace(), db)
        self.assertTrue(response["meta"]["idempotent"])
        db.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
