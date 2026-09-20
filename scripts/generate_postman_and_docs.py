"""
Generate HealthOS API Postman Collection (v2.1.0) and API_STATUS.md documentation.
Extracts schema directly from FastAPI app.openapi() and generates:
1. HealthOS_All_APIs.postman_collection.json (All endpoints with realistic requests and saved responses)
2. HealthOS_Staff_Invitation_Flow.postman_collection.json (Dedicated Staff Invitation & Onboarding Lifecycle)
3. API_STATUS.md (Complete API reference, schema mappings, and flow guides)
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.app.main import app

DOCS_DIR = ROOT_DIR / "docs"
DOCS_DIR.mkdir(exist_ok=True)

POSTMAN_ALL_OUTPUT = DOCS_DIR / "HealthOS_All_APIs.postman_collection.json"
POSTMAN_INVITE_OUTPUT = DOCS_DIR / "HealthOS_Staff_Invitation_Flow.postman_collection.json"
MARKDOWN_OUTPUT = DOCS_DIR / "API_STATUS.md"

# Standard collection variables
COMMON_VARIABLES = [
    {"key": "base_url", "value": "http://localhost:8000", "type": "string", "description": "Backend API base URL"},
    {"key": "csrf_token", "value": "sample-csrf-token", "type": "string", "description": "CSRF token from /auth/me or session"},
    {"key": "auth_token", "value": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sample-token", "type": "string", "description": "Bearer JWT for direct Authorization header"},
    {"key": "organization_id", "value": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee", "type": "string", "description": "Primary Organization UUID"},
    {"key": "facility_uuid", "value": "01a0b95d-d330-7887-8462-7dc6a0190fb8", "type": "string", "description": "Active Facility UUID"},
    {"key": "patient_uuid", "value": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22", "type": "string", "description": "Patient UUID"},
    {"key": "practitioner_uuid", "value": "d3eebc99-9c0b-4ef8-bb6d-6bb9bd380a44", "type": "string", "description": "Doctor / Practitioner UUID"},
    {"key": "appointment_uuid", "value": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33", "type": "string", "description": "Appointment UUID"},
    {"key": "encounter_uuid", "value": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "type": "string", "description": "Clinical Encounter UUID"},
    {"key": "queue_entry_uuid", "value": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77", "type": "string", "description": "Queue entry UUID"},
    {"key": "prescription_uuid", "value": "22eebc99-9c0b-4ef8-bb6d-6bb9bd380a88", "type": "string", "description": "Prescription UUID"},
    {"key": "staff_uuid", "value": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66", "type": "string", "description": "Staff Member UUID"},
    {"key": "invitation_token", "value": "sample-staff-invitation-token-abc123xyz", "type": "string", "description": "Token for invitation validation and accept"},
    {"key": "vital_uuid", "value": "55eebc99-9c0b-4ef8-bb6d-6bb9bd380a01", "type": "string", "description": "Vitals entry UUID"},
    {"key": "period_id", "value": "44eebc99-9c0b-4ef8-bb6d-6bb9bd380aaa", "type": "string", "description": "Facility Protected Period UUID"},
    {"key": "exception_uuid", "value": "33eebc99-9c0b-4ef8-bb6d-6bb9bd380a99", "type": "string", "description": "Availability Exception UUID"},
]

# Domain categorization rules
DOMAIN_RULES = [
    ("01. Health & System", lambda p, m: p in ["/api/v1/health", "/api/v1/ready"]),
    ("02. Authentication & Identity", lambda p, m: "/auth/" in p),
    ("03. Organizations & Multi-Tenancy", lambda p, m: "/organizations" in p),
    ("04. Bootstrap & Context", lambda p, m: p in ["/api/v1/me", "/api/v1/facilities", "/api/v1/platform/context"]),
    ("05. Patients", lambda p, m: "/patients" in p),
    ("06. Scheduling & Appointments", lambda p, m: ("/scheduling/" in p or "/appointments" in p or "/practitioners" in p) and "frontend-compat" not in p and "availability" not in p),
    ("07. Queue & Walk-ins", lambda p, m: "/queue" in p or "/walk-ins" in p),
    ("08. Encounters & Clinical Notes (SOAP)", lambda p, m: ("/encounters" in p and "/settings" not in p) or "/prescriptions" in p),
    ("09. Clinical Documentation Settings", lambda p, m: "/clinical" in p),
    ("10. Dashboard & Analytics", lambda p, m: "/dashboard" in p or "/stats" in p or "/masters" in p),
    ("11. Staff Administration & Invitations", lambda p, m: "/staff" in p or "/admin/staff" in p),
    ("12. Facility Scheduling & Protected Periods", lambda p, m: "/facilities/" in p and ("schedule" in p or "protected-periods" in p)),
    ("13. Events & Audit Logs", lambda p, m: "/events" in p or "/admin/audit-logs" in p),
    ("14. Frontend Compatibility", lambda p, m: "frontend-compat" in p or "availability" in p or p.startswith("/api/v1/api/")),
]

def get_folder_name(path: str, method: str) -> str:
    for folder, rule in DOMAIN_RULES:
        if rule(path, method):
            return folder
    return "15. Other Endpoints"

# Database schema mappings based on architecture
DB_SCHEMA_MAP = {
    "01. Health & System": "N/A (System / Cache / DB ping)",
    "02. Authentication & Identity": "`identity.user_account`, `identity.person`, Logto IdP",
    "03. Organizations & Multi-Tenancy": "`organization.organization`, `organization.facility`, `organization.department`",
    "04. Bootstrap & Context": "`identity`, `organization`, `platform`",
    "05. Patients": "`identity.patient`, `identity.person`, `care.encounter`",
    "06. Scheduling & Appointments": "`care.appointment`, `care.availability_rule`, `organization.practitioner`",
    "07. Queue & Walk-ins": "`care.queue_entry`, `care.appointment`, `identity.patient`",
    "08. Encounters & Clinical Notes (SOAP)": "`care.encounter`, `care.clinical_note`, `care.diagnosis`, `care.vital`, `care.prescription`",
    "09. Clinical Documentation Settings": "`care.clinical_documentation_setting`",
    "10. Dashboard & Analytics": "`care`, `organization`, `identity` (Read aggregations)",
    "11. Staff Administration & Invitations": "`organization.staff_member`, `organization.staff_assignment`, `organization.invitation`, `identity.user_account`",
    "12. Facility Scheduling & Protected Periods": "`care.facility_schedule`, `care.protected_period`",
    "13. Events & Audit Logs": "`governance.audit_log`, In-Memory / Redis Event Bus",
    "14. Frontend Compatibility": "`care.appointment`, `care.availability_rule`",
}

# Rich, realistic request payloads
EXAMPLE_REQUEST_BODIES = {
    ("POST", "/api/v1/auth/local/register"): {
        "email": "doctor.shukla@healthos.dev",
        "password": "Password123!",
        "full_name": "Dr. Nitin Shukla",
        "organization_name": "Apex Health Systems",
        "specialty": "GENERAL_PRACTICE"
    },
    ("POST", "/api/v1/auth/local/login"): {
        "email": "chilledoutnick@gmail.com",
        "password": "Password123!"
    },
    ("POST", "/api/v1/auth/logout"): {},
    ("POST", "/api/v1/organizations"): {
        "name": "Apex Multispecialty Clinic",
        "code": "APEX_MAIN",
        "country_code": "IN",
        "currency": "INR",
        "timezone": "Asia/Kolkata"
    },
    ("POST", "/api/v1/organizations/{organization_id}/departments"): {
        "name": "Cardiology & Internal Medicine",
        "code": "CARDIO_01"
    },
    ("POST", "/api/v1/organizations/{organization_id}/facilities"): {
        "name": "Apex City Center Clinic",
        "code": "APEX_CC",
        "address": "45 MG Road, Bangalore",
        "timezone": "Asia/Kolkata"
    },
    ("POST", "/api/v1/organizations/{organization_id}/features"): {
        "feature_code": "TELEHEALTH",
        "is_enabled": True
    },
    ("POST", "/api/v1/organizations/{organization_id}/members/roles"): {
        "member_uuid": "{{staff_uuid}}",
        "role_code": "practitioner"
    },
    ("POST", "/api/v1/patients"): {
        "first_name": "Robert",
        "last_name": "Chen",
        "date_of_birth": "1988-04-12",
        "gender": "male",
        "phone_number": "+919876543210",
        "email": "robert.chen@example.com",
        "address": "452 Market St, Indiranagar, Bangalore"
    },
    ("PATCH", "/api/v1/patients/{patient_uuid}"): {
        "phone_number": "+919876543211",
        "address": "789 Mission St, Bangalore"
    },
    ("POST", "/api/v1/appointments"): {
        "patient_id": "{{patient_uuid}}",
        "practitioner_id": "{{practitioner_uuid}}",
        "facility_id": "{{facility_uuid}}",
        "start_time": "2026-09-20T10:00:00Z",
        "end_time": "2026-09-20T10:30:00Z",
        "visit_reason": "General Consultation & Health Check",
        "appointment_type": "IN_PERSON"
    },
    ("POST", "/api/v1/appointments/{appointment_uuid}/cancel"): {
        "reason": "Patient requested reschedule due to conflict"
    },
    ("POST", "/api/v1/appointments/{appointment_uuid}/check-in"): {},
    ("POST", "/api/v1/appointments/{appointment_uuid}/no-show"): {
        "notes": "Patient did not answer confirmation call and failed to arrive"
    },
    ("POST", "/api/v1/appointments/{appointment_uuid}/reschedule"): {
        "new_start_time": "2026-09-21T11:00:00Z",
        "new_end_time": "2026-09-21T11:30:00Z",
        "reason": "Doctor schedule emergency shift"
    },
    ("POST", "/api/v1/appointments/{appointment_uuid}/start-consultation"): {},
    ("POST", "/api/v1/walk-ins"): {
        "patient_id": "{{patient_uuid}}",
        "facility_id": "{{facility_uuid}}",
        "chief_complaint": "Acute tension headache and low fever for 2 days",
        "priority": "STANDARD"
    },
    ("POST", "/api/v1/queue/{queue_entry_uuid}/call"): {},
    ("POST", "/api/v1/queue/{queue_entry_uuid}/cancel"): {
        "reason": "Patient left clinic before consultation"
    },
    ("POST", "/api/v1/queue/{queue_entry_uuid}/skip"): {
        "reason": "Patient temporarily stepped out"
    },
    ("POST", "/api/v1/queue/{queue_entry_uuid}/start-consultation"): {},
    ("POST", "/api/v1/encounters/{encounter_uuid}/complete"): {},
    ("PUT", "/api/v1/encounters/{encounter_uuid}/diagnoses"): {
        "diagnoses": [
            {
                "code": "G44.209",
                "description": "Tension-type headache, unspecified",
                "type": "PRIMARY",
                "status": "CONFIRMED"
            },
            {
                "code": "R50.9",
                "description": "Fever, unspecified",
                "type": "SECONDARY",
                "status": "PROVISIONAL"
            }
        ]
    },
    ("POST", "/api/v1/encounters/{encounter_uuid}/prescriptions"): {
        "medications": [
            {
                "drug_name": "Paracetamol 650mg",
                "dosage": "650 mg",
                "frequency": "TID (3 times daily)",
                "duration_days": 5,
                "instructions": "Take after meals with warm water"
            },
            {
                "drug_name": "Pantoprazole 40mg",
                "dosage": "40 mg",
                "frequency": "OD (Once daily)",
                "duration_days": 5,
                "instructions": "Take before breakfast"
            }
        ],
        "notes": "Review in 5 days if headache does not subside."
    },
    ("PUT", "/api/v1/encounters/{encounter_uuid}/prescriptions"): {
        "medications": [
            {
                "drug_name": "Paracetamol 650mg",
                "dosage": "650 mg",
                "frequency": "TID (3 times daily)",
                "duration_days": 3,
                "instructions": "Take after meals"
            }
        ],
        "notes": "Updated dosage to 3 days."
    },
    ("PUT", "/api/v1/prescriptions/{prescription_uuid}"): {
        "medications": [
            {
                "drug_name": "Paracetamol 650mg",
                "dosage": "650 mg",
                "frequency": "TID",
                "duration_days": 3,
                "instructions": "After food"
            }
        ],
        "notes": "Updated prescription"
    },
    ("POST", "/api/v1/prescriptions/{prescription_uuid}/sign"): {
        "signature_text": "Signed digitally by Dr. Nitin Shukla"
    },
    ("POST", "/api/v1/encounters/{encounter_uuid}/soap"): {
        "subjective": {
            "notes": "Patient reports persistent bilateral throbbing headache across forehead and temple for 3 days.",
            "chief_complaints": "Bilateral headache (6/10), mild eye strain, low-grade fever",
            "hpi": "Symptoms began on Thursday after prolonged computer screen use. Relieved partially by resting.",
            "family_social": "Software engineer, working 10+ hours daily. No family history of migraines."
        },
        "objective": {
            "notes": "Patient is conscious, alert, oriented to time, place, and person. No focal neurological deficit.",
            "general_exam": "In no acute distress, normocephalic, conjunctiva clear.",
            "systemic_exam": "Mild neck muscle tenderness. Cranial nerves intact. Deep tendon reflexes normal."
        },
        "assessment": {
            "notes": "Tension-type headache likely exacerbated by prolonged screen time and ergonomic strain.",
            "diagnoses": [
                {
                    "code": "G44.209",
                    "description": "Tension-type headache, unspecified",
                    "type": "PRIMARY",
                    "status": "CONFIRMED"
                }
            ]
        },
        "plan": {
            "notes": "Ergonomic corrections, 20-20-20 screen rule, adequate hydration (2.5L/day), Paracetamol 650mg PRN.",
            "general_plan": "Rest, avoid bright screen lights before sleep, follow-up if pain worsens.",
            "lifestyle": "Daily 30 minutes light aerobic walking, reduce late-night coffee intake."
        }
    },
    ("PUT", "/api/v1/encounters/{encounter_uuid}/soap"): {
        "subjective": {
            "notes": "Updated subjective notes with latest symptom duration."
        },
        "objective": {
            "notes": "Vitals stable, pupils reacting equally."
        },
        "assessment": {
            "notes": "Tension headache confirmed."
        },
        "plan": {
            "notes": "Continue Paracetamol 650mg SOS."
        }
    },
    ("POST", "/api/v1/encounters/{encounter_uuid}/soap/sign"): {
        "attestation_statement": "I attest that I have personally examined this patient and verified all clinical documentation."
    },
    ("POST", "/api/v1/encounters/{encounter_uuid}/vitals"): {
        "blood_pressure_systolic": 120,
        "blood_pressure_diastolic": 80,
        "pulse_rate": 72,
        "spo2": 99,
        "temperature": 98.4,
        "respiratory_rate": 16,
        "height_cm": 178,
        "weight_kg": 74
    },
    ("PATCH", "/api/v1/encounters/{encounter_uuid}/vitals/{vital_uuid}"): {
        "pulse_rate": 74,
        "temperature": 98.6
    },
    ("PUT", "/api/v1/clinical/documentation-settings"): {
        "facility_id": None,
        "specialty_code": "GENERAL_PRACTICE",
        "layout_mode": "soap",
        "is_active": True,
        "soap_config": {
            "subjective": {
                "general_notes": {"required": True, "default_state": "expanded"},
                "chief_complaints": {"visible": True, "mandatory": True, "default_state": "expanded"},
                "hpi": {"visible": True, "mandatory": False, "default_state": "expanded"},
                "family_social": {"visible": True, "mandatory": False, "default_state": "collapsed"}
            },
            "objective": {
                "general_exam": {"visible": True, "mandatory": False, "default_state": "expanded"},
                "systemic_exam": {"visible": True, "mandatory": False, "default_state": "collapsed"}
            },
            "assessment": {
                "diagnoses": {"visible": True, "mandatory": True, "default_state": "expanded"}
            },
            "plan": {
                "general_plan": {"visible": True, "mandatory": True, "default_state": "expanded"},
                "custom_sections": ["Lifestyle & Ergonomics Advice"]
            }
        }
    },
    ("POST", "/api/v1/clinical/documentation-settings/reset"): {
        "specialty_code": "GENERAL_PRACTICE"
    },
    ("PUT", "/api/v1/clinical/settings"): {
        "facility_id": None,
        "specialty_code": "GENERAL_PRACTICE",
        "layout_mode": "soap",
        "is_active": True,
        "soap_config": {
            "subjective": {
                "general_notes": {"required": True, "default_state": "expanded"},
                "chief_complaints": {"visible": True, "mandatory": True, "default_state": "expanded"}
            },
            "objective": {
                "general_exam": {"visible": True, "mandatory": False, "default_state": "expanded"}
            },
            "assessment": {
                "diagnoses": {"visible": True, "mandatory": True, "default_state": "expanded"}
            },
            "plan": {
                "general_plan": {"visible": True, "mandatory": True, "default_state": "expanded"}
            }
        }
    },
    ("POST", "/api/v1/clinical/settings/reset"): {
        "specialty_code": "GENERAL_PRACTICE"
    },
    ("POST", "/api/v1/staff/invitations"): {
        "email": "dr.smith@example.com",
        "full_name": "Dr. Sarah Smith",
        "role_code": "practitioner",
        "facility_uuid": "{{facility_uuid}}",
        "specialty": "Cardiology"
    },
    ("POST", "/api/v1/admin/staff/invitations"): {
        "email": "nurse.jane@example.com",
        "full_name": "Jane Doe",
        "role_code": "nurse",
        "facility_uuid": "{{facility_uuid}}",
        "specialty": "General Practice"
    },
    ("POST", "/api/v1/staff/invitations/accept"): {
        "token": "{{invitation_token}}",
        "password": "SecurePassword123!",
        "full_name": "Dr. Sarah Smith"
    },
    ("POST", "/api/v1/admin/staff/invitations/accept"): {
        "token": "{{invitation_token}}",
        "password": "SecurePassword123!",
        "full_name": "Jane Doe"
    },
    ("POST", "/api/v1/admin/staff"): {
        "user_account_uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef",
        "role_code": "practitioner",
        "facility_uuids": ["{{facility_uuid}}"]
    },
    ("PATCH", "/api/v1/admin/staff/{staff_uuid}"): {
        "display_name": "Dr. Nitin Shukla (Senior Consultant)",
        "email": "dr.shukla@healthos.dev"
    },
    ("POST", "/api/v1/admin/staff/{staff_uuid}/deactivate"): {},
    ("PUT", "/api/v1/admin/staff/{staff_uuid}/assignments"): {
        "assignments": ["{{facility_uuid}}"]
    },
    ("PUT", "/api/v1/admin/staff/{staff_uuid}/roles"): {
        "role_keys": ["organization_admin", "doctor"]
    },
    ("POST", "/api/v1/facilities/{facility_uuid}/protected-periods"): {
        "name": "Clinical Team Huddle & Disinfection Break",
        "start_time": "2026-09-20T13:00:00Z",
        "end_time": "2026-09-20T14:00:00Z",
        "reason": "Mandatory sanitization and shift handover"
    },
    ("PUT", "/api/v1/facilities/{facility_uuid}/schedule"): {
        "weekly_schedule": [
            {"day_of_week": 1, "open_time": "09:00", "close_time": "18:00"},
            {"day_of_week": 2, "open_time": "09:00", "close_time": "18:00"},
            {"day_of_week": 3, "open_time": "09:00", "close_time": "18:00"},
            {"day_of_week": 4, "open_time": "09:00", "close_time": "18:00"},
            {"day_of_week": 5, "open_time": "09:00", "close_time": "18:00"},
            {"day_of_week": 6, "open_time": "09:00", "close_time": "14:00"}
        ],
        "slot_duration_minutes": 15
    },
    ("POST", "/api/v1/scheduling/availability-exceptions"): {
        "practitioner_id": "{{practitioner_uuid}}",
        "facility_id": "{{facility_uuid}}",
        "date": "2026-09-25",
        "start_time": "09:00:00",
        "end_time": "13:00:00",
        "is_available": False,
        "reason": "Medical conference attendance"
    },
    ("PUT", "/api/v1/scheduling/availability-rules"): {
        "practitioner_id": "{{practitioner_uuid}}",
        "facility_id": "{{facility_uuid}}",
        "day_of_week": 1,
        "start_time": "09:00:00",
        "end_time": "17:00:00",
        "slot_duration_minutes": 15
    },
}

def get_example_request_body(method: str, path: str) -> dict:
    if (method, path) in EXAMPLE_REQUEST_BODIES:
        return EXAMPLE_REQUEST_BODIES[(method, path)]
    for (m, p), body in EXAMPLE_REQUEST_BODIES.items():
        if m == method and p.split("?")[0] == path.split("?")[0]:
            return body
    return {}

# Realistic saved responses mapped by endpoint patterns
def get_realistic_success_data(method: str, path: str) -> tuple[int, dict]:
    """Returns (status_code, response_data_dict)"""
    if path == "/api/v1/health":
        return 200, {"status": "healthy", "service": "healthos-api", "timestamp": "2026-09-20T10:00:00Z", "version": "1.0.0"}
    if path == "/api/v1/ready":
        return 200, {"database": "connected", "cache": "connected", "migrations": "up_to_date", "status": "ready"}
    
    # Auth endpoints
    if path == "/api/v1/auth/local/register":
        return 201, {
            "uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef",
            "email": "doctor.shukla@healthos.dev",
            "display_name": "Dr. Nitin Shukla",
            "organization_uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
            "role": "doctor"
        }
    if path == "/api/v1/auth/local/login":
        return 200, {
            "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIwMWEwYjk1Yy0zZmVkLTc4NWEtOTVlMC00OGNjOGQwN2YyZWYiLCJlbWFpbCI6ImNoaWxsZWRvdXRuaWNrQGdtYWlsLmNvbSIsIm5hbWUiOiJEci4gTml0aW4gU2h1a2xhIiwiZXhwIjoxNzg5ODE5ODc5LCJ0b2tlbl90eXBlIjoiYWNjZXNzIn0.sagYWgXNoH3595ZKm6LTfEpDWKvL56OTcx25Q1cpAqU",
            "token_type": "bearer",
            "user": {
                "uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef",
                "email": "chilledoutnick@gmail.com",
                "name": "Dr. Nitin Shukla"
            }
        }
    if path == "/api/v1/auth/logout":
        return 200, {"message": "Logged out successfully. HealthOS session terminated."}
    if path in ["/api/v1/auth/me", "/api/v1/me"]:
        return 200, {
            "user": {
                "uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef",
                "email": "chilledoutnick@gmail.com",
                "display_name": "Dr. Nitin Shukla",
                "role": "doctor",
                "is_active": True
            },
            "organization": {
                "uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
                "name": "Apex Health Care",
                "code": "APEX_HEALTH"
            },
            "facilities": [
                {
                    "uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                    "name": "Apex Main Hospital",
                    "code": "MAIN_01",
                    "is_primary": True
                }
            ],
            "roles": ["doctor", "organization_admin", "practitioner"],
            "csrf_token": "sample-csrf-token-998877"
        }
    if path == "/api/v1/platform/context":
        return 200, {
            "platform_version": "1.0.0",
            "environment": "production",
            "features_enabled": ["APPOINTMENTS", "QUEUE", "SOAP_NOTES", "PRESCRIPTIONS", "VITALS", "AUDIT_LOGS"],
            "timezone_default": "Asia/Kolkata"
        }

    # Organizations
    if path == "/api/v1/organizations":
        return 201 if method == "POST" else 200, {
            "uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
            "name": "Apex Multispecialty Clinic",
            "code": "APEX_MAIN",
            "country_code": "IN",
            "currency": "INR",
            "timezone": "Asia/Kolkata",
            "is_active": True
        }
    if "/departments" in path:
        return 201 if method == "POST" else 200, {
            "uuid": "d1eebc99-9c0b-4ef8-bb6d-6bb9bd380a01",
            "organization_id": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
            "name": "Cardiology & Internal Medicine",
            "code": "CARDIO_01"
        }
    if "/organizations/" in path and "/facilities" in path:
        return 201 if method == "POST" else 200, {
            "uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
            "organization_id": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
            "name": "Apex City Center Clinic",
            "code": "APEX_CC",
            "address": "45 MG Road, Bangalore",
            "timezone": "Asia/Kolkata",
            "is_active": True
        }
    if "/features" in path:
        return 200, {"feature_code": "TELEHEALTH", "status": "enabled", "assigned_at": "2026-09-20T10:00:00Z"}
    if "/members/roles" in path:
        return 200, {"member_uuid": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66", "role_code": "practitioner", "status": "assigned"}

    # Facilities
    if path == "/api/v1/facilities":
        return 200, {
            "items": [
                {
                    "uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                    "name": "Apex Main Hospital",
                    "code": "MAIN_01",
                    "address": "123 Healthcare Ave, Suite 100",
                    "timezone": "Asia/Kolkata",
                    "is_active": True,
                    "is_primary": True
                }
            ]
        }
    if "/schedule" in path:
        return 200, {
            "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
            "weekly_schedule": [
                {"day_of_week": 1, "open_time": "09:00", "close_time": "18:00"},
                {"day_of_week": 2, "open_time": "09:00", "close_time": "18:00"},
                {"day_of_week": 3, "open_time": "09:00", "close_time": "18:00"},
                {"day_of_week": 4, "open_time": "09:00", "close_time": "18:00"},
                {"day_of_week": 5, "open_time": "09:00", "close_time": "18:00"},
                {"day_of_week": 6, "open_time": "09:00", "close_time": "14:00"}
            ],
            "slot_duration_minutes": 15
        }
    if "/protected-periods" in path:
        if method == "DELETE":
            return 200, {"deleted": True, "period_id": "44eebc99-9c0b-4ef8-bb6d-6bb9bd380aaa"}
        return 201 if method == "POST" else 200, {
            "items": [
                {
                    "id": "44eebc99-9c0b-4ef8-bb6d-6bb9bd380aaa",
                    "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                    "name": "Clinical Team Huddle & Disinfection Break",
                    "start_time": "2026-09-20T13:00:00Z",
                    "end_time": "2026-09-20T14:00:00Z",
                    "reason": "Mandatory sanitization and shift handover"
                }
            ] if method == "GET" else {
                "id": "44eebc99-9c0b-4ef8-bb6d-6bb9bd380aaa",
                "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                "name": "Clinical Team Huddle & Disinfection Break",
                "start_time": "2026-09-20T13:00:00Z",
                "end_time": "2026-09-20T14:00:00Z"
            }
        }

    # Patients
    if path == "/api/v1/patients/search" or path == "/api/v1/patients/duplicates":
        return 200, {
            "items": [
                {
                    "uuid": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22",
                    "mrn": "MRN-2026-0042",
                    "first_name": "Robert",
                    "last_name": "Chen",
                    "gender": "male",
                    "date_of_birth": "1988-04-12",
                    "phone_number": "+919876543210",
                    "email": "robert.chen@example.com"
                }
            ],
            "total": 1
        }
    if "/patients/" in path and "/context" in path:
        return 200, {
            "patient_uuid": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22",
            "active_diagnoses": [{"code": "G44.209", "name": "Tension-type headache"}],
            "allergies": [{"allergen": "Penicillin", "severity": "Moderate"}],
            "recent_vitals": {"blood_pressure": "120/80 mmHg", "pulse_rate": 72, "spo2": 99},
            "last_encounter": {"uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "date": "2026-09-18T10:00:00Z"}
        }
    if "/patients/" in path and "/timeline" in path:
        return 200, {
            "timeline": [
                {"date": "2026-09-20T10:00:00Z", "event_type": "encounter", "title": "OPD Consultation", "practitioner": "Dr. Nitin Shukla"},
                {"date": "2026-09-18T14:30:00Z", "event_type": "appointment", "title": "Check-in & Vitals", "practitioner": "Dr. Sarah Smith"}
            ]
        }
    if path.startswith("/api/v1/patients"):
        return 201 if method == "POST" else 200, {
            "uuid": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22",
            "mrn": "MRN-2026-0042",
            "first_name": "Robert",
            "last_name": "Chen",
            "date_of_birth": "1988-04-12",
            "gender": "male",
            "phone_number": "+919876543210",
            "email": "robert.chen@example.com",
            "address": "452 Market St, Indiranagar, Bangalore",
            "created_at": "2026-09-20T10:00:00Z"
        }

    # Appointments & Scheduling
    if "availability" in path or "next-slots" in path:
        return 200, {
            "practitioner_id": "d3eebc99-9c0b-4ef8-bb6d-6bb9bd380a44",
            "facility_id": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
            "date": "2026-09-20",
            "available_slots": [
                {"start_time": "10:00:00", "end_time": "10:15:00", "is_available": True},
                {"start_time": "10:15:00", "end_time": "10:30:00", "is_available": True},
                {"start_time": "10:30:00", "end_time": "10:45:00", "is_available": False},
                {"start_time": "11:00:00", "end_time": "11:15:00", "is_available": True}
            ]
        }
    if "calendar" in path:
        return 200, {
            "date_range": {"from": "2026-09-20", "to": "2026-09-26"},
            "events": [
                {
                    "appointment_uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33",
                    "patient_name": "Robert Chen",
                    "practitioner_name": "Dr. Nitin Shukla",
                    "start_time": "2026-09-20T10:00:00Z",
                    "end_time": "2026-09-20T10:30:00Z",
                    "status": "CONFIRMED"
                }
            ]
        }
    if "availability-exceptions" in path:
        if method == "DELETE":
            return 200, {"deleted": True, "exception_uuid": "33eebc99-9c0b-4ef8-bb6d-6bb9bd380a99"}
        return 201 if method == "POST" else 200, {
            "uuid": "33eebc99-9c0b-4ef8-bb6d-6bb9bd380a99",
            "date": "2026-09-25",
            "start_time": "09:00:00",
            "end_time": "13:00:00",
            "is_available": False,
            "reason": "Medical conference attendance"
        }
    if "availability-rules" in path:
        return 200, {
            "practitioner_id": "d3eebc99-9c0b-4ef8-bb6d-6bb9bd380a44",
            "day_of_week": 1,
            "start_time": "09:00:00",
            "end_time": "17:00:00",
            "slot_duration_minutes": 15
        }
    if path.startswith("/api/v1/appointments"):
        if "/check-in" in path:
            return 200, {
                "uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33",
                "status": "CHECKED_IN",
                "checked_in_at": "2026-09-20T09:55:00Z",
                "queue_entry_uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77"
            }
        if "/cancel" in path:
            return 200, {"uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33", "status": "CANCELLED"}
        if "/reschedule" in path:
            return 200, {"uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33", "status": "CONFIRMED", "start_time": "2026-09-21T11:00:00Z"}
        if "/no-show" in path:
            return 200, {"uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33", "status": "NO_SHOW"}
        if "/start-consultation" in path:
            return 200, {
                "appointment_uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33",
                "encounter_uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
                "status": "IN_PROGRESS"
            }
        if method == "GET" and path == "/api/v1/appointments":
            return 200, {
                "items": [
                    {
                        "uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33",
                        "patient_id": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22",
                        "patient_name": "Robert Chen",
                        "practitioner_id": "d3eebc99-9c0b-4ef8-bb6d-6bb9bd380a44",
                        "practitioner_name": "Dr. Nitin Shukla",
                        "start_time": "2026-09-20T10:00:00Z",
                        "end_time": "2026-09-20T10:30:00Z",
                        "status": "CONFIRMED",
                        "visit_reason": "General Consultation & Health Check"
                    }
                ],
                "total": 1
            }
        return 201 if method == "POST" else 200, {
            "uuid": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33",
            "patient_id": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22",
            "practitioner_id": "d3eebc99-9c0b-4ef8-bb6d-6bb9bd380a44",
            "facility_id": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
            "start_time": "2026-09-20T10:00:00Z",
            "end_time": "2026-09-20T10:30:00Z",
            "status": "CONFIRMED",
            "appointment_type": "IN_PERSON",
            "visit_reason": "General Consultation & Health Check"
        }

    # Queue & Walk-ins
    if path == "/api/v1/walk-ins":
        return 201, {
            "queue_entry_uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77",
            "token_number": "W-042",
            "patient_id": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22",
            "priority": "STANDARD",
            "status": "WAITING",
            "estimated_wait_minutes": 15,
            "created_at": "2026-09-20T10:05:00Z"
        }
    if path.startswith("/api/v1/queue"):
        if "/call" in path:
            return 200, {"uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77", "token_number": "W-042", "status": "CALLED"}
        if "/cancel" in path:
            return 200, {"uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77", "status": "CANCELLED"}
        if "/skip" in path:
            return 200, {"uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77", "status": "SKIPPED"}
        if "/start-consultation" in path:
            return 200, {
                "uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77",
                "status": "IN_CONSULTATION",
                "encounter_uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
            }
        if method == "GET" and path == "/api/v1/queue":
            return 200, {
                "items": [
                    {
                        "uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77",
                        "token_number": "W-042",
                        "patient_name": "Robert Chen",
                        "status": "WAITING",
                        "priority": "STANDARD",
                        "wait_duration_minutes": 12,
                        "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8"
                    }
                ],
                "total_waiting": 1
            }
        return 200, {
            "uuid": "11eebc99-9c0b-4ef8-bb6d-6bb9bd380a77",
            "token_number": "W-042",
            "patient_name": "Robert Chen",
            "status": "WAITING"
        }

    # Encounters, SOAP, Vitals, Prescriptions
    if "/vitals" in path:
        return 201 if method == "POST" else 200, {
            "uuid": "55eebc99-9c0b-4ef8-bb6d-6bb9bd380a01",
            "encounter_uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "blood_pressure_systolic": 120,
            "blood_pressure_diastolic": 80,
            "pulse_rate": 72,
            "spo2": 99,
            "temperature": 98.4,
            "respiratory_rate": 16,
            "height_cm": 178,
            "weight_kg": 74,
            "bmi": 23.4,
            "recorded_at": "2026-09-20T10:10:00Z"
        }
    if "/soap/sign" in path:
        return 200, {
            "encounter_uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "is_signed": True,
            "signed_by": "Dr. Nitin Shukla",
            "signed_at": "2026-09-20T10:25:00Z"
        }
    if "/soap" in path:
        return 200, {
            "encounter_uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "subjective": {
                "notes": "Patient reports persistent bilateral throbbing headache across forehead for 3 days.",
                "chief_complaints": "Bilateral headache (6/10), mild eye strain",
                "hpi": "Onset after prolonged computer screen use.",
                "family_social": "Software engineer, no history of migraines."
            },
            "objective": {
                "notes": "Conscious, alert, oriented x3. Neck supple, mild trapezius tightness.",
                "general_exam": "In no acute distress, normocephalic.",
                "systemic_exam": "Cranial nerves intact, reflexes normal."
            },
            "assessment": {
                "notes": "Tension-type headache related to posture and screen fatigue.",
                "diagnoses": [
                    {
                        "code": "G44.209",
                        "description": "Tension-type headache, unspecified",
                        "type": "PRIMARY",
                        "status": "CONFIRMED"
                    }
                ]
            },
            "plan": {
                "notes": "Paracetamol 650mg PRN, 20-20-20 screen rule, posture ergonomics.",
                "general_plan": "Rest, adequate hydration (2.5L/day), follow-up if symptoms persist.",
                "lifestyle": "30 mins daily walking, reduce evening caffeine."
            },
            "is_signed": False
        }
    if "/diagnoses" in path:
        return 200, {
            "items": [
                {
                    "code": "G44.209",
                    "description": "Tension-type headache, unspecified",
                    "type": "PRIMARY",
                    "status": "CONFIRMED"
                }
            ]
        }
    if "/prescriptions" in path:
        if "/document" in path:
            return 200, {
                "prescription_uuid": "22eebc99-9c0b-4ef8-bb6d-6bb9bd380a88",
                "pdf_url": "https://api.medikai.in/prescriptions/22eebc99-9c0b-4ef8-bb6d-6bb9bd380a88.pdf",
                "html_preview": "<div>Prescription for Robert Chen</div>"
            }
        if "/sign" in path:
            return 200, {
                "uuid": "22eebc99-9c0b-4ef8-bb6d-6bb9bd380a88",
                "status": "SIGNED",
                "signed_at": "2026-09-20T10:28:00Z",
                "signed_by": "Dr. Nitin Shukla"
            }
        return 201 if method == "POST" else 200, {
            "uuid": "22eebc99-9c0b-4ef8-bb6d-6bb9bd380a88",
            "encounter_uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "status": "DRAFT",
            "medications": [
                {
                    "drug_name": "Paracetamol 650mg",
                    "dosage": "650 mg",
                    "frequency": "TID (3 times daily)",
                    "duration_days": 5,
                    "instructions": "Take after meals with warm water"
                },
                {
                    "drug_name": "Pantoprazole 40mg",
                    "dosage": "40 mg",
                    "frequency": "OD (Once daily)",
                    "duration_days": 5,
                    "instructions": "Take before breakfast"
                }
            ],
            "notes": "Review in 5 days if headache does not subside."
        }
    if path.startswith("/api/v1/encounters"):
        if "/complete" in path:
            return 200, {
                "uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
                "status": "COMPLETED",
                "completed_at": "2026-09-20T10:30:00Z"
            }
        return 200, {
            "uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "patient_id": "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22",
            "practitioner_id": "d3eebc99-9c0b-4ef8-bb6d-6bb9bd380a44",
            "facility_id": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
            "status": "IN_PROGRESS",
            "started_at": "2026-09-20T10:05:00Z"
        }

    # Clinical Settings
    if "clinical" in path and ("settings" in path or "documentation-settings" in path):
        return 200, {
            "specialty_code": "GENERAL_PRACTICE",
            "layout_mode": "soap",
            "soap_config": {
                "subjective": {"general_notes": {"required": True, "default_state": "expanded"}},
                "objective": {"general_exam": {"visible": True, "mandatory": False}},
                "assessment": {"diagnoses": {"visible": True, "mandatory": True}},
                "plan": {"general_plan": {"visible": True, "mandatory": True}}
            },
            "is_active": True
        }

    # Staff Invitations
    if "invitations/validate" in path:
        return 200, {
            "valid": True,
            "email": "dr.smith@example.com",
            "full_name": "Dr. Sarah Smith",
            "role_code": "practitioner",
            "organization_name": "Apex Health Systems",
            "organization_id": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
            "facility_name": "Apex Main Hospital",
            "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
            "expires_at": "2026-09-27T10:00:00Z"
        }
    if "invitations/accept" in path:
        return 200, {
            "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sample-new-staff-token",
            "token_type": "bearer",
            "user": {
                "uuid": "01a0b95f-1234-7890-abcd-ef0123456789",
                "email": "dr.smith@example.com",
                "display_name": "Dr. Sarah Smith",
                "role_code": "practitioner"
            }
        }
    if "invitations" in path:
        if method == "POST":
            return 201, {
                "id": "7b0a8c23-4e89-4a90-b184-904d9c79e612",
                "email": "dr.smith@example.com",
                "full_name": "Dr. Sarah Smith",
                "role_code": "practitioner",
                "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                "token": "dGhpcy1pcy1hLXNhbXBsZS1pbnZpdGF0aW9uLXRva2Vu",
                "expires_at": "2026-09-27T10:00:00Z",
                "status": "pending",
                "invite_url": "/staff/accept?token=dGhpcy1pcy1hLXNhbXBsZS1pbnZpdGF0aW9uLXRva2Vu"
            }
        return 200, {
            "items": [
                {
                    "id": "7b0a8c23-4e89-4a90-b184-904d9c79e612",
                    "email": "dr.smith@example.com",
                    "full_name": "Dr. Sarah Smith",
                    "role_code": "practitioner",
                    "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                    "status": "pending",
                    "token": "dGhpcy1pcy1hLXNhbXBsZS1pbnZpdGF0aW9uLXRva2Vu",
                    "expires_at": "2026-09-27T10:00:00Z",
                    "created_at": "2026-09-20T10:00:00Z"
                }
            ]
        }

    # Staff Admin
    if "/admin/staff" in path:
        if "/deactivate" in path:
            return 200, {"uuid": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66", "status": "inactive"}
        if "/assignments" in path:
            if method == "PUT":
                return 200, {"facility_uuids": ["01a0b95d-d330-7887-8462-7dc6a0190fb8"]}
            return 200, {
                "items": [
                    {
                        "uuid": "88eebc99-9c0b-4ef8-bb6d-6bb9bd380a99",
                        "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                        "role_code": "practitioner"
                    }
                ]
            }
        if "/roles" in path:
            return 200, {"role_keys": ["organization_admin", "doctor"], "logto_user_id": "local:dr.shukla@healthos.dev"}
        if method == "GET" and path == "/api/v1/admin/staff":
            return 200, {
                "items": [
                    {
                        "uuid": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66",
                        "user_uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef",
                        "display_name": "Dr. Nitin Shukla",
                        "email": "chilledoutnick@gmail.com",
                        "status": "active",
                        "role_codes": ["doctor", "organization_admin"],
                        "facility_uuids": ["01a0b95d-d330-7887-8462-7dc6a0190fb8"]
                    }
                ]
            }
        if method == "POST":
            return 201, {"uuid": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66", "user_uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef"}
        return 200, {
            "uuid": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66",
            "user_uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef",
            "display_name": "Dr. Nitin Shukla",
            "email": "chilledoutnick@gmail.com",
            "status": "active",
            "role_codes": ["doctor", "organization_admin"],
            "facility_uuids": ["01a0b95d-d330-7887-8462-7dc6a0190fb8"]
        }

    # Dashboard & Stats
    if path in ["/api/v1/dashboard/today", "/api/v1/stats"]:
        return 200, {
            "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
            "facility_name": "Apex Main Hospital",
            "appointments_total": 18,
            "completed": 12,
            "in_progress": 2,
            "waiting_in_queue": 3,
            "no_show": 1,
            "active_doctors": 4,
            "revenue_today": 24500.00
        }

    # Masters
    if "/masters/" in path:
        if "specialties" in path:
            return 200, {
                "items": [
                    {"code": "GENERAL_PRACTICE", "name": "General Practice"},
                    {"code": "CARDIOLOGY", "name": "Cardiology"},
                    {"code": "PEDIATRICS", "name": "Pediatrics"},
                    {"code": "DERMATOLOGY", "name": "Dermatology"},
                    {"code": "ORTHOPEDICS", "name": "Orthopedics"}
                ]
            }
        if "access-roles" in path:
            return 200, {
                "items": [
                    {"code": "organization_admin", "name": "Organization Administrator"},
                    {"code": "doctor", "name": "Doctor / Practitioner"},
                    {"code": "nurse", "name": "Nurse / Clinical Assistant"},
                    {"code": "receptionist", "name": "Front Desk Receptionist"},
                    {"code": "billing_operator", "name": "Billing Operator"}
                ]
            }
        if "staff-designations" in path:
            return 200, {
                "items": [
                    {"code": "CHIEF_MEDICAL_OFFICER", "name": "Chief Medical Officer"},
                    {"code": "SENIOR_CONSULTANT", "name": "Senior Consultant"},
                    {"code": "RESIDENT_PHYSICIAN", "name": "Resident Physician"},
                    {"code": "HEAD_NURSE", "name": "Head Nurse"},
                    {"code": "CLINICAL_COORDINATOR", "name": "Clinical Coordinator"}
                ]
            }
        if "visit-reasons" in path:
            return 200, {
                "items": [
                    {"code": "GENERAL_CONSULTATION", "name": "General Consultation"},
                    {"code": "FOLLOW_UP", "name": "Follow-up Consultation"},
                    {"code": "ROUTINE_CHECKUP", "name": "Routine Health Checkup"},
                    {"code": "PRESCRIPTION_REFILL", "name": "Prescription Refill"},
                    {"code": "VACCINATION", "name": "Vaccination"}
                ]
            }

    # Practitioners
    if path.startswith("/api/v1/practitioners"):
        return 200, {
            "items": [
                {
                    "uuid": "d3eebc99-9c0b-4ef8-bb6d-6bb9bd380a44",
                    "name": "Dr. Nitin Shukla",
                    "specialty": "General Practice",
                    "facility_ids": ["01a0b95d-d330-7887-8462-7dc6a0190fb8"],
                    "is_active": True
                }
            ]
        }

    # Events
    if "/events/stream" in path:
        return 200, {"event_type": "appointment.created", "payload": {"appointment_id": "c2eebc99-9c0b-4ef8-bb6d-6bb9bd380a33"}}

    # Generic fallback with realistic UUID
    return 200, {
        "uuid": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "status": "success",
        "message": "Operation completed successfully"
    }

def build_postman_collection(openapi_spec):
    paths = openapi_spec.get("paths", {})
    
    collection = {
        "info": {
            "_postman_id": "healthos-api-all-endpoints-2026",
            "name": "HealthOS API - Complete Collection",
            "description": "Comprehensive, production-ready Postman collection for all 78 HealthOS endpoints across identity, organization, scheduling, care/encounters/SOAP, governance, and administration. Every endpoint includes realistic request bodies and saved 200/201 Success and 400/404 Error responses.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
        },
        "variable": COMMON_VARIABLES,
        "item": []
    }

    folders = {}
    for folder_name, _ in DOMAIN_RULES:
        folders[folder_name] = []
    folders["15. Other Endpoints"] = []

    for path, path_item in sorted(paths.items()):
        if path in ["/docs", "/redoc", "/openapi.json"]:
            continue
        
        for method_lower, op_data in path_item.items():
            if method_lower not in ["get", "post", "put", "patch", "delete"]:
                continue
            
            method = method_lower.upper()
            summary = op_data.get("summary") or op_data.get("operationId") or f"{method} {path}"
            description = op_data.get("description") or summary
            
            folder = get_folder_name(path, method)

            # Convert OpenAPI path parameters {param} to Postman :param
            url_path_segments = [seg for seg in path.strip("/").split("/") if seg]
            postman_path = []
            path_variables = []
            
            for seg in url_path_segments:
                if seg.startswith("{") and seg.endswith("}"):
                    param_name = seg[1:-1]
                    postman_path.append(f":{param_name}")
                    path_variables.append({
                        "key": param_name,
                        "value": f"{{{{{param_name}}}}}",
                        "description": f"Target {param_name}"
                    })
                else:
                    postman_path.append(seg)

            headers = [
                {"key": "Accept", "value": "application/json", "type": "text"}
            ]
            if method in ["POST", "PUT", "PATCH", "DELETE"]:
                headers.append({"key": "Content-Type", "value": "application/json", "type": "text"})
                headers.append({"key": "X-CSRF-Token", "value": "{{csrf_token}}", "type": "text"})
            
            # Auth header
            if "/auth/login" not in path and "/auth/register" not in path and "invitations/validate" not in path and "invitations/accept" not in path and path not in ["/api/v1/health", "/api/v1/ready"]:
                headers.append({"key": "Authorization", "value": "Bearer {{auth_token}}", "type": "text", "description": "BFF JWT Bearer Token or cookie equivalent"})

            # Query params
            query_params = []
            for param in op_data.get("parameters", []):
                if param.get("in") == "query":
                    param_name = param.get("name")
                    default_val = str(param.get("schema", {}).get("default", ""))
                    if not default_val:
                        if "facility" in param_name:
                            default_val = "{{facility_uuid}}"
                        elif "date" in param_name:
                            default_val = "2026-09-20"
                        elif "practitioner" in param_name:
                            default_val = "{{practitioner_uuid}}"
                        elif "token" in param_name:
                            default_val = "{{invitation_token}}"
                    query_params.append({
                        "key": param_name,
                        "value": default_val,
                        "description": param.get("description", ""),
                        "disabled": not param.get("required", False) and default_val == ""
                    })

            # Body
            req_body = None
            if method in ["POST", "PUT", "PATCH"]:
                sample_body = get_example_request_body(method, path)
                req_body = {
                    "mode": "raw",
                    "raw": json.dumps(sample_body, indent=2),
                    "options": {"raw": {"language": "json"}}
                }

            # Realistic Saved Responses
            success_status_code, success_data = get_realistic_success_data(method, path)
            status_text = "OK" if success_status_code == 200 else "Created"
            
            saved_responses = [
                {
                    "name": f"Success Response ({success_status_code} {status_text})",
                    "originalRequest": {
                        "method": method,
                        "header": headers,
                        "url": {
                            "raw": "{{base_url}}/" + "/".join(postman_path),
                            "host": ["{{base_url}}"],
                            "path": postman_path
                        }
                    },
                    "status": status_text,
                    "code": success_status_code,
                    "_postman_previewlanguage": "json",
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": json.dumps({
                        "success": True,
                        "data": success_data,
                        "meta": {
                            "request_id": f"req-success-{abs(hash(path + method)) % 100000:05d}"
                        }
                    }, indent=2)
                },
                {
                    "name": "Validation Error (400 Bad Request)",
                    "originalRequest": {
                        "method": method,
                        "header": headers,
                        "url": {
                            "raw": "{{base_url}}/" + "/".join(postman_path),
                            "host": ["{{base_url}}"],
                            "path": postman_path
                        }
                    },
                    "status": "Bad Request",
                    "code": 400,
                    "_postman_previewlanguage": "json",
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": json.dumps({
                        "success": False,
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "The request body failed validation constraints or required fields are missing.",
                            "details": [
                                {
                                    "loc": ["body", "required_field"],
                                    "msg": "field required",
                                    "type": "value_error.missing"
                                }
                            ]
                        },
                        "meta": {
                            "request_id": f"req-err-{abs(hash(path + method)) % 100000:05d}"
                        }
                    }, indent=2)
                }
            ]

            # Add 401 Unauthorized for protected endpoints
            if "/auth/login" not in path and "/auth/register" not in path and "invitations/validate" not in path and "invitations/accept" not in path and path not in ["/api/v1/health", "/api/v1/ready"]:
                saved_responses.append({
                    "name": "Unauthorized Error (401 Unauthorized)",
                    "originalRequest": {
                        "method": method,
                        "header": headers,
                        "url": {
                            "raw": "{{base_url}}/" + "/".join(postman_path),
                            "host": ["{{base_url}}"],
                            "path": postman_path
                        }
                    },
                    "status": "Unauthorized",
                    "code": 401,
                    "_postman_previewlanguage": "json",
                    "header": [{"key": "Content-Type", "value": "application/json"}],
                    "body": json.dumps({
                        "success": False,
                        "error": {
                            "code": "AUTHENTICATION_REQUIRED",
                            "message": "A valid authenticated session or Bearer token is required to access this resource.",
                            "details": []
                        },
                        "meta": {
                            "request_id": f"req-unauth-{abs(hash(path + method)) % 100000:05d}"
                        }
                    }, indent=2)
                })

            item_def = {
                "name": summary,
                "request": {
                    "method": method,
                    "header": headers,
                    "url": {
                        "raw": "{{base_url}}/" + "/".join(postman_path),
                        "host": ["{{base_url}}"],
                        "path": postman_path,
                        "variable": path_variables,
                        "query": query_params
                    },
                    "description": description
                },
                "response": saved_responses
            }
            if req_body:
                item_def["request"]["body"] = req_body

            folders[folder].append(item_def)

    for folder_name, items in folders.items():
        if items:
            collection["item"].append({
                "name": folder_name,
                "item": items,
                "description": f"Endpoints in domain: {folder_name}"
            })

    return collection

def build_staff_invitation_flow_collection():
    """
    Build dedicated, end-to-end Staff Invitation & Onboarding Lifecycle Postman collection.
    Includes automated chaining scripts and rich saved responses for every step.
    """
    collection = {
        "info": {
            "_postman_id": "healthos-staff-invitation-flow-2026",
            "name": "HealthOS - Staff Invitation & Onboarding Flow",
            "description": "Dedicated end-to-end lifecycle collection for Staff Invitations, Token Validation, Account Acceptance/Provisioning, and Post-Acceptance Session Verification. Pre-configured with automatic variable propagation (invitation_token, staff_access_token) and complete saved responses for every step.",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
        },
        "variable": [
            {"key": "base_url", "value": "http://localhost:8000", "type": "string", "description": "Backend API base URL"},
            {"key": "admin_access_token", "value": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sample-admin-token", "type": "string", "description": "Admin Bearer token"},
            {"key": "csrf_token", "value": "sample-csrf-token", "type": "string", "description": "CSRF Token"},
            {"key": "facility_uuid", "value": "01a0b95d-d330-7887-8462-7dc6a0190fb8", "type": "string", "description": "Target Facility UUID"},
            {"key": "invited_email", "value": "dr.sarah.smith@example.com", "type": "string", "description": "Email address of invited practitioner"},
            {"key": "invitation_token", "value": "dGhpcy1pcy1hLXNhbXBsZS1pbnZpdGF0aW9uLXRva2Vu", "type": "string", "description": "Invitation token dynamically extracted from Step 1"},
            {"key": "staff_access_token", "value": "sample-staff-access-token-step4", "type": "string", "description": "Access token dynamically extracted from Step 4"},
            {"key": "new_staff_uuid", "value": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66", "type": "string", "description": "New staff UUID from Step 4"},
        ],
        "item": [
            {
                "name": "Phase 1: Admin Invitation Actions",
                "description": "Administrator creates and manages pending staff invitations.",
                "item": [
                    {
                        "name": "01. [Admin] Send Staff Invitation",
                        "event": [
                            {
                                "listen": "test",
                                "script": {
                                    "type": "text/javascript",
                                    "exec": [
                                        "if (pm.response.code === 201 || pm.response.code === 200) {",
                                        "    var jsonData = pm.response.json();",
                                        "    if (jsonData.data && jsonData.data.token) {",
                                        "        pm.collectionVariables.set('invitation_token', jsonData.data.token);",
                                        "        console.log('✅ Captured invitation_token:', jsonData.data.token);",
                                        "    }",
                                        "}"
                                    ]
                                }
                            }
                        ],
                        "request": {
                            "method": "POST",
                            "header": [
                                {"key": "Content-Type", "value": "application/json", "type": "text"},
                                {"key": "Accept", "value": "application/json", "type": "text"},
                                {"key": "Authorization", "value": "Bearer {{admin_access_token}}", "type": "text"},
                                {"key": "X-CSRF-Token", "value": "{{csrf_token}}", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/staff/invitations",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "staff", "invitations"]
                            },
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps({
                                    "email": "{{invited_email}}",
                                    "full_name": "Dr. Sarah Smith",
                                    "role_code": "practitioner",
                                    "facility_uuid": "{{facility_uuid}}",
                                    "specialty": "Cardiology"
                                }, indent=2),
                                "options": {"raw": {"language": "json"}}
                            },
                            "description": "Admin generates a new staff invitation with role assignment and target facility. Generates a secure URL-safe 7-day token and invite link."
                        },
                        "response": [
                            {
                                "name": "Invitation Created (201 Created)",
                                "status": "Created",
                                "code": 201,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "id": "7b0a8c23-4e89-4a90-b184-904d9c79e612",
                                        "email": "dr.sarah.smith@example.com",
                                        "full_name": "Dr. Sarah Smith",
                                        "role_code": "practitioner",
                                        "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                                        "token": "dGhpcy1pcy1hLXNhbXBsZS1pbnZpdGF0aW9uLXRva2Vu",
                                        "expires_at": "2026-09-27T10:00:00Z",
                                        "status": "pending",
                                        "invite_url": "/staff/accept?token=dGhpcy1pcy1hLXNhbXBsZS1pbnZpdGF0aW9uLXRva2Vu"
                                    },
                                    "meta": {"request_id": "req-inv-001"}
                                }, indent=2)
                            },
                            {
                                "name": "Forbidden: Non-Admin Caller (403 Forbidden)",
                                "status": "Forbidden",
                                "code": 403,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": False,
                                    "error": {
                                        "code": "FORBIDDEN",
                                        "message": "Staff administration access is required.",
                                        "details": []
                                    },
                                    "meta": {"request_id": "req-inv-err-403"}
                                }, indent=2)
                            }
                        ]
                    },
                    {
                        "name": "02. [Admin] List Pending Invitations",
                        "request": {
                            "method": "GET",
                            "header": [
                                {"key": "Accept", "value": "application/json", "type": "text"},
                                {"key": "Authorization", "value": "Bearer {{admin_access_token}}", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/staff/invitations",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "staff", "invitations"]
                            },
                            "description": "Admin retrieves all sent invitations and their current status (pending, accepted, expired)."
                        },
                        "response": [
                            {
                                "name": "Active Invitations List (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "items": [
                                            {
                                                "id": "7b0a8c23-4e89-4a90-b184-904d9c79e612",
                                                "email": "dr.sarah.smith@example.com",
                                                "full_name": "Dr. Sarah Smith",
                                                "role_code": "practitioner",
                                                "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                                                "status": "pending",
                                                "token": "dGhpcy1pcy1hLXNhbXBsZS1pbnZpdGF0aW9uLXRva2Vu",
                                                "expires_at": "2026-09-27T10:00:00Z",
                                                "created_at": "2026-09-20T10:00:00Z"
                                            }
                                        ]
                                    },
                                    "meta": {"count": 1, "request_id": "req-inv-002"}
                                }, indent=2)
                            }
                        ]
                    }
                ]
            },
            {
                "name": "Phase 2: Invited Staff Acceptance",
                "description": "Invited doctor or staff member opens the email link, validates token, sets password, and receives authenticated credentials.",
                "item": [
                    {
                        "name": "03. [Invited User] Validate Invitation Token (Public Link)",
                        "request": {
                            "method": "GET",
                            "header": [
                                {"key": "Accept", "value": "application/json", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/staff/invitations/validate?token={{invitation_token}}",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "staff", "invitations", "validate"],
                                "query": [
                                    {"key": "token", "value": "{{invitation_token}}", "description": "Invitation verification token"}
                                ]
                            },
                            "description": "Public verification endpoint called when user clicks the invite email link. Checks expiry and displays clinic name and assigned facility."
                        },
                        "response": [
                            {
                                "name": "Valid Token (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "valid": True,
                                        "email": "dr.sarah.smith@example.com",
                                        "full_name": "Dr. Sarah Smith",
                                        "role_code": "practitioner",
                                        "organization_name": "Apex Health Systems",
                                        "organization_id": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
                                        "facility_name": "Apex Main Hospital",
                                        "facility_uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                                        "expires_at": "2026-09-27T10:00:00Z"
                                    },
                                    "meta": {"request_id": "req-inv-003"}
                                }, indent=2)
                            },
                            {
                                "name": "Invalid or Expired Token (404/400)",
                                "status": "Not Found",
                                "code": 404,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": False,
                                    "error": {
                                        "code": "NOT_FOUND",
                                        "message": "Invitation not found or invalid.",
                                        "details": []
                                    },
                                    "meta": {"request_id": "req-inv-err-404"}
                                }, indent=2)
                            }
                        ]
                    },
                    {
                        "name": "04. [Invited User] Accept Invitation & Set Password",
                        "event": [
                            {
                                "listen": "test",
                                "script": {
                                    "type": "text/javascript",
                                    "exec": [
                                        "if (pm.response.code === 200 || pm.response.code === 201) {",
                                        "    var jsonData = pm.response.json();",
                                        "    if (jsonData.data && jsonData.data.access_token) {",
                                        "        pm.collectionVariables.set('staff_access_token', jsonData.data.access_token);",
                                        "        pm.collectionVariables.set('new_staff_uuid', jsonData.data.user.uuid);",
                                        "        console.log('✅ Captured staff_access_token and user uuid:', jsonData.data.user.uuid);",
                                        "    }",
                                        "}"
                                    ]
                                }
                            }
                        ],
                        "request": {
                            "method": "POST",
                            "header": [
                                {"key": "Content-Type", "value": "application/json", "type": "text"},
                                {"key": "Accept", "value": "application/json", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/staff/invitations/accept",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "staff", "invitations", "accept"]
                            },
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps({
                                    "token": "{{invitation_token}}",
                                    "password": "SecurePassword123!",
                                    "full_name": "Dr. Sarah Smith"
                                }, indent=2),
                                "options": {"raw": {"language": "json"}}
                            },
                            "description": "Public acceptance endpoint. Provisions UserAccount, activates StaffMember, links Practitioner record, and returns immediate Bearer access token."
                        },
                        "response": [
                            {
                                "name": "Invitation Accepted & Provisioned (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sample-new-staff-token",
                                        "token_type": "bearer",
                                        "user": {
                                            "uuid": "01a0b95f-1234-7890-abcd-ef0123456789",
                                            "email": "dr.sarah.smith@example.com",
                                            "display_name": "Dr. Sarah Smith",
                                            "role_code": "practitioner"
                                        }
                                    },
                                    "meta": {"request_id": "req-inv-004"}
                                }, indent=2)
                            },
                            {
                                "name": "Already Accepted or Expired (400 Bad Request)",
                                "status": "Bad Request",
                                "code": 400,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": False,
                                    "error": {
                                        "code": "BAD_REQUEST",
                                        "message": "Invitation has already been accepted.",
                                        "details": []
                                    },
                                    "meta": {"request_id": "req-inv-err-400"}
                                }, indent=2)
                            }
                        ]
                    }
                ]
            },
            {
                "name": "Phase 3: Post-Acceptance Verification",
                "description": "Using the newly minted staff_access_token, verify session context and accessible facilities.",
                "item": [
                    {
                        "name": "05. [Invited User] Verify Authenticated Session (/auth/me)",
                        "request": {
                            "method": "GET",
                            "header": [
                                {"key": "Accept", "value": "application/json", "type": "text"},
                                {"key": "Authorization", "value": "Bearer {{staff_access_token}}", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/auth/me",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "auth", "me"]
                            },
                            "description": "Verifies that the new staff member is fully authenticated and retrieves organization, roles, and CSRF token."
                        },
                        "response": [
                            {
                                "name": "Session Verified (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "user": {
                                            "uuid": "01a0b95f-1234-7890-abcd-ef0123456789",
                                            "email": "dr.sarah.smith@example.com",
                                            "display_name": "Dr. Sarah Smith",
                                            "role": "practitioner",
                                            "is_active": True
                                        },
                                        "organization": {
                                            "uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ee",
                                            "name": "Apex Health Systems",
                                            "code": "APEX_HEALTH"
                                        },
                                        "facilities": [
                                            {
                                                "uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                                                "name": "Apex Main Hospital",
                                                "code": "MAIN_01",
                                                "is_primary": True
                                            }
                                        ],
                                        "roles": ["practitioner"],
                                        "csrf_token": "sample-csrf-token-smith"
                                    },
                                    "meta": {"request_id": "req-inv-005"}
                                }, indent=2)
                            }
                        ]
                    },
                    {
                        "name": "06. [Invited User] Fetch Assigned Facilities",
                        "request": {
                            "method": "GET",
                            "header": [
                                {"key": "Accept", "value": "application/json", "type": "text"},
                                {"key": "Authorization", "value": "Bearer {{staff_access_token}}", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/facilities",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "facilities"]
                            },
                            "description": "Lists facilities assigned to the accepted staff member."
                        },
                        "response": [
                            {
                                "name": "Facilities List (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "items": [
                                            {
                                                "uuid": "01a0b95d-d330-7887-8462-7dc6a0190fb8",
                                                "name": "Apex Main Hospital",
                                                "code": "MAIN_01",
                                                "address": "123 Healthcare Ave, Suite 100",
                                                "timezone": "Asia/Kolkata",
                                                "is_active": True,
                                                "is_primary": True
                                            }
                                        ]
                                    },
                                    "meta": {"count": 1, "request_id": "req-inv-006"}
                                }, indent=2)
                            }
                        ]
                    }
                ]
            },
            {
                "name": "Phase 4: Admin Staff Management",
                "description": "Admin verifies the newly accepted staff in the roster, updates assignments, or deactivates.",
                "item": [
                    {
                        "name": "07. [Admin] Inspect Staff Roster",
                        "request": {
                            "method": "GET",
                            "header": [
                                {"key": "Accept", "value": "application/json", "type": "text"},
                                {"key": "Authorization", "value": "Bearer {{admin_access_token}}", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/admin/staff",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "admin", "staff"]
                            },
                            "description": "Admin lists all staff members in the organization."
                        },
                        "response": [
                            {
                                "name": "Staff Roster (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "items": [
                                            {
                                                "uuid": "f5eebc99-9c0b-4ef8-bb6d-6bb9bd380a66",
                                                "user_uuid": "01a0b95c-3fed-785a-95e0-48cc8d07f2ef",
                                                "display_name": "Dr. Nitin Shukla",
                                                "email": "chilledoutnick@gmail.com",
                                                "status": "active",
                                                "role_codes": ["doctor", "organization_admin"],
                                                "facility_uuids": ["01a0b95d-d330-7887-8462-7dc6a0190fb8"]
                                            },
                                            {
                                                "uuid": "f6eebc99-9c0b-4ef8-bb6d-6bb9bd380a77",
                                                "user_uuid": "01a0b95f-1234-7890-abcd-ef0123456789",
                                                "display_name": "Dr. Sarah Smith",
                                                "email": "dr.sarah.smith@example.com",
                                                "status": "active",
                                                "role_codes": ["practitioner"],
                                                "facility_uuids": ["01a0b95d-d330-7887-8462-7dc6a0190fb8"]
                                            }
                                        ]
                                    },
                                    "meta": {"count": 2, "request_id": "req-inv-007"}
                                }, indent=2)
                            }
                        ]
                    },
                    {
                        "name": "08. [Admin] Update Staff Facility Assignments",
                        "request": {
                            "method": "PUT",
                            "header": [
                                {"key": "Content-Type", "value": "application/json", "type": "text"},
                                {"key": "Accept", "value": "application/json", "type": "text"},
                                {"key": "Authorization", "value": "Bearer {{admin_access_token}}", "type": "text"},
                                {"key": "X-CSRF-Token", "value": "{{csrf_token}}", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/admin/staff/:staff_uuid/assignments",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "admin", "staff", ":staff_uuid", "assignments"],
                                "variable": [
                                    {"key": "staff_uuid", "value": "{{new_staff_uuid}}", "description": "Target Staff UUID"}
                                ]
                            },
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps({
                                    "assignments": ["{{facility_uuid}}"]
                                }, indent=2),
                                "options": {"raw": {"language": "json"}}
                            },
                            "description": "Admin replaces assigned facilities for the staff member."
                        },
                        "response": [
                            {
                                "name": "Assignments Updated (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "facility_uuids": ["01a0b95d-d330-7887-8462-7dc6a0190fb8"]
                                    },
                                    "meta": {"request_id": "req-inv-008"}
                                }, indent=2)
                            }
                        ]
                    },
                    {
                        "name": "09. [Admin] Deactivate Staff Member",
                        "request": {
                            "method": "POST",
                            "header": [
                                {"key": "Content-Type", "value": "application/json", "type": "text"},
                                {"key": "Accept", "value": "application/json", "type": "text"},
                                {"key": "Authorization", "value": "Bearer {{admin_access_token}}", "type": "text"},
                                {"key": "X-CSRF-Token", "value": "{{csrf_token}}", "type": "text"}
                            ],
                            "url": {
                                "raw": "{{base_url}}/api/v1/admin/staff/:staff_uuid/deactivate",
                                "host": ["{{base_url}}"],
                                "path": ["api", "v1", "admin", "staff", ":staff_uuid", "deactivate"],
                                "variable": [
                                    {"key": "staff_uuid", "value": "{{new_staff_uuid}}", "description": "Target Staff UUID"}
                                ]
                            },
                            "body": {
                                "mode": "raw",
                                "raw": "{}",
                                "options": {"raw": {"language": "json"}}
                            },
                            "description": "Admin deactivates staff member and all active facility assignments."
                        },
                        "response": [
                            {
                                "name": "Staff Deactivated (200 OK)",
                                "status": "OK",
                                "code": 200,
                                "_postman_previewlanguage": "json",
                                "header": [{"key": "Content-Type", "value": "application/json"}],
                                "body": json.dumps({
                                    "success": True,
                                    "data": {
                                        "uuid": "f6eebc99-9c0b-4ef8-bb6d-6bb9bd380a77",
                                        "status": "inactive"
                                    },
                                    "meta": {"request_id": "req-inv-009"}
                                }, indent=2)
                            }
                        ]
                    }
                ]
            }
        ]
    }
    return collection

def build_markdown_docs(openapi_spec):
    paths = openapi_spec.get("paths", {})
    all_endpoints = []
    
    for path, path_item in sorted(paths.items()):
        if path in ["/docs", "/redoc", "/openapi.json"]:
            continue
        for method_lower, op_data in path_item.items():
            if method_lower not in ["get", "post", "put", "patch", "delete"]:
                continue
            method = method_lower.upper()
            summary = op_data.get("summary") or op_data.get("operationId") or f"{method} {path}"
            description = op_data.get("description") or summary
            folder = get_folder_name(path, method)
            db_schema = DB_SCHEMA_MAP.get(folder, "`care` / `organization`")
            
            if "/auth/login" in path or "/auth/register" in path or "/auth/callback" in path or path in ["/api/v1/health", "/api/v1/ready"]:
                auth_req = "Public"
            elif "/invitations/accept" in path or "/invitations/validate" in path:
                auth_req = "Public / Token"
            else:
                auth_req = "BFF Cookie (`healthos_session`) + CSRF"

            all_endpoints.append({
                "method": method,
                "path": path,
                "summary": summary,
                "description": description,
                "domain": folder,
                "db_schema": db_schema,
                "auth": auth_req,
                "status": "✅ Active / Implemented",
                "operation_id": op_data.get("operationId", "")
            })

    md = []
    md.append("# HealthOS API - Complete Endpoints Status & Reference")
    md.append("")
    md.append("> **Architecture Baseline**: HealthOS Initial PostgreSQL Architecture 4.0 (Modular Monolith).")
    md.append("> **Standard Response Envelope**: Every endpoint returns `{\"success\": true, \"data\": {...}, \"meta\": {\"request_id\": \"...\"}}` or standard error envelope.")
    md.append("")
    md.append("## 📊 Summary Statistics")
    md.append("")
    md.append(f"- **Total API Endpoints / Operations**: {len(all_endpoints)}")
    md.append(f"- **Total Unique API Routes**: {len([p for p in paths if p not in ['/docs', '/redoc', '/openapi.json']])}")
    md.append("- **Architecture Schemas in Use**: `identity`, `organization`, `platform`, `care`, `governance`")
    md.append("- **Authentication Model**: Logto Backend-For-Frontend (BFF) Authorization-Code flow with secure HTTP-only cookies (`healthos_session`) and `X-CSRF-Token` protection.")
    md.append("- **Postman Collections Available**:")
    md.append("  1. 📦 [Complete API Collection (78 Endpoints)](HealthOS_All_APIs.postman_collection.json)")
    md.append("  2. 🔗 [Dedicated Staff Invitation & Onboarding Flow Collection](HealthOS_Staff_Invitation_Flow.postman_collection.json)")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 🔄 Dedicated Staff Invitation & Onboarding Flow")
    md.append("")
    md.append("The invitation and onboarding flow is isolated into a standalone Postman collection with automatic variable extraction:")
    md.append("")
    md.append("| Step | Actor | Action | Endpoint | Description |")
    md.append("|:---|:---|:---|:---|:---|")
    md.append("| 1 | Admin | Send Staff Invitation | `POST /api/v1/staff/invitations` | Generates 7-day token; extracts `{{invitation_token}}` |")
    md.append("| 2 | Admin | View Pending Invitations | `GET /api/v1/staff/invitations` | Lists all pending invitations |")
    md.append("| 3 | Invited Staff | Validate Token | `GET /api/v1/staff/invitations/validate?token=...` | Public validation with org/facility details |")
    md.append("| 4 | Invited Staff | Accept & Set Password | `POST /api/v1/staff/invitations/accept` | Provisions user; extracts `{{staff_access_token}}` |")
    md.append("| 5 | Invited Staff | Verify Profile | `GET /api/v1/auth/me` | Confirms permissions and assigned roles |")
    md.append("| 6 | Invited Staff | List Facilities | `GET /api/v1/facilities` | Lists accessible clinics |")
    md.append("| 7 | Admin | View Roster | `GET /api/v1/admin/staff` | Confirms staff member status is active |")
    md.append("| 8 | Admin | Update Assignments | `PUT /api/v1/admin/staff/:staff_uuid/assignments` | Assigns clinic facilities |")
    md.append("| 9 | Admin | Deactivate Staff | `POST /api/v1/admin/staff/:staff_uuid/deactivate` | Revokes staff access |")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 🗂️ Domain Overview & Status Matrix")
    md.append("")
    md.append("| Domain / Feature Area | Endpoints | Status | Primary Database Schemas | Auth Requirement |")
    md.append("|:---|:---:|:---:|:---|:---|")
    
    grouped = {}
    for ep in all_endpoints:
        d = ep["domain"]
        if d not in grouped:
            grouped[d] = []
        grouped[d].append(ep)

    ordered_grouped = {k: grouped[k] for k in sorted(grouped.keys())}

    for domain_name, eps in ordered_grouped.items():
        db_s = DB_SCHEMA_MAP.get(domain_name, "Varies")
        sample_auth = eps[0]["auth"] if len(set(e["auth"] for e in eps)) == 1 else "BFF Cookie / CSRF"
        md.append(f"| **{domain_name}** | {len(eps)} | ✅ Complete | {db_s} | {sample_auth} |")

    md.append("")
    md.append("---")
    md.append("")
    md.append("## 📑 Detailed Endpoint Reference by Domain")
    md.append("")

    for domain_name, eps in ordered_grouped.items():
        md.append(f"### {domain_name}")
        md.append("")
        md.append(f"**Database Schema Ownership**: {DB_SCHEMA_MAP.get(domain_name, '`care` / `organization`')}")
        md.append("")
        md.append("| Method | Endpoint | Description | Auth | Status |")
        md.append("|:---|:---|:---|:---|:---:|")
        for ep in eps:
            clean_summary = ep["summary"].replace("|", "\\|")
            md.append(f"| `{ep['method']}` | `{ep['path']}` | {clean_summary} | {ep['auth']} | {ep['status']} |")
        md.append("")

    md.append("---")
    md.append("")
    md.append("## 🔒 HealthOS Standard Envelope Contract")
    md.append("")
    md.append("### Success Envelope (`200 OK`, `201 Created`)")
    md.append("```json")
    md.append(json.dumps({
        "success": True,
        "data": {
            "example_resource_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
            "name": "Sample Object"
        },
        "meta": {
            "request_id": "req-0191-abcd-ef01"
        }
    }, indent=2))
    md.append("```")
    md.append("")
    md.append("### Error Envelope (`400`, `401`, `403`, `404`, `409`, `500`)")
    md.append("```json")
    md.append(json.dumps({
        "success": False,
        "error": {
            "code": "AUTHENTICATION_REQUIRED",
            "message": "A valid authenticated session is required to perform this action.",
            "details": []
        },
        "meta": {
            "request_id": "req-0191-err-0123"
        }
    }, indent=2))
    md.append("```")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 🚀 How to Import into Postman")
    md.append("")
    md.append("1. Open Postman -> **Import** (Top left).")
    md.append("2. Select file: `docs/HealthOS_All_APIs.postman_collection.json` (or `docs/HealthOS_Staff_Invitation_Flow.postman_collection.json`).")
    md.append("3. In the collection settings, configure the `base_url` variable (defaults to `http://localhost:8000`).")
    md.append("4. Call `POST /api/v1/auth/local/login` to authenticate and acquire the `healthos_session` cookie or set `auth_token`.")
    md.append("5. Set your `csrf_token` variable from `GET /api/v1/auth/me` to authorize state-changing requests.")
    md.append("")

    return "\n".join(md)

def main():
    print("Exporting OpenAPI specification from FastAPI application...")
    spec = app.openapi()
    
    print("Generating complete Postman collection v2.1.0...")
    all_collection = build_postman_collection(spec)
    with open(POSTMAN_ALL_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(all_collection, f, indent=2)
    print(f"Saved Complete Collection to: {POSTMAN_ALL_OUTPUT}")

    print("Generating dedicated Staff Invitation Flow Postman collection v2.1.0...")
    invite_collection = build_staff_invitation_flow_collection()
    with open(POSTMAN_INVITE_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(invite_collection, f, indent=2)
    print(f"Saved Invitation Flow Collection to: {POSTMAN_INVITE_OUTPUT}")

    print(f"Generating markdown documentation: {MARKDOWN_OUTPUT}...")
    markdown_content = build_markdown_docs(spec)
    with open(MARKDOWN_OUTPUT, "w", encoding="utf-8") as f:
        f.write(markdown_content)
    print(f"Saved API status doc to: {MARKDOWN_OUTPUT}")
    print("Done!")

if __name__ == "__main__":
    main()
