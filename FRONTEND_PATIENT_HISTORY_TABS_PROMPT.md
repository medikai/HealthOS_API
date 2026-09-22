# HealthOS Patient History, Encounters, SOAP & Prescriptions — Frontend Implementation Prompt

Implement chronological visit ordering, formatted timestamps, and the two missing patient detail tabs (`Encounters & SOAP` and `Prescriptions`) in `healthos-frontend`. Read `docs/healthos-agent-rules.md` first. Reuse existing components, table/card styles, and badge systems; do not introduce external libraries.

---

## 1. Problem Summary & Root Causes Discovered

1. **Ordering of Visits**:
   - `GET /api/v1/appointments?patient_uuid=...` previously ordered chronologically ascending (`scheduled_start ASC`), showing older past consultations at the top instead of the active or latest consultation.
   - Backend now supports `?order=desc` (`GET /api/v1/appointments?patient_uuid=<uuid>&order=desc`).
   - The frontend should sort appointments with **Active / In Consultation / Checked-In visits pinned to the top**, followed by descending `scheduled_start` date.

2. **Missing Dates / Times on Visit Cards**:
   - The appointments API response already provides `"scheduled_start"` and `"scheduled_end"` in ISO-8601 UTC format.
   - The Visit card rendered status labels ("Scheduled", "Encounter Concluded") but omitted the date and time strings.
   - Frontend must format and display clinic-localized date and time (e.g. `22 Sep 2026 • 02:00 PM - 02:30 PM`).

3. **Empty Tabs (`Encounters & SOAP` and `Prescriptions`)**:
   - Previously, all SOAP and prescription endpoints were strictly scoped by single `encounter_uuid` (`/encounters/:uuid/soap`, `/encounters/:uuid/prescriptions`). There was no patient-level historical aggregation endpoint.
   - **Backend now provides dedicated patient-level endpoints**:
     - `GET /api/v1/patients/{patient_uuid}/encounters`
     - `GET /api/v1/patients/{patient_uuid}/prescriptions`

---

## 2. Backend API Contracts

All endpoints return standard envelopes: `{"success": true, "data": {"items": [...]}, "meta": {"count": N}}`.

### A. Patient Encounters & Historical SOAP
`GET /api/v1/patients/{patient_uuid}/encounters?limit=50`

Each item in `data.items` contains:
```ts
interface PatientEncounterItem {
  uuid: string;
  encounter_uuid: string;
  appointment_uuid: string | null;
  queue_entry_uuid: string | null;
  status: 'in_progress' | 'completed' | string;
  started_at: string; // ISO string
  completed_at: string | null;
  facility: {
    uuid: string;
    name: string;
  } | null;
  practitioner: {
    uuid: string;
    name: string;
    specialty: string | null;
  } | null;
  soap: {
    uuid: string;
    status: 'draft' | 'signed';
    subjective: string;
    objective: string;
    assessment: string;
    plan: string;
    custom_fields: Record<string, any>;
    signed_at: string | null;
  } | null;
  diagnoses: Array<{
    uuid: string;
    code: string;
    description: string;
    is_primary: boolean;
  }>;
  vitals: Array<{
    uuid: string;
    name: string;
    value: string;
    unit: string | null;
    recorded_at: string | null;
  }>;
  prescription: {
    uuid: string;
    status: 'draft' | 'signed';
    advice: string | null;
    signed_at: string | null;
    items: Array<MedicationItem>;
    medications: Array<MedicationItem>;
  } | null;
}
```

### B. Patient Prescriptions History
`GET /api/v1/patients/{patient_uuid}/prescriptions?limit=50`

Each item in `data.items` contains:
```ts
interface PatientPrescriptionItem {
  uuid: string;
  prescription_uuid: string;
  encounter_uuid: string;
  appointment_uuid: string | null;
  status: 'draft' | 'signed';
  advice: string | null;
  signed_at: string | null;
  encounter_started_at: string;
  facility: {
    uuid: string;
    name: string;
  } | null;
  practitioner: {
    uuid: string;
    name: string;
    specialty: string | null;
  } | null;
  items: Array<{
    uuid: string;
    medicine_id: string | null;
    medicine_name: string;
    name: string;
    dosage: string;
    dose: string;
    frequency: string;
    duration: string;
    strength: string | null;
    brand: string | null;
    route: string | null;
    timing: string | null;
    instructions: string | null;
  }>;
  medications: Array<any>;
}
```

### C. Appointments Query
`GET /api/v1/appointments?patient_uuid={patient_uuid}&order=desc`
- Returns appointments ordered descending by `scheduled_start`.

---

## 3. Frontend Implementation Steps

### Step 1: Update API Client (`src/lib/api.ts` or corresponding API service)
Add client helper functions:
```ts
export const getPatientEncounters = async (patientUuid: string) => {
  const res = await api(`/patients/${encodeURIComponent(patientUuid)}/encounters`);
  return res.data?.items ?? [];
};

export const getPatientPrescriptions = async (patientUuid: string) => {
  const res = await api(`/patients/${encodeURIComponent(patientUuid)}/prescriptions`);
  return res.data?.items ?? [];
};
```
Update appointment query calls for the patient page to include `&order=desc`.

### Step 2: Tab Badges & Counts
In the Patient profile navigation header:
- `All Visits (${visits.length})`
- `Encounters & SOAP (${encounters.length})`
- `Prescriptions (${prescriptions.length})`
- `Documents (${documents.length})`

### Step 3: Tab 1 — "All Visits" Card Enhancements
1. **Sort Order**:
   - Active consultation (`status === 'in_consultation'` or `checked_in`) pinned on top.
   - Remaining visits ordered by `scheduled_start` descending (latest date first).
2. **Date & Time Display**:
   - Render formatted visit date and time cleanly in the card header/meta area:
     e.g., `Today, 22 Sep 2026 • 02:00 PM - 02:30 PM` or `21 Sep 2026 • 12:00 PM - 12:30 PM`.
   - Use the clinic/user timezone (e.g., `Asia/Kolkata`) or `Intl.DateTimeFormat`.
3. **Action Button & Encounter Linking**:
   - For `in_consultation`: "Open Clinical SOAP Note (Active)" navigating to `/encounters/${encounter_uuid}`.
   - For `completed`: "Print Prescription" or "View Encounter Summary".

### Step 4: Tab 2 — "Encounters & SOAP" Implementation
Render an encounter timeline ordered newest to oldest:
1. **Header**: Date & Time, Doctor name, Specialty, Clinic facility, Status badge (`Active` or `Signed / Concluded`).
2. **Chief Complaint & Clinical Notes**:
   - Display `soap.subjective` and chief complaints.
   - If `soap.custom_fields` has section notes (e.g. `chief_complaints`, `hpi`), display them.
3. **Clinical Findings & Examination (Objective)**:
   - Display `soap.objective`.
   - Vitals pill bar: Blood Pressure, Pulse, SpO2, Temp recorded during that encounter.
4. **Assessment & Diagnoses**:
   - Primary & secondary diagnoses badges (`code` + `description`), e.g., `[J06.9] Acute upper respiratory infection (Primary)`.
   - `soap.assessment` text.
5. **Plan**:
   - `soap.plan` treatment instructions.
   - If prescription exists for this encounter, render a quick summary box with prescribed medications.
6. **Actions**:
   - If signed: "View Signed SOAP", "Print Record".
   - If active/in-progress: "Resume Consultation" / "Open Clinical Note".

### Step 5: Tab 3 — "Prescriptions" Implementation
Render prescription cards grouped by encounter date/time (newest first):
1. **Card Header**:
   - Prescribing Doctor (`Dr. Nitin Kumar • General Practice`)
   - Clinic Facility & Date (`21 Sep 2026, 12:25 PM`)
   - Status badge (`Signed` / `Draft`)
   - "Print Prescription" button (triggers printable prescription view or document download `/prescriptions/${uuid}/document`).
2. **Medications Table / List**:
   - Columns: Medication Name & Brand, Dosage, Frequency, Duration, Route, Instructions.
   - Example row: `Paracetamol 500mg (Calpol)` • `1 tab` • `Twice daily (BD)` • `5 days` • `Oral • After meals`.
3. **Clinical Advice**:
   - Render `advice` text block (e.g., "Take after meals. Drink plenty of water and rest.").

---

## 4. Verification & Acceptance Criteria
- [ ] Active / Live visit is displayed at the top of the visits list.
- [ ] Each visit card displays clear date and time range.
- [ ] Switching to `Encounters & SOAP` displays findings (Subjective, Objective, Assessment, Plan, Diagnoses, Vitals) for each encounter.
- [ ] Switching to `Prescriptions` displays all prescribed medication rows, dosage, instructions, prescriber, and date.
- [ ] Tab header badges show correct counts for each category.
