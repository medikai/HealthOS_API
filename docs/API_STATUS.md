# HealthOS API - Complete Endpoints Status & Reference

> **Architecture Baseline**: HealthOS Initial PostgreSQL Architecture 4.0 (Modular Monolith).
> **Standard Response Envelope**: Every endpoint returns `{"success": true, "data": {...}, "meta": {"request_id": "..."}}` or standard error envelope.

## 📊 Summary Statistics

- **Total API Endpoints / Operations**: 102
- **Total Unique API Routes**: 81
- **Architecture Schemas in Use**: `identity`, `organization`, `platform`, `care`, `governance`
- **Authentication Model**: Logto Backend-For-Frontend (BFF) Authorization-Code flow with secure HTTP-only cookies (`healthos_session`) and `X-CSRF-Token` protection.
- **Postman Collections Available**:
  1. 📦 [Complete API Collection (78 Endpoints)](HealthOS_All_APIs.postman_collection.json)
  2. 🔗 [Dedicated Staff Invitation & Onboarding Flow Collection](HealthOS_Staff_Invitation_Flow.postman_collection.json)

---

## 🔄 Dedicated Staff Invitation & Onboarding Flow

The invitation and onboarding flow is isolated into a standalone Postman collection with automatic variable extraction:

| Step | Actor | Action | Endpoint | Description |
|:---|:---|:---|:---|:---|
| 1 | Admin | Send Staff Invitation | `POST /api/v1/staff/invitations` | Generates 7-day token; extracts `{{invitation_token}}` |
| 2 | Admin | View Pending Invitations | `GET /api/v1/staff/invitations` | Lists all pending invitations |
| 3 | Invited Staff | Validate Token | `GET /api/v1/staff/invitations/validate?token=...` | Public validation with org/facility details |
| 4 | Invited Staff | Accept & Set Password | `POST /api/v1/staff/invitations/accept` | Provisions user; extracts `{{staff_access_token}}` |
| 5 | Invited Staff | Verify Profile | `GET /api/v1/auth/me` | Confirms permissions and assigned roles |
| 6 | Invited Staff | List Facilities | `GET /api/v1/facilities` | Lists accessible clinics |
| 7 | Admin | View Roster | `GET /api/v1/admin/staff` | Confirms staff member status is active |
| 8 | Admin | Update Assignments | `PUT /api/v1/admin/staff/:staff_uuid/assignments` | Assigns clinic facilities |
| 9 | Admin | Deactivate Staff | `POST /api/v1/admin/staff/:staff_uuid/deactivate` | Revokes staff access |

---

## 🗂️ Domain Overview & Status Matrix

| Domain / Feature Area | Endpoints | Status | Primary Database Schemas | Auth Requirement |
|:---|:---:|:---:|:---|:---|
| **01. Health & System** | 2 | ✅ Complete | N/A (System / Cache / DB ping) | Public |
| **02. Authentication & Identity** | 4 | ✅ Complete | `identity.user_account`, `identity.person`, Logto IdP | BFF Cookie (`healthos_session`) + CSRF |
| **03. Organizations & Multi-Tenancy** | 5 | ✅ Complete | `organization.organization`, `organization.facility`, `organization.department` | BFF Cookie (`healthos_session`) + CSRF |
| **04. Bootstrap & Context** | 3 | ✅ Complete | `identity`, `organization`, `platform` | BFF Cookie (`healthos_session`) + CSRF |
| **05. Patients** | 7 | ✅ Complete | `identity.patient`, `identity.person`, `care.encounter` | BFF Cookie (`healthos_session`) + CSRF |
| **06. Scheduling & Appointments** | 12 | ✅ Complete | `care.appointment`, `care.availability_rule`, `organization.practitioner` | BFF Cookie (`healthos_session`) + CSRF |
| **07. Queue & Walk-ins** | 7 | ✅ Complete | `care.queue_entry`, `care.appointment`, `identity.patient` | BFF Cookie (`healthos_session`) + CSRF |
| **08. Encounters & Clinical Notes (SOAP)** | 18 | ✅ Complete | `care.encounter`, `care.clinical_note`, `care.diagnosis`, `care.vital`, `care.prescription` | BFF Cookie (`healthos_session`) + CSRF |
| **09. Clinical Documentation Settings** | 6 | ✅ Complete | `care.clinical_documentation_setting` | BFF Cookie (`healthos_session`) + CSRF |
| **10. Dashboard & Analytics** | 6 | ✅ Complete | `care`, `organization`, `identity` (Read aggregations) | BFF Cookie (`healthos_session`) + CSRF |
| **11. Staff Administration & Invitations** | 17 | ✅ Complete | `organization.staff_member`, `organization.staff_assignment`, `organization.invitation`, `identity.user_account` | BFF Cookie / CSRF |
| **12. Facility Scheduling & Protected Periods** | 5 | ✅ Complete | `care.facility_schedule`, `care.protected_period` | BFF Cookie (`healthos_session`) + CSRF |
| **13. Events & Audit Logs** | 2 | ✅ Complete | `governance.audit_log`, In-Memory / Redis Event Bus | BFF Cookie (`healthos_session`) + CSRF |
| **14. Frontend Compatibility** | 8 | ✅ Complete | `care.appointment`, `care.availability_rule` | BFF Cookie (`healthos_session`) + CSRF |

---

## 📑 Detailed Endpoint Reference by Domain

### 01. Health & System

**Database Schema Ownership**: N/A (System / Cache / DB ping)

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/health` | Health | Public | ✅ Active / Implemented |
| `GET` | `/api/v1/ready` | Ready | Public | ✅ Active / Implemented |

### 02. Authentication & Identity

**Database Schema Ownership**: `identity.user_account`, `identity.person`, Logto IdP

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `POST` | `/api/v1/auth/local/login` | Local Login | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/auth/local/register` | Local Register | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/auth/logout` | Logout | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/auth/me` | Me | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 03. Organizations & Multi-Tenancy

**Database Schema Ownership**: `organization.organization`, `organization.facility`, `organization.department`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `POST` | `/api/v1/organizations` | Create Organization | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/organizations/{organization_id}/departments` | Create Department | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/organizations/{organization_id}/facilities` | Create Facility | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/organizations/{organization_id}/features` | Add Organization Feature | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/organizations/{organization_id}/members/roles` | Assign Member Role | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 04. Bootstrap & Context

**Database Schema Ownership**: `identity`, `organization`, `platform`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/facilities` | Facilities | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/me` | Me | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/platform/context` | Platform Context | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 05. Patients

**Database Schema Ownership**: `identity.patient`, `identity.person`, `care.encounter`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `POST` | `/api/v1/patients` | Create Patient | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/patients/duplicates` | Duplicate Patients | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/patients/search` | Search Patients | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/patients/{patient_uuid}` | Get Patient | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PATCH` | `/api/v1/patients/{patient_uuid}` | Update Patient | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/patients/{patient_uuid}/context` | Patient Context | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/patients/{patient_uuid}/timeline` | Patient Timeline | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 06. Scheduling & Appointments

**Database Schema Ownership**: `care.appointment`, `care.availability_rule`, `organization.practitioner`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `POST` | `/api/v1/appointments` | Create Appointment | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/appointments` | List Appointments | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/appointments/{appointment_uuid}` | Get Appointment | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/appointments/{appointment_uuid}/cancel` | Cancel Appointment | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/appointments/{appointment_uuid}/check-in` | Check In | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/appointments/{appointment_uuid}/no-show` | No Show Appointment | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/appointments/{appointment_uuid}/reschedule` | Reschedule Appointment | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/appointments/{appointment_uuid}/start-consultation` | Start Appointment Consultation | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/practitioners` | Practitioners | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/practitioners/{practitioner_uuid}` | Practitioner Detail | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/scheduling/calendar` | Calendar | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/scheduling/next-slots` | Next Slots | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 07. Queue & Walk-ins

**Database Schema Ownership**: `care.queue_entry`, `care.appointment`, `identity.patient`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/queue` | Queue List | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/queue/{queue_entry_uuid}` | Get Queue Entry | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/queue/{queue_entry_uuid}/call` | Call Queue Entry | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/queue/{queue_entry_uuid}/cancel` | Cancel Queue Entry | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/queue/{queue_entry_uuid}/skip` | Skip Queue Entry | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/queue/{queue_entry_uuid}/start-consultation` | Start Queue Consultation | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/walk-ins` | Create Walk In | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 08. Encounters & Clinical Notes (SOAP)

**Database Schema Ownership**: `care.encounter`, `care.clinical_note`, `care.diagnosis`, `care.vital`, `care.prescription`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/encounters/{encounter_uuid}` | Get Encounter | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/encounters/{encounter_uuid}/complete` | Complete Encounter | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/encounters/{encounter_uuid}/diagnoses` | Get Diagnoses | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/encounters/{encounter_uuid}/diagnoses` | Replace Diagnoses | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/encounters/{encounter_uuid}/prescriptions` | Create Prescription | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/encounters/{encounter_uuid}/prescriptions` | Create Prescription | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/encounters/{encounter_uuid}/prescriptions` | Get Encounter Prescription | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/encounters/{encounter_uuid}/soap` | Get Soap | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/encounters/{encounter_uuid}/soap` | Upsert Soap | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/encounters/{encounter_uuid}/soap` | Upsert Soap | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/encounters/{encounter_uuid}/soap/sign` | Sign Soap | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/encounters/{encounter_uuid}/vitals` | Get Vitals | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/encounters/{encounter_uuid}/vitals` | Create Vital | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PATCH` | `/api/v1/encounters/{encounter_uuid}/vitals/{vital_uuid}` | Update Vital | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/prescriptions/{prescription_uuid}` | Get Prescription | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/prescriptions/{prescription_uuid}` | Update Prescription | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/prescriptions/{prescription_uuid}/document` | Prescription Document | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/prescriptions/{prescription_uuid}/sign` | Sign Prescription | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 09. Clinical Documentation Settings

**Database Schema Ownership**: `care.clinical_documentation_setting`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/clinical/documentation-settings` | Get Clinical Documentation Settings | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/clinical/documentation-settings` | Update Clinical Documentation Settings | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/clinical/documentation-settings/reset` | Reset Clinical Documentation Settings | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/clinical/settings` | Get Clinical Documentation Settings | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/clinical/settings` | Update Clinical Documentation Settings | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/clinical/settings/reset` | Reset Clinical Documentation Settings | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 10. Dashboard & Analytics

**Database Schema Ownership**: `care`, `organization`, `identity` (Read aggregations)

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/dashboard/today` | Today Dashboard | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/masters/access-roles` | Access Roles | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/masters/specialties` | Specialties | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/masters/staff-designations` | Staff Designations | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/masters/visit-reasons` | Visit Reasons | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/stats` | Facility Stats | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 11. Staff Administration & Invitations

**Database Schema Ownership**: `organization.staff_member`, `organization.staff_assignment`, `organization.invitation`, `identity.user_account`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/admin/staff` | List Staff | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/admin/staff` | Create Staff | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/admin/staff/invitations` | List Invitations | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/admin/staff/invitations` | Create Invitation | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/admin/staff/invitations/accept` | Accept Invitation | Public / Token | ✅ Active / Implemented |
| `GET` | `/api/v1/admin/staff/invitations/validate` | Validate Invitation | Public / Token | ✅ Active / Implemented |
| `GET` | `/api/v1/admin/staff/{staff_uuid}` | Get Staff | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PATCH` | `/api/v1/admin/staff/{staff_uuid}` | Update Staff | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/admin/staff/{staff_uuid}/assignments` | Get Assignments | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/admin/staff/{staff_uuid}/assignments` | Replace Assignments | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/admin/staff/{staff_uuid}/deactivate` | Deactivate Staff | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/admin/staff/{staff_uuid}/roles` | Get Roles | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/admin/staff/{staff_uuid}/roles` | Replace Roles | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/staff/invitations` | List Invitations | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/staff/invitations` | Create Invitation | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/staff/invitations/accept` | Accept Invitation | Public / Token | ✅ Active / Implemented |
| `GET` | `/api/v1/staff/invitations/validate` | Validate Invitation | Public / Token | ✅ Active / Implemented |

### 12. Facility Scheduling & Protected Periods

**Database Schema Ownership**: `care.facility_schedule`, `care.protected_period`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/facilities/{facility_uuid}/protected-periods` | List Periods | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/facilities/{facility_uuid}/protected-periods` | Create Period | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `DELETE` | `/api/v1/facilities/{facility_uuid}/protected-periods/{period_id}` | Delete Period | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/facilities/{facility_uuid}/schedule` | Get Schedule | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/facilities/{facility_uuid}/schedule` | Update Schedule | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 13. Events & Audit Logs

**Database Schema Ownership**: `governance.audit_log`, In-Memory / Redis Event Bus

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/admin/audit-logs` | Audit Logs | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/events/stream` | Event Stream | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

### 14. Frontend Compatibility

**Database Schema Ownership**: `care.appointment`, `care.availability_rule`

| Method | Endpoint | Description | Auth | Status |
|:---|:---|:---|:---|:---:|
| `GET` | `/api/v1/api/appointments/availability` | Availability | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/appointments/availability` | Availability | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/scheduling/availability` | Availability | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/scheduling/availability-exceptions` | Availability Exceptions | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `POST` | `/api/v1/scheduling/availability-exceptions` | Create Availability Exception | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `DELETE` | `/api/v1/scheduling/availability-exceptions/{exception_uuid}` | Delete Availability Exception | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `GET` | `/api/v1/scheduling/availability-rules` | Availability Rules | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |
| `PUT` | `/api/v1/scheduling/availability-rules` | Replace Availability Rule | BFF Cookie (`healthos_session`) + CSRF | ✅ Active / Implemented |

---

## 🔒 HealthOS Standard Envelope Contract

### Success Envelope (`200 OK`, `201 Created`)
```json
{
  "success": true,
  "data": {
    "example_resource_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    "name": "Sample Object"
  },
  "meta": {
    "request_id": "req-0191-abcd-ef01"
  }
}
```

### Error Envelope (`400`, `401`, `403`, `404`, `409`, `500`)
```json
{
  "success": false,
  "error": {
    "code": "AUTHENTICATION_REQUIRED",
    "message": "A valid authenticated session is required to perform this action.",
    "details": []
  },
  "meta": {
    "request_id": "req-0191-err-0123"
  }
}
```

---

## 🚀 How to Import into Postman

1. Open Postman -> **Import** (Top left).
2. Select file: `docs/HealthOS_All_APIs.postman_collection.json` (or `docs/HealthOS_Staff_Invitation_Flow.postman_collection.json`).
3. In the collection settings, configure the `base_url` variable (defaults to `http://localhost:8000`).
4. Call `POST /api/v1/auth/local/login` to authenticate and acquire the `healthos_session` cookie or set `auth_token`.
5. Set your `csrf_token` variable from `GET /api/v1/auth/me` to authorize state-changing requests.
