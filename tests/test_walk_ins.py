import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from src.app.api.v1.queue import check_in, create_walk_in
from src.app.schemas.queue import WalkInCreate


class WalkInTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reuses_active_token_with_frontend_contract(self) -> None:
        payload = WalkInCreate(
            facility_uuid=UUID(int=2),
            patient_uuid=UUID(int=3),
            practitioner_uuid=UUID(int=4),
        )
        entry = SimpleNamespace(
            id=UUID(int=5),
            facility_id=payload.facility_uuid,
            patient_id=payload.patient_uuid,
            appointment_id=None,
            practitioner_id=payload.practitioner_uuid,
            queue_date=date(2026, 9, 17),
            token_number=1,
            status="waiting",
            reason_code=None,
            called_at=None,
        )
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[SimpleNamespace(), entry, 1]),
            add=Mock(),
            commit=AsyncMock(),
        )

        with patch(
            "src.app.api.v1.queue._scope",
            AsyncMock(return_value=(SimpleNamespace(id=UUID(int=1)), None)),
        ):
            response = await create_walk_in(payload, SimpleNamespace(), db)

        queue_entry = response["data"]["queue_entry"]
        self.assertEqual(queue_entry["token_label"], "T-1")
        self.assertEqual(queue_entry["queue_position"], 1)
        self.assertTrue(response["meta"]["reused"])
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_check_in_retry_reuses_existing_queue_entry(self) -> None:
        appointment = SimpleNamespace(
            id=UUID(int=6),
            facility_id=UUID(int=2),
            status="checked_in",
        )
        entry = SimpleNamespace(
            id=UUID(int=7),
            facility_id=appointment.facility_id,
            patient_id=UUID(int=3),
            appointment_id=appointment.id,
            practitioner_id=UUID(int=4),
            token_number=1,
            status="waiting",
            reason_code=None,
            called_at=None,
        )
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[appointment, entry]),
            add=Mock(),
            commit=AsyncMock(),
        )

        with patch("src.app.api.v1.queue._scope", AsyncMock()):
            response = await check_in(appointment.id, SimpleNamespace(), db)

        self.assertTrue(response["meta"]["reused"])
        self.assertEqual(response["data"]["token_label"], "T-1")
        db.add.assert_not_called()
        db.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
