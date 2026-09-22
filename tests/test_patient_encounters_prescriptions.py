import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import HTTPException

from src.app.api.v1.patients import list_patient_encounters, list_patient_prescriptions
from src.app.api.v1.scheduling import list_appointments


class PatientHistoryReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.patient_id = UUID("00000000-0000-0000-0000-000000000001")
        self.org_id = UUID("00000000-0000-0000-0000-000000000010")
        self.account = SimpleNamespace(id=UUID(int=99))
        self.patient = SimpleNamespace(id=self.patient_id, organization_id=self.org_id, is_active=True)

    async def test_list_patient_encounters_success(self) -> None:
        enc_id = UUID(int=101)
        encounter = SimpleNamespace(
            id=enc_id,
            patient_id=self.patient_id,
            organization_id=self.org_id,
            facility_id=UUID(int=2),
            practitioner_id=UUID(int=3),
            appointment_id=UUID(int=4),
            queue_entry_id=None,
            status="completed",
            started_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
            completed_at=datetime(2026, 9, 21, 12, 30, tzinfo=UTC),
        )
        facility = SimpleNamespace(id=UUID(int=2), name="City Care Clinic")
        practitioner = SimpleNamespace(id=UUID(int=3), person_name="Dr. Nitin Kumar", specialty="General Practice")

        soap = SimpleNamespace(
            id=UUID(int=201),
            encounter_id=enc_id,
            subjective="Cough and cold",
            objective="Throat congested",
            assessment="Viral URI",
            plan="Rest and fluids",
            custom_fields={},
            status="signed",
            signed_at=datetime(2026, 9, 21, 12, 25, tzinfo=UTC),
        )
        diagnosis = SimpleNamespace(
            id=UUID(int=301),
            encounter_id=enc_id,
            code="J06.9",
            description="Acute upper respiratory infection",
            is_primary=True,
        )
        vital = SimpleNamespace(
            id=UUID(int=401),
            encounter_id=enc_id,
            name="blood_pressure",
            value="120/80",
            unit="mmHg",
            recorded_at=datetime(2026, 9, 21, 12, 5, tzinfo=UTC),
        )
        rx = SimpleNamespace(
            id=UUID(int=501),
            encounter_id=enc_id,
            status="signed",
            advice="Take after meals",
            signed_at=datetime(2026, 9, 21, 12, 25, tzinfo=UTC),
        )
        rx_item = SimpleNamespace(
            id=UUID(int=601),
            prescription_id=rx.id,
            medicine_id=UUID(int=701),
            medicine_name="Paracetamol 500mg",
            dosage="1 tab",
            frequency="TDS",
            duration="3 days",
            strength="500mg",
            brand="Calpol",
            route="Oral",
            timing="After meals",
            instructions="Drink plenty of water",
        )

        db = SimpleNamespace(
            scalar=AsyncMock(return_value=self.patient),
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(all=lambda: [(encounter, facility, practitioner)]),
                    SimpleNamespace(all=lambda: [(rx, rx_item)]),
                ]
            ),
            scalars=AsyncMock(
                side_effect=[
                    SimpleNamespace(all=lambda: [soap]),
                    SimpleNamespace(all=lambda: [diagnosis]),
                    SimpleNamespace(all=lambda: [vital]),
                ]
            ),
        )

        with patch("src.app.api.v1.patients._org", AsyncMock(return_value=SimpleNamespace(id=self.org_id))):
            res = await list_patient_encounters(self.patient_id, self.account, db)

        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]["items"]), 1)
        item = res["data"]["items"][0]
        self.assertEqual(item["uuid"], str(enc_id))
        self.assertEqual(item["facility"]["name"], "City Care Clinic")
        self.assertEqual(item["practitioner"]["name"], "Dr. Nitin Kumar")
        self.assertEqual(item["soap"]["assessment"], "Viral URI")
        self.assertEqual(item["diagnoses"][0]["code"], "J06.9")
        self.assertEqual(item["vitals"][0]["value"], "120/80")
        self.assertEqual(item["prescription"]["items"][0]["medicine_name"], "Paracetamol 500mg")

    async def test_list_patient_prescriptions_success(self) -> None:
        enc_id = UUID(int=101)
        encounter = SimpleNamespace(
            id=enc_id,
            appointment_id=UUID(int=4),
            started_at=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
        )
        facility = SimpleNamespace(id=UUID(int=2), name="City Care Clinic")
        practitioner = SimpleNamespace(id=UUID(int=3), person_name="Dr. Nitin Kumar", specialty="General Practice")
        rx = SimpleNamespace(
            id=UUID(int=501),
            encounter_id=enc_id,
            status="signed",
            advice="Take after food",
            signed_at=datetime(2026, 9, 21, 12, 25, tzinfo=UTC),
        )
        rx_item = SimpleNamespace(
            id=UUID(int=601),
            prescription_id=rx.id,
            medicine_id=UUID(int=701),
            medicine_name="Amoxicillin 500mg",
            dosage="1 cap",
            frequency="BD",
            duration="5 days",
            strength="500mg",
            brand="Amoxil",
            route="Oral",
            timing="After meals",
            instructions="Complete course",
        )

        db = SimpleNamespace(
            scalar=AsyncMock(return_value=self.patient),
            execute=AsyncMock(
                return_value=SimpleNamespace(all=lambda: [(rx, encounter, facility, practitioner)])
            ),
            scalars=AsyncMock(
                return_value=SimpleNamespace(all=lambda: [rx_item])
            ),
        )

        with patch("src.app.api.v1.patients._org", AsyncMock(return_value=SimpleNamespace(id=self.org_id))):
            res = await list_patient_prescriptions(self.patient_id, self.account, db)

        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]["items"]), 1)
        item = res["data"]["items"][0]
        self.assertEqual(item["uuid"], str(rx.id))
        self.assertEqual(item["advice"], "Take after food")
        self.assertEqual(item["practitioner"]["name"], "Dr. Nitin Kumar")
        self.assertEqual(item["items"][0]["medicine_name"], "Amoxicillin 500mg")

    async def test_list_appointments_order_and_encounter_uuid(self) -> None:
        appt = SimpleNamespace(
            id=UUID(int=1),
            facility_id=UUID(int=2),
            patient_id=self.patient_id,
            practitioner_id=UUID(int=3),
            scheduled_start=datetime(2026, 9, 22, 14, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 22, 14, 30, tzinfo=UTC),
            status="in_consultation",
            reason_code="Consultation",
            reason_text=None,
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(all=lambda: [(appt, None, None, None)])
            )
        )

        query_mock = Mock()
        query_mock.where.return_value = query_mock
        query_mock.order_by.return_value = query_mock

        with (
            patch("src.app.api.v1.scheduling._appointment_query", return_value=query_mock),
            patch("src.app.api.v1.scheduling._scope", AsyncMock()),
        ):
            res = await list_appointments(
                account=self.account,
                db=db,
                patient_uuid=self.patient_id,
                order="desc",
            )

        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["items"][0]["uuid"], str(appt.id))
        query_mock.order_by.assert_called_once()


if __name__ == "__main__":
    unittest.main()
