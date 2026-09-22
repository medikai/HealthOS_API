import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from pydantic import ValidationError

from src.app.api.v1.billing import _refresh_invoice_status, _resolve_fee
from src.app.schemas.billing import PaymentInput


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class BillingTests(unittest.IsolatedAsyncioTestCase):
    async def test_practitioner_fee_wins_on_facility_local_visit_date(self) -> None:
        practitioner_id = UUID(int=1)
        specialty_id = UUID(int=2)
        encounter = SimpleNamespace(
            organization_id=UUID(int=3),
            facility_id=UUID(int=4),
            practitioner_id=practitioner_id,
            started_at=datetime(2026, 9, 22, 20, 0, tzinfo=UTC),
        )
        practitioner = SimpleNamespace(specialty_id=specialty_id)
        facility_fee = SimpleNamespace(scope_type="facility")
        specialty_fee = SimpleNamespace(scope_type="specialty")
        practitioner_fee = SimpleNamespace(scope_type="practitioner")
        db = SimpleNamespace(
            get=AsyncMock(return_value=practitioner),
            scalar=AsyncMock(return_value="Asia/Kolkata"),
            scalars=AsyncMock(
                return_value=_Scalars([facility_fee, specialty_fee, practitioner_fee])
            ),
        )

        resolved = await _resolve_fee(db, encounter)

        self.assertIs(resolved, practitioner_fee)
        query = db.scalars.await_args.args[0]
        self.assertIn(date(2026, 9, 23), query.compile().params.values())

    async def test_multiple_payments_and_refund_refresh_invoice_status(self) -> None:
        invoice = SimpleNamespace(id=UUID(int=5), amount_minor=100_000, status="issued")
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[70_000, 10_000]))

        paid, refunded = await _refresh_invoice_status(db, invoice)

        self.assertEqual((paid, refunded), (70_000, 10_000))
        self.assertEqual(invoice.status, "partially_paid")
        self.assertEqual(db.scalar.await_count, 2)

    def test_payment_timestamp_requires_timezone(self) -> None:
        with self.assertRaises(ValidationError):
            PaymentInput(
                amount_minor=10_000,
                idempotency_key="payment-001",
                received_at=datetime.fromisoformat("2026-09-23T10:00:00"),
            )


if __name__ == "__main__":
    unittest.main()
