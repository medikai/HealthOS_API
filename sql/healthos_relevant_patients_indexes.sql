-- ============================================================================
-- HealthOS: Relevant Patients Optimization Indexes
-- Target Database: PostgreSQL 16+
-- Schema: care, identity
-- Idempotent, safe migration script
-- ============================================================================

-- 1. Tier 3 (Recently Completed Visits): Facility-scoped completed encounters
CREATE INDEX IF NOT EXISTS ix_care_encounter_facility_status_completed
ON care.encounter (facility_id, status, completed_at DESC);

-- 2. Tier 3 (Recently Completed Visits): Organization-scoped completed encounters
CREATE INDEX IF NOT EXISTS ix_care_encounter_org_status_completed
ON care.encounter (organization_id, status, completed_at DESC);

-- 3. Tier 4 (Recently Registered Active Patients): Org-scoped active patients
CREATE INDEX IF NOT EXISTS ix_identity_patient_org_active_created
ON identity.patient (organization_id, is_active, created_at DESC);

-- 4. Tier 1 & 2 (Appointments): Org-scoped non-cancelled / booked appointments
CREATE INDEX IF NOT EXISTS ix_care_appointment_org_status_start
ON care.appointment (organization_id, status, scheduled_start);

-- Verification Query:
-- SELECT schemaname, tablename, indexname FROM pg_indexes 
-- WHERE indexname IN (
--   'ix_care_encounter_facility_status_completed',
--   'ix_care_encounter_org_status_completed',
--   'ix_identity_patient_org_active_created',
--   'ix_care_appointment_org_status_start'
-- );
