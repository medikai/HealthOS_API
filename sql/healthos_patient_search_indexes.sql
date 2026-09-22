-- ============================================================================
-- HealthOS Patient Search & Keyset Browsing Indexes
-- Target Database: PostgreSQL 16+ / 18+
-- Target Schemas: identity, public
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Extension Prerequisite
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ----------------------------------------------------------------------------
-- 2. Production Execution Note (Concurrent Creation)
-- ----------------------------------------------------------------------------
-- For high-traffic or large live tables:
-- Execute the following CREATE INDEX CONCURRENTLY statements individually OUTSIDE
-- of any transaction block (Autocommit mode in DataGrip / psql).
--
-- If a concurrent build fails or is interrupted:
-- Inspect the invalid index using the diagnostic query below, drop the invalid index,
-- and re-run.

-- ----------------------------------------------------------------------------
-- 3. Justified Query-Matched Indexes
-- ----------------------------------------------------------------------------

-- A. Trigram GIN Index on Normalized Full-Name Expression
--    Matches: ilike('%q%'), ilike('q%'), lower(first_name || ' ' || coalesce(last_name, '')) = lower_q
--    Expression uses immutable operators: lower(), ||, coalesce()
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_identity_person_trgm_name
ON identity.person
USING gin (lower(first_name || ' ' || coalesce(last_name, '')) gin_trgm_ops);

-- B. Tenant-Leading Deterministic Ordering / Keyset Browsing Index
--    Matches: WHERE organization_id = :org_id
--             ORDER BY lower(first_name) ASC, lower(coalesce(last_name, '')) ASC, id ASC
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_identity_person_org_lower_names
ON identity.person (organization_id, lower(first_name), lower(coalesce(last_name, '')), id);

-- ----------------------------------------------------------------------------
-- 4. Existing Indexes Reused (Do NOT duplicate or drop)
-- ----------------------------------------------------------------------------
-- - identity.person (organization_id, phone) -> uq_identity_person_org_phone
-- - identity.patient (organization_id, mrn)   -> uq_identity_patient_org_mrn
-- - identity.patient (organization_id, person_id) -> uq_identity_patient_org_person
-- - identity.patient (organization_id)       -> ix_identity_patient_organization_id
-- - identity.person (organization_id)        -> ix_identity_person_organization_id

-- ----------------------------------------------------------------------------
-- 5. Diagnostic / Health Check Query (Inspect Invalid Indexes)
-- ----------------------------------------------------------------------------
SELECT 
    n.nspname AS schema_name,
    t.relname AS table_name,
    c.relname AS index_name,
    i.indisvalid AS is_valid,
    i.indisready AS is_ready,
    pg_size_pretty(pg_relation_size(c.oid)) AS index_size,
    pg_get_indexdef(c.oid) AS index_definition
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'identity' 
  AND c.relname IN ('ix_identity_person_trgm_name', 'ix_identity_person_org_lower_names')
ORDER BY c.relname;

-- ----------------------------------------------------------------------------
-- 6. Rollback (Drop only newly created indexes)
-- ----------------------------------------------------------------------------
-- DROP INDEX CONCURRENTLY IF EXISTS identity.ix_identity_person_trgm_name;
-- DROP INDEX CONCURRENTLY IF EXISTS identity.ix_identity_person_org_lower_names;
