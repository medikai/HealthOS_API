import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import TypeAdapter

from src.app.api.v1.clinical import get_soap
from src.app.api.v1.clinical import router as clinical_router
from src.app.api.v1.encounters import create_vital, get_encounter, get_vitals
from src.app.api.v1.encounters import router as encounters_router
from src.app.schemas.clinical import SoapNoteResponse
from src.app.schemas.encounters import (
    EncounterDetailResponse,
    VitalInput,
    VitalSetCreate,
    VitalsResponse,
)


def entity(number: int, **values):
    return SimpleNamespace(id=UUID(int=number), **values)


class EncounterReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.encounter = entity(
            1,
            organization_id=UUID(int=10),
            facility_id=UUID(int=2),
            patient_id=UUID(int=3),
            practitioner_id=UUID(int=5),
            appointment_id=UUID(int=6),
            queue_entry_id=UUID(int=7),
            status="in_progress",
            started_at=datetime(2026, 9, 16, 19, 14, tzinfo=UTC),
            completed_at=None,
        )
        self.row = (
            self.encounter,
            entity(2, name="Main Clinic"),
            entity(3, mrn="MRN-001", person_id=UUID(int=4)),
            entity(
                4,
                first_name="Patient",
                last_name="Name",
                gender="male",
                date_of_birth="1990-01-01",
            ),
            entity(5, person_name="Dr. Ganesh Sawant", specialty="General Medicine"),
            entity(7, token_number=1),
        )

    async def test_detail_returns_required_nested_entities(self) -> None:
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: self.row))
        )

        with patch("src.app.api.v1.encounters._scope", AsyncMock()):
            response = await get_encounter(self.encounter.id, SimpleNamespace(), db)

        EncounterDetailResponse.model_validate(response)
        data = response["data"]
        self.assertEqual(data["facility"]["name"], "Main Clinic")
        self.assertEqual(data["patient"]["display_name"], "Patient Name")
        self.assertEqual(data["patient"]["mrn"], "MRN-001")
        self.assertEqual(data["practitioner"]["name"], "Dr. Ganesh Sawant")
        self.assertEqual(data["token_label"], "T-1")

    async def test_missing_encounter_returns_404(self) -> None:
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None))
        )

        with self.assertRaises(HTTPException) as raised:
            await get_encounter(UUID(int=99), SimpleNamespace(), db)

        self.assertEqual(raised.exception.status_code, 404)

    async def test_cross_facility_scope_is_still_enforced(self) -> None:
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: self.row))
        )

        with (
            patch(
                "src.app.api.v1.encounters._scope",
                AsyncMock(
                    side_effect=HTTPException(status_code=403, detail="Forbidden")
                ),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            await get_encounter(self.encounter.id, SimpleNamespace(), db)

        self.assertEqual(raised.exception.status_code, 403)

    async def test_missing_related_entity_is_a_controlled_error(self) -> None:
        row = (*self.row[:4], None, self.row[5])
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: row))
        )

        with (
            patch("src.app.api.v1.encounters._scope", AsyncMock()),
            self.assertRaises(HTTPException) as raised,
        ):
            await get_encounter(self.encounter.id, SimpleNamespace(), db)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"], "ENCOUNTER_DATA_INTEGRITY_ERROR"
        )

    async def test_empty_soap_is_non_null_and_does_not_write(self) -> None:
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    first=lambda: (self.encounter, None, True)
                )
            ),
            add=Mock(),
        )

        response = await get_soap(
            self.encounter.id, SimpleNamespace(id=UUID(int=10)), db
        )

        self.assertEqual(
            response["data"],
            {
                "uuid": None,
                "encounter_uuid": str(self.encounter.id),
                "subjective": "",
                "objective": "",
                "assessment": "",
                "plan": "",
                "custom_fields": {},
                "status": "draft",
                "signed_at": None,
            },
        )
        db.add.assert_not_called()
        SoapNoteResponse.model_validate(response)

    async def test_missing_encounter_soap_returns_404(self) -> None:
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(first=lambda: None)
            )
        )

        with self.assertRaises(HTTPException) as raised:
            await get_soap(
                UUID(int=99), SimpleNamespace(id=UUID(int=10)), db
            )

        self.assertEqual(raised.exception.status_code, 404)

    async def test_soap_rejects_cross_organization_encounter(self) -> None:
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    first=lambda: (self.encounter, None, False)
                )
            )
        )
        account = SimpleNamespace(id=UUID(int=10))

        with (
            patch(
                "src.app.api.v1.clinical._staff_context",
                AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace())),
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            await get_soap(self.encounter.id, account, db)

        self.assertEqual(raised.exception.status_code, 403)

    async def test_existing_soap_normalizes_nullable_text(self) -> None:
        note = entity(
            8,
            encounter_id=self.encounter.id,
            subjective=None,
            objective="Observed",
            assessment=None,
            plan="Rest",
            status="draft",
            signed_at=None,
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    first=lambda: (self.encounter, note, True)
                )
            ),
        )

        response = await get_soap(
            self.encounter.id, SimpleNamespace(id=UUID(int=10)), db
        )

        self.assertEqual(response["data"]["subjective"], "")
        self.assertEqual(response["data"]["objective"], "Observed")
        self.assertEqual(response["data"]["assessment"], "")

    async def test_vitals_empty_and_legacy_observation_shapes(self) -> None:
        for values, expected in (
            ([], []),
            (
                [
                    entity(
                        9,
                        recording_id=None,
                        name="temperature_c",
                        value="36.8",
                        unit="C",
                        recorded_at=datetime(2026, 9, 16, 19, 14, tzinfo=UTC),
                    )
                ],
                ["temperature_c"],
            ),
        ):
            db = SimpleNamespace(
                get=AsyncMock(return_value=self.encounter),
                scalars=AsyncMock(
                    return_value=SimpleNamespace(all=lambda values=values: values)
                ),
            )
            with patch("src.app.api.v1.encounters._scope", AsyncMock()):
                response = await get_vitals(self.encounter.id, SimpleNamespace(), db)
            VitalsResponse.model_validate(response)
            self.assertEqual(
                [item["name"] for item in response["data"]["items"]], expected
            )

    async def test_vitals_with_recording_id_are_safely_aggregated(self) -> None:
        recording_id = UUID(int=20)
        recorded_at = datetime(2026, 9, 16, 19, 14, tzinfo=UTC)
        values = [
            entity(
                21,
                recording_id=recording_id,
                name="systolic_bp_mmhg",
                value="120",
                unit="mmHg",
                recorded_at=recorded_at,
            ),
            entity(
                22,
                recording_id=recording_id,
                name="temperature_c",
                value="36.8",
                unit="°C",
                recorded_at=recorded_at,
            ),
        ]
        db = SimpleNamespace(
            get=AsyncMock(return_value=self.encounter),
            scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: values)),
        )

        with patch("src.app.api.v1.encounters._scope", AsyncMock()):
            response = await get_vitals(self.encounter.id, SimpleNamespace(), db)

        self.assertEqual(len(response["data"]["items"]), 1)
        self.assertEqual(response["data"]["items"][0]["systolic_bp_mmhg"], 120)
        self.assertEqual(response["data"]["items"][0]["temperature_c"], 36.8)
        VitalsResponse.model_validate(response)

    async def test_frontend_vitals_payload_creates_one_grouped_recording(self) -> None:
        payload = TypeAdapter(VitalInput).validate_python(
            {
                "systolic": 120,
                "diastolic": 80,
                "heart_rate": 90,
                "temperature_c": 37,
                "height_cm": 168,
                "weight_kg": 64,
            }
        )
        self.assertIsInstance(payload, VitalSetCreate)
        db = SimpleNamespace(
            get=AsyncMock(return_value=self.encounter),
            add_all=Mock(),
            commit=AsyncMock(),
        )

        with (
            patch("src.app.api.v1.encounters._scope", AsyncMock()),
            patch("src.app.api.v1.encounters.record_audit", AsyncMock()),
        ):
            response = await create_vital(
                self.encounter.id,
                payload,
                SimpleNamespace(id=UUID(int=30)),
                db,
            )

        rows = db.add_all.call_args.args[0]
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({row.recording_id for row in rows}), 1)
        self.assertEqual(response["data"]["pulse_bpm"], 90)
        self.assertEqual(response["data"]["weight_kg"], 64)
        db.commit.assert_awaited_once()

    def test_openapi_exposes_read_response_models(self) -> None:
        app = FastAPI()
        app.include_router(encounters_router)
        app.include_router(clinical_router)
        paths = app.openapi()["paths"]

        for path, schema_name in (
            ("/encounters/{encounter_uuid}", "EncounterDetailResponse"),
            ("/encounters/{encounter_uuid}/vitals", "VitalsResponse"),
            ("/encounters/{encounter_uuid}/soap", "SoapNoteResponse"),
        ):
            schema = paths[path]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]
            self.assertTrue(schema["$ref"].endswith(schema_name))


if __name__ == "__main__":
    unittest.main()
