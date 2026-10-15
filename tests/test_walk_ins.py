import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import HTTPException

from src.app.api.v1.queue import _item, call_queue_entry, check_in, create_walk_in
from src.app.core.timezones import timezone
from src.app.schemas.queue import WalkInCreate


class WalkInTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_retry_returns_called_entry_without_duplicate_write(self) -> None:
        called_at = SimpleNamespace(isoformat=lambda: "2026-09-21T08:29:06+00:00")
        entry = SimpleNamespace(
            id=UUID(int=1),
            organization_id=UUID(int=2),
            facility_id=UUID(int=3),
            patient_id=UUID(int=4),
            appointment_id=None,
            practitioner_id=UUID(int=5),
            token_number=2,
            status="called",
            reason_code=None,
            called_at=called_at,
        )
        db = SimpleNamespace(scalar=AsyncMock(return_value=entry), commit=AsyncMock())

        with (
            patch("src.app.api.v1.queue._scope", AsyncMock()),
            patch("src.app.api.v1.queue.record_audit", AsyncMock()) as audit,
        ):
            response = await call_queue_entry(entry.id, SimpleNamespace(id=UUID(int=100)), db)

        self.assertEqual(response["data"]["status"], "called")
        self.assertIs(entry.called_at, called_at)
        audit.assert_not_awaited()
        db.commit.assert_not_awaited()

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
            scalar=AsyncMock(side_effect=[SimpleNamespace(id=UUID(int=100)), entry, 1]),
            add=Mock(),
            commit=AsyncMock(),
        )

        with (
            patch(
                "src.app.api.v1.queue._scope",
                AsyncMock(return_value=(SimpleNamespace(id=UUID(int=1)), None)),
            ),
            patch("src.app.api.v1.queue.record_audit", AsyncMock()) as audit,
        ):
            response = await create_walk_in(payload, SimpleNamespace(id=UUID(int=100)), db)

        queue_entry = response["data"]["queue_entry"]
        self.assertEqual(queue_entry["token_label"], "T-1")
        self.assertEqual(queue_entry["queue_position"], 1)
        self.assertTrue(queue_entry["is_active"])
        self.assertTrue(response["meta"]["reused"])
        db.add.assert_not_called()
        db.commit.assert_awaited_once()
        audit.assert_awaited_once()
        self.assertTrue(audit.await_args.kwargs["details"]["reused"])

    def test_completed_queue_entry_is_not_active(self) -> None:
        entry = SimpleNamespace(
            id=UUID(int=1),
            facility_id=UUID(int=2),
            patient_id=UUID(int=3),
            appointment_id=None,
            practitioner_id=None,
            token_number=1,
            status="completed",
            reason_code=None,
            called_at=None,
        )

        self.assertFalse(_item(entry)["is_active"])

    def _appointment(self, **overrides):
        values = {
            "id": UUID(int=6),
            "organization_id": UUID(int=1),
            "facility_id": UUID(int=2),
            "patient_id": UUID(int=3),
            "practitioner_id": UUID(int=4),
            "resource_id": None,
            "status": "booked",
            "version": 1,
            "reason_code": None,
            "reason_text": None,
            "scheduled_start": datetime.now(UTC),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _queue_entry(self, appointment, **overrides):
        values = {
            "id": UUID(int=7),
            "organization_id": appointment.organization_id,
            "facility_id": appointment.facility_id,
            "patient_id": appointment.patient_id,
            "appointment_id": appointment.id,
            "practitioner_id": appointment.practitioner_id,
            "queue_date": datetime.now(UTC).date(),
            "token_number": 1,
            "status": "waiting",
            "reason_code": None,
            "called_at": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    async def test_check_in_retry_reuses_existing_queue_entry(self) -> None:
        appointment = self._appointment(status="checked_in")
        entry = self._queue_entry(appointment)
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[appointment, entry]),
            add=Mock(),
            commit=AsyncMock(),
        )

        with (
            patch("src.app.api.v1.queue._scope", AsyncMock()),
            patch(
                "src.app.api.v1.queue._facility_timezone",
                AsyncMock(return_value=timezone("Asia/Kolkata")),
            ),
        ):
            response = await check_in(appointment.id, SimpleNamespace(id=UUID(int=100)), db)

        self.assertTrue(response["meta"]["reused"])
        self.assertEqual(response["data"]["token_label"], "T-1")
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    async def test_check_in_repairs_appointment_with_existing_queue_entry(self) -> None:
        appointment = self._appointment()
        entry = self._queue_entry(appointment)
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[appointment, entry]),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
        )
        audit = AsyncMock()

        with (
            patch("src.app.api.v1.queue._scope", AsyncMock()),
            patch(
                "src.app.api.v1.queue._facility_timezone",
                AsyncMock(return_value=timezone("Asia/Kolkata")),
            ),
            patch("src.app.api.v1.queue.record_audit", audit),
            patch("src.app.api.v1.queue.emit_queue_token_assigned", AsyncMock()),
            patch("src.app.api.v1.queue.publish", AsyncMock()),
        ):
            response = await check_in(appointment.id, SimpleNamespace(id=UUID(int=100)), db)

        self.assertEqual(appointment.status, "checked_in")
        self.assertTrue(response["meta"]["reused"])
        db.add.assert_not_called()
        db.commit.assert_awaited_once()
        self.assertEqual(audit.await_args.kwargs["action"], "appointment.checked_in")
        self.assertTrue(audit.await_args.kwargs["details"]["reused"])

    async def test_check_in_rejects_non_today_appointment(self) -> None:
        appointment = self._appointment(
            scheduled_start=datetime.now(UTC) + timedelta(days=1)
        )
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[appointment]),
            add=Mock(),
            commit=AsyncMock(),
        )

        with (
            patch("src.app.api.v1.queue._scope", AsyncMock()),
            patch(
                "src.app.api.v1.queue._facility_timezone",
                AsyncMock(return_value=timezone("Asia/Kolkata")),
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            await check_in(appointment.id, SimpleNamespace(id=UUID(int=100)), db)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["code"], "APPOINTMENT_NOT_TODAY")
        db.commit.assert_not_awaited()

    async def test_check_in_creates_new_entry_when_no_active_entry_today(self) -> None:
        appointment = self._appointment()
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[appointment, None]),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
        )
        audit = AsyncMock()

        with (
            patch("src.app.api.v1.queue._scope", AsyncMock()),
            patch(
                "src.app.api.v1.queue._facility_timezone",
                AsyncMock(return_value=timezone("Asia/Kolkata")),
            ),
            patch("src.app.api.v1.queue._next_token", AsyncMock(return_value=5)),
            patch("src.app.api.v1.queue.record_audit", audit),
            patch("src.app.api.v1.queue.emit_queue_token_assigned", AsyncMock()),
            patch("src.app.api.v1.queue.publish", AsyncMock()),
        ):
            response = await check_in(appointment.id, SimpleNamespace(id=UUID(int=100)), db)

        self.assertEqual(response["data"]["token_label"], "T-5")
        self.assertEqual(response["meta"], {})
        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        self.assertFalse(audit.await_args.kwargs["details"]["reused"])

    async def test_walk_in_records_audit_without_reuse(self) -> None:
        payload = WalkInCreate(facility_uuid=UUID(int=2), patient_uuid=UUID(int=3))
        patient = SimpleNamespace(id=UUID(int=3), is_active=True, organization_id=UUID(int=1))
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[patient, None, 1]),
            add=Mock(),
            flush=AsyncMock(),
            commit=AsyncMock(),
        )
        audit = AsyncMock()

        with (
            patch(
                "src.app.api.v1.queue._scope",
                AsyncMock(return_value=(SimpleNamespace(id=UUID(int=1)), None)),
            ),
            patch("src.app.api.v1.queue._next_token", AsyncMock(return_value=3)),
            patch("src.app.api.v1.queue.record_audit", audit),
            patch("src.app.api.v1.queue.emit_queue_token_assigned", AsyncMock()),
            patch("src.app.api.v1.queue.publish", AsyncMock()),
        ):
            response = await create_walk_in(payload, SimpleNamespace(id=UUID(int=100)), db)

        self.assertEqual(response["data"]["queue_entry"]["token_label"], "T-3")
        db.commit.assert_awaited_once()
        self.assertEqual(audit.await_args.kwargs["action"], "queue.walk_in_created")
        self.assertFalse(audit.await_args.kwargs["details"]["reused"])


if __name__ == "__main__":
    unittest.main()
