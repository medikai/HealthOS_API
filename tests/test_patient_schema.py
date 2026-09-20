from src.app.schemas.patients import PatientCreate


def test_patient_create_accepts_flat_and_nested_person() -> None:
    flat = PatientCreate.model_validate({"first_name": "Jacob"})
    nested = PatientCreate.model_validate({"person": {"first_name": "Jacob"}})

    assert flat.normalized().first_name == nested.normalized().first_name == "Jacob"
