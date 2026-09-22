-- HealthOS Migration: Practitioner Schedule Storage and Optimistic Concurrency Control
-- Target Schema: care.practitioner_schedule

CREATE TABLE IF NOT EXISTS care.practitioner_schedule (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organization.organization(id),
    facility_id UUID NOT NULL REFERENCES organization.facility(id),
    practitioner_id UUID NOT NULL REFERENCES identity.practitioner(id),
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    slot_interval_minutes INTEGER NOT NULL DEFAULT 30,
    effective_from DATE NOT NULL,
    effective_to DATE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,
    weekly_hours JSONB NOT NULL DEFAULT '[]'::jsonb,
    date_exceptions JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_override BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ,
    updated_by_user_id UUID REFERENCES identity.user_account(id)
);

CREATE INDEX IF NOT EXISTS ix_care_practitioner_schedule_facility_practitioner_active
ON care.practitioner_schedule (facility_id, practitioner_id, is_active);

CREATE INDEX IF NOT EXISTS ix_care_practitioner_schedule_org_practitioner
ON care.practitioner_schedule (organization_id, practitioner_id);
