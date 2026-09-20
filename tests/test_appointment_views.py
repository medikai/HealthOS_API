import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy.dialects import postgresql

from src.app.api.v1.queue import _queue_query
from src.app.api.v1.scheduling import _appointment_query
from src.app.core.appointment_views import appointment_view


class AppointmentViewTests(unittest.TestCase):
    def test_hydrates_canonical_patient_and_practitioner(self) -> None:
        appointment = SimpleNamespace(
            id=UUID("01a0ab6a-c712-720f-b0ae-24790d6535f5"),
            facility_id=UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d"),
            patient_id=UUID("01a0a94a-a8ca-73ea-880b-a3ea59622542"),
            practitioner_id=UUID("01a0a022-b31b-728f-b7ae-b06211a65893"),
            scheduled_start=datetime(2026, 9, 16, 4, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 16, 4, 30, tzinfo=UTC),
            status="booked",
            reason_code="examination",
            reason_text=None,
        )
        patient = SimpleNamespace(id=appointment.patient_id, mrn="MRN-123")
        person = SimpleNamespace(
            first_name="Aarav",
            last_name="Mehta",
            date_of_birth="1985-06-15",
            gender="male",
            phone="+919876543210",
        )
        practitioner = SimpleNamespace(
            id=appointment.practitioner_id,
            person_name="Dr. Ananya Kulkarni",
            specialty="Internal Medicine",
        )

        result = appointment_view(appointment, patient, person, practitioner)

        self.assertEqual(result["patient"]["display_name"], "Aarav Mehta")
        self.assertEqual(result["patient"]["name"], "Aarav Mehta")
        self.assertEqual(result["patient"]["medical_record_number"], "MRN-123")
        self.assertEqual(result["patient"]["mrn"], "MRN-123")
        self.assertEqual(result["patient"]["date_of_birth"], "1985-06-15")
        self.assertEqual(result["patient"]["gender"], "male")
        self.assertEqual(result["practitioner"]["name"], "Dr. Ananya Kulkarni")
        self.assertEqual(result["practitioner"]["specialty"], "Internal Medicine")
        self.assertEqual(result["patient_uuid"], str(patient.id))

    def test_missing_related_rows_are_explicitly_null(self) -> None:
        appointment = SimpleNamespace(
            id=UUID(int=1),
            facility_id=UUID(int=2),
            patient_id=UUID(int=3),
            practitioner_id=UUID(int=4),
            scheduled_start=datetime(2026, 9, 16, tzinfo=UTC),
            scheduled_end=datetime(2026, 9, 16, 0, 30, tzinfo=UTC),
            status="booked",
            reason_code=None,
            reason_text=None,
        )

        result = appointment_view(appointment)

        self.assertIsNone(result["patient"])
        self.assertIsNone(result["practitioner"])

    def test_read_queries_join_relations_with_tenant_and_facility_scope(self) -> None:
        organization_id, staff_member_id = UUID(int=1), UUID(int=2)

        for table, query in (
            ("appointment", _appointment_query(organization_id, staff_member_id)),
            ("queue_entry", _queue_query(organization_id, staff_member_id)),
        ):
            sql = str(query.compile(dialect=postgresql.dialect()))
            self.assertEqual(sql.count("LEFT OUTER JOIN"), 3)
            self.assertIn(
                f"identity.patient.organization_id = care.{table}.organization_id",
                sql,
            )
            self.assertIn(
                f"identity.practitioner.organization_id = care.{table}.organization_id",
                sql,
            )
            self.assertIn("EXISTS", sql)


if __name__ == "__main__":
    unittest.main()
