import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import HTTPException

from src.app.api.v1.clinical import complete_encounter
from src.app.api.v1.encounters import _start, start_appointment_consultation


class EncounterResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_starting_an_active_consultation_returns_existing_encounter(
        self,
    ) -> None:
        appointment_id = UUID("01a0a94b-3294-7e25-ada9-144673c238e7")
        appointment = SimpleNamespace(id=appointment_id, status="in_consultation")
        encounter = SimpleNamespace(
            id=UUID(int=1),
            facility_id=UUID(int=2),
            patient_id=UUID(int=3),
            practitioner_id=UUID(int=4),
            appointment_id=appointment_id,
            queue_entry_id=None,
            status="in_progress",
            started_at=SimpleNamespace(isoformat=lambda: "2026-09-17T09:00:00+05:30"),
            completed_at=None,
        )
        db = SimpleNamespace(get=AsyncMock(return_value=appointment))

        with patch(
            "src.app.api.v1.encounters._start", AsyncMock(return_value=encounter)
        ):
            response = await start_appointment_consultation(
                appointment_id,
                SimpleNamespace(),
                db,
            )

        self.assertEqual(response["data"]["status"], "in_consultation")
        self.assertEqual(response["data"]["encounter"]["uuid"], str(encounter.id))
        self.assertEqual(response["data"]["encounter"]["status"], "in_progress")

    async def test_new_appointment_requires_check_in_and_queue_entry(self) -> None:
        appointment = SimpleNamespace(
            id=UUID(int=1),
            organization_id=UUID(int=2),
            facility_id=UUID(int=3),
            patient_id=UUID(int=4),
            practitioner_id=UUID(int=5),
            status="booked",
        )
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[None, None]))

        with patch("src.app.api.v1.encounters._scope", AsyncMock()), self.assertRaises(HTTPException) as raised:
            await _start(db, SimpleNamespace(), appointment=appointment)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "APPOINTMENT_NOT_CHECKED_IN")

    async def test_completed_encounter_cannot_be_restarted(self) -> None:
        queue = SimpleNamespace(id=UUID(int=1), facility_id=UUID(int=2))
        encounter = SimpleNamespace(status="completed")
        db = SimpleNamespace(scalar=AsyncMock(return_value=encounter))

        with patch("src.app.api.v1.encounters._scope", AsyncMock()), self.assertRaises(HTTPException) as raised:
            await _start(db, SimpleNamespace(), queue=queue)

        self.assertEqual(raised.exception.status_code, 409)

    async def test_scheduled_completion_updates_encounter_queue_and_appointment(self) -> None:
        queue = SimpleNamespace(status="in_consultation")
        appointment = SimpleNamespace(status="in_consultation")
        encounter = SimpleNamespace(
            id=UUID(int=1),
            queue_entry_id=UUID(int=2),
            appointment_id=UUID(int=3),
            status="in_progress",
            completed_at=None,
        )
        db = SimpleNamespace(
            get=AsyncMock(side_effect=[queue, appointment]), commit=AsyncMock()
        )

        with patch(
            "src.app.api.v1.clinical._encounter", AsyncMock(return_value=encounter)
        ):
            response = await complete_encounter(
                encounter.id, SimpleNamespace(), db
            )

        self.assertEqual(response["data"]["status"], "completed")
        self.assertEqual(queue.status, "completed")
        self.assertEqual(appointment.status, "completed")
        db.commit.assert_awaited_once()

    async def test_walk_in_completion_updates_encounter_and_queue(self) -> None:
        queue = SimpleNamespace(status="in_consultation")
        encounter = SimpleNamespace(
            id=UUID(int=1),
            queue_entry_id=UUID(int=2),
            appointment_id=None,
            status="in_progress",
            completed_at=None,
        )
        db = SimpleNamespace(get=AsyncMock(return_value=queue), commit=AsyncMock())

        with patch(
            "src.app.api.v1.clinical._encounter", AsyncMock(return_value=encounter)
        ):
            await complete_encounter(encounter.id, SimpleNamespace(), db)

        self.assertEqual(encounter.status, "completed")
        self.assertEqual(queue.status, "completed")
        db.commit.assert_awaited_once()

    async def test_checked_in_appointment_updates_all_links_atomically(self) -> None:
        appointment = SimpleNamespace(
            id=UUID(int=1),
            organization_id=UUID(int=2),
            facility_id=UUID(int=3),
            patient_id=UUID(int=4),
            practitioner_id=UUID(int=5),
            status="checked_in",
        )
        queue = SimpleNamespace(
            id=UUID(int=6),
            organization_id=appointment.organization_id,
            facility_id=appointment.facility_id,
            patient_id=appointment.patient_id,
            practitioner_id=appointment.practitioner_id,
            appointment_id=appointment.id,
            status="waiting",
        )
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, queue]),
            add=Mock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        with patch("src.app.api.v1.encounters._scope", AsyncMock()):
            encounter = await _start(db, SimpleNamespace(), appointment=appointment)

        self.assertEqual(encounter.appointment_id, appointment.id)
        self.assertEqual(encounter.queue_entry_id, queue.id)
        self.assertEqual(appointment.status, "in_consultation")
        self.assertEqual(queue.status, "in_consultation")
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()

    async def test_failed_commit_rolls_back_the_lifecycle_transition(self) -> None:
        appointment = SimpleNamespace(
            id=UUID(int=1),
            organization_id=UUID(int=2),
            facility_id=UUID(int=3),
            patient_id=UUID(int=4),
            practitioner_id=UUID(int=5),
            status="checked_in",
        )
        queue = SimpleNamespace(
            id=UUID(int=6),
            organization_id=appointment.organization_id,
            facility_id=appointment.facility_id,
            patient_id=appointment.patient_id,
            practitioner_id=appointment.practitioner_id,
            appointment_id=appointment.id,
            status="waiting",
        )
        db = SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, queue]),
            add=Mock(),
            commit=AsyncMock(side_effect=RuntimeError("forced commit failure")),
            rollback=AsyncMock(),
        )

        with patch("src.app.api.v1.encounters._scope", AsyncMock()), self.assertRaises(RuntimeError):
            await _start(db, SimpleNamespace(), appointment=appointment)

        db.rollback.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
