import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from scripts.backfill_billing_invoices import backfill


class BackfillBillingTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_does_not_insert_and_apply_skips_missing_fees(self):
        facility_id, actor_id, organization_id = (UUID(int=n) for n in (1, 2, 3))
        visits = [SimpleNamespace(id=UUID(int=n), organization_id=organization_id,
                                  facility_id=facility_id, patient_id=UUID(int=n + 10),
                                  practitioner_id=None) for n in (4, 5)]
        db = SimpleNamespace(get=AsyncMock(side_effect=[SimpleNamespace(id=facility_id,
                              organization_id=organization_id, is_active=True),
                              SimpleNamespace(id=actor_id, is_active=True)]),
                             scalar=AsyncMock(return_value=UUID(int=9)),
                             scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: visits)),
                             add=unittest.mock.Mock(), commit=AsyncMock())
        session = unittest.mock.MagicMock()
        session.__aenter__ = AsyncMock(return_value=db)
        session.__aexit__ = AsyncMock(return_value=None)
        fee = SimpleNamespace(id=UUID(int=6), amount_minor=50000, currency="INR")
        with patch("scripts.backfill_billing_invoices.local_session", return_value=session), \
             patch("scripts.backfill_billing_invoices._resolve_fee", new=AsyncMock(side_effect=[fee, None])):
            self.assertEqual(await backfill(facility_id, actor_id, False), (1, 1))
        db.add.assert_not_called()
        db.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
