import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from src.app.api.v1.clinical import create_prescription, get_encounter_prescription
from src.app.schemas.clinical import PrescriptionInput


class PrescriptionTests(unittest.IsolatedAsyncioTestCase):
    def test_frontend_medications_payload_is_accepted(self) -> None:
        payload = PrescriptionInput.model_validate(
            {
                "advice": "After meals",
                "medications": [
                    {
                        "name": "Paracetamol",
                        "dose": "500 mg",
                        "frequency": "BID",
                        "duration": "3 days",
                        "route": "Oral",
                        "instructions": "After meals",
                    }
                ],
                "status": "signed",
            }
        )

        self.assertEqual(payload.items[0].medicine_name, "Paracetamol")
        self.assertEqual(payload.items[0].dosage, "500 mg")
        self.assertEqual(payload.items[0].route, "Oral")
        self.assertEqual(payload.items[0].instructions, "After meals")
        self.assertEqual(payload.status, "signed")

    async def test_get_returns_encounter_prescription_and_items(self) -> None:
        encounter = SimpleNamespace(id=UUID(int=1))
        prescription = SimpleNamespace(
            id=UUID(int=2),
            encounter_id=encounter.id,
            status="draft",
            advice="After meals",
            signed_at=None,
        )
        item = SimpleNamespace(
            id=UUID(int=3),
            medicine_name="Paracetamol",
            dosage="500 mg",
            frequency="BID",
            duration="3 days",
            strength=None,
            brand=None,
            route="Oral",
            timing=None,
            instructions="After meals",
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(all=lambda: [(prescription, item)])
            ),
        )

        with patch(
            "src.app.api.v1.clinical._encounter",
            AsyncMock(return_value=encounter),
        ):
            response = await get_encounter_prescription(
                encounter.id,
                SimpleNamespace(),
                db,
            )

        self.assertEqual(response["data"]["items"][0]["name"], "Paracetamol")
        self.assertEqual(response["data"]["items"][0]["route"], "Oral")
        self.assertEqual(response["data"]["medications"], response["data"]["items"])
        self.assertEqual(response["data"]["advice"], "After meals")

    async def test_missing_prescription_returns_empty_draft(self) -> None:
        encounter = SimpleNamespace(id=UUID(int=1))
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(all=lambda: [(None, None)])
            )
        )

        with patch(
            "src.app.api.v1.clinical._encounter",
            AsyncMock(return_value=encounter),
        ):
            response = await get_encounter_prescription(
                encounter.id,
                SimpleNamespace(),
                db,
            )

        self.assertEqual(response["data"]["items"], [])
        self.assertEqual(response["data"]["medications"], [])
        self.assertEqual(response["data"]["status"], "draft")

    async def test_resuming_signed_prescription_updates_without_conflict(self) -> None:
        encounter = SimpleNamespace(
            id=UUID(int=1),
            organization_id=UUID(int=10),
            facility_id=UUID(int=20),
            patient_id=UUID(int=30),
        )
        prescription = SimpleNamespace(
            id=UUID(int=2),
            encounter_id=encounter.id,
            status="signed",
            advice="Old advice",
            signed_at=None,
        )
        db = SimpleNamespace(
            scalar=AsyncMock(return_value=prescription),
            execute=AsyncMock(),
            add_all=Mock(),
            commit=AsyncMock(),
        )

        payload = PrescriptionInput.model_validate(
            {
                "advice": "Updated advice",
                "medications": [
                    {
                        "name": "Amoxicillin",
                        "dose": "500 mg",
                        "frequency": "TID",
                        "duration": "5 days",
                    }
                ],
                "status": "signed",
            }
        )

        with (
            patch(
                "src.app.api.v1.clinical._encounter",
                AsyncMock(return_value=encounter),
            ),
            patch("src.app.api.v1.clinical._practitioner", AsyncMock()),
            patch("src.app.api.v1.clinical.record_audit", AsyncMock()) as mock_audit,
        ):
            response = await create_prescription(
                encounter.id,
                payload,
                SimpleNamespace(id=UUID(int=99)),
                db,
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["advice"], "Updated advice")
        self.assertEqual(len(response["data"]["items"]), 1)
        self.assertEqual(response["data"]["items"][0]["name"], "Amoxicillin")
        self.assertEqual(response["data"]["status"], "signed")
        mock_audit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
