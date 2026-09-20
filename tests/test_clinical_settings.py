import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from src.app.api.v1.clinical import (
    _clinical_settings_scope,
    _format_clinical_setting_response,
    _get_default_clinical_settings,
    upsert_soap,
)
from src.app.schemas.clinical import (
    DocumentationSettingInput,
    DocumentationSettingsResponse,
    SoapInput,
)


class ClinicalSettingsTests(unittest.TestCase):
    def test_frontend_payload_and_custom_answers_are_supported(self) -> None:
        payload = DocumentationSettingInput.model_validate(
            {
                "facility_uuid": "all",
                "vitals_config": {"respiratory_rate": {"visible": False}},
                "soap_config": {
                    "objective": {
                        "systemic_exam": {
                            "visible": True,
                            "mandatory": False,
                            "default_state": "collapsed",
                        }
                    }
                },
                "custom_sections": [
                    {
                        "key": "lifestyle_notes",
                        "label": "Lifestyle notes",
                        "group": "plan",
                    }
                ],
            }
        )

        self.assertEqual(payload.custom_sections[0].input_type, "multiline_text")
        self.assertEqual(
            SoapInput(custom_fields={"lifestyle_notes": "Walk daily"}).custom_fields,
            {"lifestyle_notes": "Walk daily"},
        )
        self.assertIsNone(SoapInput(subjective="Legacy client").custom_fields)

    def test_unsafe_configuration_is_rejected(self) -> None:
        invalid_payloads = [
            {"soap_config": {"plan": {"unknown": {"visible": True}}}},
            {
                "custom_sections": [
                    {"key": "bad key", "label": "Bad", "group": "plan"}
                ]
            },
            {
                "custom_sections": [
                    {
                        "key": "hidden_required",
                        "label": "Bad",
                        "group": "plan",
                        "visible": False,
                        "required": True,
                    }
                ]
            },
            {
                "vitals_config": {
                    "bmi": {"visible": True},
                    "height": {"visible": False},
                }
            },
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                DocumentationSettingInput.model_validate(payload)

    def test_default_response_matches_the_settings_preview(self) -> None:
        defaults = _get_default_clinical_settings()
        response = _format_clinical_setting_response(None, UUID(int=1), None)

        self.assertEqual(response["vitals_config"], defaults["vitals_config"])
        self.assertEqual(response["soap_config"], defaults["soap_config"])
        self.assertEqual(response["source"], "default")
        DocumentationSettingsResponse.model_validate(
            {"success": True, "data": response, "meta": {}}
        )


class ClinicalSettingsAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_soap_save_preserves_custom_answers(self) -> None:
        encounter = SimpleNamespace(id=UUID(int=1))
        note = SimpleNamespace(
            id=UUID(int=2),
            encounter_id=encounter.id,
            status="draft",
            subjective="Old",
            objective="",
            assessment="",
            plan="",
            custom_fields={"lifestyle_notes": "Walk daily"},
            signed_at=None,
        )
        db = SimpleNamespace(
            scalar=AsyncMock(return_value=note),
            commit=AsyncMock(),
        )

        with (
            patch(
                "src.app.api.v1.clinical._encounter",
                AsyncMock(return_value=encounter),
            ),
            patch("src.app.api.v1.clinical._practitioner", AsyncMock()),
        ):
            await upsert_soap(
                encounter.id,
                SoapInput(subjective="Updated"),
                SimpleNamespace(),
                db,
            )

        self.assertEqual(note.custom_fields, {"lifestyle_notes": "Walk daily"})

    async def test_resuming_signed_soap_note_updates_data_without_conflict(self) -> None:
        encounter = SimpleNamespace(
            id=UUID(int=1),
            organization_id=UUID(int=10),
            facility_id=UUID(int=20),
            patient_id=UUID(int=30),
        )
        note = SimpleNamespace(
            id=UUID(int=2),
            encounter_id=encounter.id,
            status="signed",
            subjective="Previous subjective",
            objective="Previous objective",
            assessment="Previous assessment",
            plan="Previous plan",
            custom_fields={"chief_complaints": "Fever"},
            signed_at=None,
        )
        db = SimpleNamespace(
            scalar=AsyncMock(return_value=note),
            commit=AsyncMock(),
        )

        with (
            patch(
                "src.app.api.v1.clinical._encounter",
                AsyncMock(return_value=encounter),
            ),
            patch("src.app.api.v1.clinical._practitioner", AsyncMock()),
            patch("src.app.api.v1.clinical.record_audit", AsyncMock()) as mock_audit,
        ):
            response = await upsert_soap(
                encounter.id,
                SoapInput(
                    subjective="Updated subjective notes",
                    objective="Updated objective findings",
                    assessment="Updated assessment",
                    plan="Updated plan",
                    custom_fields={"chief_complaints": "Fever and cough"},
                ),
                SimpleNamespace(id=UUID(int=99)),
                db,
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["data"]["subjective"], "Updated subjective notes")
        self.assertEqual(response["data"]["objective"], "Updated objective findings")
        self.assertEqual(response["data"]["assessment"], "Updated assessment")
        self.assertEqual(response["data"]["plan"], "Updated plan")
        self.assertEqual(
            response["data"]["custom_fields"], {"chief_complaints": "Fever and cough"}
        )
        self.assertEqual(response["data"]["status"], "signed")
        mock_audit.assert_awaited_once()

    async def test_practitioner_can_edit_assigned_facility_but_not_organization(self) -> None:
        facility_id = UUID(int=2)
        organization = SimpleNamespace(id=UUID(int=1))
        assignment = SimpleNamespace(
            role_code="practitioner", facility_id=facility_id
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    all=lambda: [
                        (
                            organization.id,
                            assignment.role_code,
                            assignment.facility_id,
                            facility_id,
                        )
                    ]
                )
            )
        )

        account = SimpleNamespace(id=UUID(int=4))
        result = await _clinical_settings_scope(
            db, account, facility_id, write=True
        )
        self.assertEqual(result, organization.id)

        with self.assertRaises(HTTPException) as raised:
            await _clinical_settings_scope(db, account, None, write=True)
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
