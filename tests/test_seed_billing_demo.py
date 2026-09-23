import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

from scripts.seed_billing_demo import _seed
from src.app.models.billing import ConsultationFee, Invoice, Payment


class SeedBillingDemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_seeds_invoice_and_payment_then_rolls_back(self):
        facility_id, actor_id, org_id, visit_id = (UUID(int=n) for n in range(1, 5))
        visit = SimpleNamespace(id=visit_id, organization_id=org_id,
            facility_id=facility_id, patient_id=UUID(int=5), practitioner_id=None,
            started_at=datetime(2026, 9, 21, tzinfo=UTC), completed_at=None)
        fee = SimpleNamespace(id=UUID(int=6), amount_minor=400_000, currency="INR",
                              effective_from=date(2026, 9, 23))
        db = SimpleNamespace(
            get=AsyncMock(side_effect=[SimpleNamespace(id=facility_id, organization_id=org_id, is_active=True),
                                       SimpleNamespace(id=actor_id, is_active=True)]),
            scalar=AsyncMock(side_effect=[fee, None]),
            scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [visit])),
            add=MagicMock(), flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
        )
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=db)
        session.__aexit__ = AsyncMock(return_value=None)
        args = SimpleNamespace(facility_uuid=facility_id, actor_user_uuid=actor_id,
            practitioner_uuid=None, effective_from=date(2026, 9, 1), currency="INR",
            facility_minor=50_000, specialty_minor=70_000, practitioner_minor=90_000,
            include_in_progress=True, apply=False)
        with patch("scripts.seed_billing_demo.local_session", return_value=session), \
             patch("scripts.seed_billing_demo._resolve_fee", new=AsyncMock(return_value=fee)):
            await _seed(args)
        historical_fee, invoice, payment = [call.args[0] for call in db.add.call_args_list]
        self.assertIsInstance(historical_fee, ConsultationFee)
        self.assertEqual((historical_fee.amount_minor, historical_fee.effective_to),
                         (400_000, date(2026, 9, 22)))
        self.assertIsInstance(invoice, Invoice)
        self.assertIsInstance(payment, Payment)
        self.assertEqual((invoice.status, payment.amount_minor, payment.received_at),
                         ("paid", 400_000, visit.started_at))
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
