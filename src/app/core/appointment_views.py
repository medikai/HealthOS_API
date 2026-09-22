from typing import Any


def patient_view(patient: Any, person: Any) -> dict[str, Any] | None:
    if patient is None or person is None:
        return None
    name = f"{person.first_name} {person.last_name or ''}".strip()
    return {
        "uuid": str(patient.id),
        "display_name": name,
        "name": name,
        "medical_record_number": patient.mrn,
        "mrn": patient.mrn,
        "date_of_birth": person.date_of_birth,
        "gender": person.gender,
        "phone": person.phone,
    }


def practitioner_view(practitioner: Any) -> dict[str, Any] | None:
    if practitioner is None:
        return None
    return {
        "uuid": str(practitioner.id),
        "name": practitioner.person_name,
        "specialty": practitioner.specialty,
    }


def appointment_view(
    appointment: Any,
    patient: Any = None,
    person: Any = None,
    practitioner: Any = None,
    encounter_uuid: str | None = None,
) -> dict[str, Any]:
    resolved_encounter_uuid = (
        encounter_uuid
        or (str(eid) if (eid := getattr(appointment, "encounter_uuid", None) or getattr(appointment, "encounter_id", None)) else None)
    )
    return {
        "uuid": str(appointment.id),
        "encounter_uuid": resolved_encounter_uuid,
        "facility_uuid": str(appointment.facility_id),
        "patient_uuid": str(appointment.patient_id),
        "practitioner_uuid": str(appointment.practitioner_id),
        "resource_uuid": str(resource_id) if (resource_id := getattr(appointment, "resource_id", None)) else None,
        "patient": patient_view(patient, person),
        "practitioner": practitioner_view(practitioner),
        "scheduled_start": appointment.scheduled_start.isoformat(),
        "scheduled_end": appointment.scheduled_end.isoformat(),
        "status": appointment.status,
        "version": getattr(appointment, "version", 1),
        "reason_code": appointment.reason_code,
        "reason_text": appointment.reason_text,
    }
