# HealthOS Upgrade Progress

## Task: India geography ETL (2026-09-23)
- Implemented staged LGD source discovery/download, normalization, reviewed database plans, transactional manual apply, and read-only verification in `src/scripts/import_locations.py`; operator commands and mapping rules in `docs/india-geography-etl.md`.
- Source snapshots must include states, districts, urban bodies, and coverage on one date. Existing India state/city UUIDs and user-added flags are preserved. District-only apply can proceed independently of city conflicts. No migration or application endpoint change was needed.
- Existing dependencies are `platform.country/state/district/city`, `organization.facility` location FKs, and consumers of `/api/v1/masters/states|districts|cities`. Districtless cities remain unresolved unless uniquely matched; no historical values are invented.
- API contract remains `GET /api/v1/masters/districts?state_id=<UUID>` and `GET /api/v1/masters/cities?state_id=<UUID>&district_id=<UUID>&limit=1..100`; success is `{success:true,data:{items:[...]},meta:{count:N}}`, invalid UUID/limit returns 422. No endpoint cache invalidation is required (`Cache-Control: no-store`).
- Public preparation completed from the 2026-09-22 release listing: 36 states, 784 districts, 5,050 urban bodies, 35,015 coverage rows; staged 5,076 district-scoped city records. Four source archives and reports are in gitignored `data/lgd/`. The source reports 62 rejected coverage rows and no bodies without a mapped district. No shared database was written; manual import pending.
- The downloader now uses one release CSV listing request and downloads the four selected assets concurrently, avoiding the GitHub API rate limit.
- Read-only plans against configured `neondb`: 36 existing India states, 0 districts, 419 cities. The reviewed state-38 name alias in `src/scripts/india_geography_overrides.json` gives a district-only plan of 784 inserts and 0 conflicts. Full plan proposes 784 districts, 4,657 cities, and 415 existing-city district updates, but is blocked by four same-district/source-identity city collisions; three ambiguous existing city names are left districtless. No apply was run.
- The planner now accepts explicit `city_source_aliases` to resolve same-district/name source identities without silently dropping provenance; source and canonical identities must match state, district and normalized name. No such aliases have been chosen for the four live conflicts.
- Added explicit `--disambiguate-cities` plan/apply/verify mode for the four distinct same-district LGD identities: it keeps both city rows and appends their LGD codes to display names, making the full city import possible without choosing a canonical identity. The default mode still blocks those conflicts.
- Read-only full plan with `--disambiguate-cities` passed against `neondb`: 0 conflicts, 784 district inserts, 4,661 city inserts, 415 existing-city district updates, and 8 disambiguated names. Four prior Maharashtra city rows remain districtless. Aggregate checks found 0 facility rows linked to a city, so no dependent facility district backfill is required. Apply remains manual and has not run.
- Follow-up: added flushed stderr progress to all ETL modes, with download byte counts and reconciliation/import milestones; stdout JSON summaries remain unchanged. Focused tests and Ruff passed.
- Next: run prepare and plan locally with GitHub access, review any mapping conflicts and overrides, then manually apply and verify using the documented commands.

## Task: Business reporting Prompt 3 — practitioner earnings and recorded payouts (2026-09-23)
- Added immutable effective-dated basis-point policies, source-linked signed earning entries, recorded external payouts/reversals and settlement allocations in `src/app/models/billing.py`, `src/app/api/v1/earnings.py`, and migration `20260923_34`; routed billing payment/refund/void events through earning generation.
- Only completed single-practitioner consultation invoices whose amount/currency match their fee snapshot and whose receipt provenance is `recorded` can generate payable earnings. Historical payments remain `unknown` except exact known demo seed keys, which become `synthetic_demo`; unknown/synthetic receipts are excluded and reported. No guessed historical earnings or payout backfill was run.
- Admin policy, statement, summary, catch-up, payout and reversal APIs plus self-scoped earnings are documented with paths, bodies, fields, permissions, errors and examples in `docs/healthos-business-reporting-contract.md`. Manual salary, tiers, tax/payroll and adjustments are unsupported; ordinary collections and patient payment calculations are unchanged.
- Payouts serialize on the practitioner row, recheck balance at current time and paid-at, allocate eligible earnings, reject overpayment, and use organization-scoped idempotency keys. Reversals append audited negative payout entries; refunds append negative earning entries and can make carry-forward negative.
- Focused checkpoint: `./venv/bin/python -m pytest -q tests/test_practitioner_earnings.py tests/test_billing.py tests/test_business_reporting_reads.py tests/test_seed_billing_demo.py tests/test_reports.py` passed (18 tests), covering partial receipt/refund rounding, policy/source entry snapshots, duplicate payout/reversal behavior and lock SQL, self tampering and receptionist denial. Ruff and `git diff --check` passed. No authenticated live account or concurrent database transaction was available to exercise these flows end to end.
- Migration `20260923_34` is pending; no live/shared database was changed. Isolated offline SQL for `20260923_33:20260923_34` rendered. A full-chain offline render stops in unrelated earlier data migration `20260920_18` because that migration expects database query results. Apply only to an explicitly selected authorized local/dev database from `src/` with `../venv/bin/alembic upgrade head`; verify `SELECT provenance, count(*) FROM care.payment GROUP BY provenance` before/after and zero new ledger rows. Old unknown payment history requires a trusted provenance source before any future eligibility backfill.
- Stop at Prompt 3. Copy the updated contract to the frontend repository before Prompt 4.

## Task: Business reporting Prompt 1 — read contracts (2026-09-23)
- Added `/api/v1/reports/overview`, `/reports/practitioners`, and self-scoped `/reports/my-practice` and `/reports/my-visits`; kept visits/weekly parameters and their administrator gate, with facility assignment matching. Selected totals, trend, comparable cutoff, and practitioner names use server queries.
- Added `/api/v1/billing/invoices` list/detail, `/billing/transactions`, and `/billing/outstanding`. Invoice and transaction pages have allowlisted filters/sorts and full-filter totals where supplied; current outstanding uses the payment/refund ledger across all invoice dates.
- `docs/healthos-business-reporting-contract.md` gives exact request/response/error/permission shapes and short examples. Missing due dates, invoice numbers, payment methods, historical balance snapshots, and compensation remain explicitly unavailable.
- Known synthetic demo payments are labeled from their stored seed idempotency key; other provenance remains unknown. No data migration or backfill is needed for this read-only change, and no database was changed.
- Focused checkpoint: `./venv/bin/python -m pytest -q tests/test_business_reporting_reads.py tests/test_reports.py tests/test_billing.py` passed (13 tests); `ruff` and `git diff --check` passed. Checks cover facility denial, full-filter transaction totals versus page limit, current ledger versus payment date, and own-practitioner denial. Live role/account and database behavior was not verified.
- Next: copy the contract to the frontend repository for Prompt 2. Stop here; no Prompt 2–4 work was started.

## Task: Bounded reporting update (2026-09-23)
- Completed in `src/app/api/v1/reports.py`; focused checks in `tests/test_reports.py`. No schema change, migration, or reporting table.
- `GET /api/v1/reports/visits` adds optional `sort_by=visit_started_at|completed_at|patient_name|practitioner_name|status`, `sort_order=asc|desc` (default `visit_started_at desc`), and `include_daily=true` (default false). Invalid enum values return 422. Sort is applied before pagination with encounter UUID descending as tie-breaker; null sort values are last.
- With `include_daily=true`, `data.daily` is an array of `{date,metrics}` for every facility-local day in the requested range, including zero days. It uses the weekly aggregation over the full filtered range regardless of page size. Existing response fields and top-level `meta` remain.
- Visits and weekly `metrics` add `unique_patients_all_visits`: distinct patient UUIDs over all filtered encounters. The existing `unique_visited_patients` query counts distinct patient UUIDs only where encounter status is `completed`, explaining 5 versus 7 in the supplied 13-encounter case. The new count also appears in daily, practitioner, and weekly `previous_period.metrics`; period distinct counts are queried directly, never summed from days.
- Money remains in minor units with existing mixed-currency availability, captured-payment/completed-refund rules, transaction-date and visit-linked separation. Weekly previous-period cutoff is unchanged. Authorization, timezone, practitioner/patient filters, and 366-day limit remain.
- Checks: `./venv/bin/python -m pytest -q tests/test_reports.py` — 6 passed; Ruff and `git diff --check` passed. A captured endpoint ORDER BY ran against a small in-memory dataset across two pages, including a tie at the boundary. Two-day aggregates reconcile additive visit and money totals; distinct counts are checked separately. A read-only check of the configured local database was attempted, but DNS resolution failed before connection; the supplied 13/7/5 counts were not independently confirmed. No database was changed.
- Frontend request: `GET /api/v1/reports/visits?facility_uuid=<UUID>&date_from=2026-09-22&date_to=2026-09-23&sort_by=completed_at&sort_order=asc&include_daily=true&page=1&page_size=25`. Existing optional `practitioner_uuid` and `patient_query` apply to both table and daily data. `sort_by` defaults to `visit_started_at`; `sort_order` defaults to `desc`; `include_daily` defaults to false. Invalid enum values get HTTP 422; existing range, access, and facility errors are unchanged.
- Frontend response: `data.items` is the sorted page; top-level `meta` retains `page`, `page_size`, `total`, `total_pages`. With `include_daily=true`, `data.daily` has one `{date:"YYYY-MM-DD",metrics:{...}}` object per local date, including zero days, independent of page size. Without it, `data.daily` is absent. Both `data.metrics` and each daily `metrics` add integer `unique_patients_all_visits`; weekly adds the same key to `data.metrics`, `data.daily[].metrics`, `data.practitioners[].metrics`, and `data.previous_period.metrics`.
- Frontend meaning: label `unique_visited_patients` as distinct patients with completed visits; label `unique_patients_all_visits` as distinct patients across all visits. For period cards use `data.metrics`, never the sum of daily distinct counts. Plot additive daily fields directly; render money as minor currency units and show `availability.reason` when money is unavailable. Keep `data.visit_linked` separate from transaction-date money in `data.metrics`.
- Next step: frontend wiring can consume these additive fields; no backend migration is pending for this task.

## Task: Historical billing backfill follow-up
- Clarified demo-seed requirement: `scripts/seed_billing_demo.py` now adds a full captured payment for each newly seeded invoice with a positive fee, and marks that invoice paid. It includes all completed encounters; `--include-in-progress` also seeds visits still in consultation. Invoice/payment dates use the encounter completion or start timestamp so weekly historical reports display the seeded amounts. Dry run rolls back all inserts and prints only aggregate counts. Repeated `--apply` skips encounters with an existing invoice.
- Applied the demo seed to the configured `ENVIRONMENT=local` Neon database for facility `01a0a022-b2c1-76e3-a91d-49f5568eca0d`, using actor `01a0a021-ff4b-785d-95d8-d08e7460943a`, `--effective-from 2026-09-01 --include-in-progress --apply`. The existing ₹4,000 facility fee began September 23, so the seed copied it to one inactive September 1–22 historical fee row, then created 13 paid invoices and 13 captured payments for 6 completed and 7 in-progress visits. Total seeded billing and payments: 5,200,000 minor INR (₹52,000). Week of September 21 through September 23: 8 invoices and 8 payments, 3,200,000 minor INR (₹32,000) each. Second dry run proposed zero rows.
- Added tracked `AGENTS.md` rule: every new data point/master replacement must assess and backfill existing dependent rows, verify counts, or ask for the historical source before completion.
- Added `scripts/backfill_billing_invoices.py`: previews eligible completed encounters with an effective configured fee and counts missing fees; `--apply` creates only missing invoice snapshots, using current issue time and no invented payments. Request is CLI `python -m scripts.backfill_billing_invoices --facility-uuid <UUID> --actor-user-uuid <UUID> [--apply]`; output is aggregate counts, without patient data. Invalid/missing facility or actor raises `ValueError`.
- These are synthetic demo payments based on the configured fee, not recovered transaction history. No new migration was needed; historical refunds were not seeded.
- Check: focused billing/report/backfill tests and Ruff passed.

## Task: Prompt 17 + consultation billing prerequisite
- Status: daily/range reporting and the minimal database/API billing prerequisite are implemented; weekly reporting and all admin/report UI remain out of scope.
- Storage: migration `20260923_33` creates `care.consultation_fee`, `care.invoice`, `care.payment`, and `care.refund`. Existing `identity`, `organization`, and `platform` tables remain references. Amounts are integer minor units with one three-letter currency per facility's active fees.
- Fee resolution: effective fee on the facility-local encounter date uses practitioner override, then specialty override, then facility default. A completed encounter gets at most one invoice; invoice amount/currency/fee link are immutable snapshots, so later fee changes do not rewrite history.
- Permissions: existing `administrator`, `organization_admin`, and `owner` roles manage facility/specialty fees; a doctor may manage only their own practitioner fee. Existing admin/report roles alone can read reports. Existing `billing_staff` plus admins can record/void payments and refunds. All access enforces organization and facility scope.
- Billing API: `GET|PUT /api/v1/facilities/{facility_uuid}/consultation-fees`; `POST|GET /api/v1/encounters/{encounter_uuid}/invoice`; `POST /api/v1/invoices/{invoice_uuid}/payments`; `POST /api/v1/payments/{payment_uuid}/refunds`; void actions are `POST /api/v1/{invoices|payments|refunds}/{uuid}/void`.
- Lifecycle: invoice statuses `issued|partially_paid|paid|voided`; payment `captured|voided`; refund `completed|voided`. Idempotency keys are organization-unique; overpayment and over-refund are rejected. Invoice status uses captured payments less completed refunds. Finance mutations write governance audit events.
- Reporting API: `GET /api/v1/reports/visits?facility_uuid=&date_from=&date_to=&practitioner_uuid=&patient_query=&page=&page_size=`; range max 366 inclusive facility-local days converted to indexed UTC half-open predicates. Totals and page are independent; rows link existing patient and encounter detail routes.
- Count definitions: visit = encounter by `started_at`; completed visit = encounter current status `completed`; unique visited patients = distinct completed-visit patients. Scheduled/cancelled/no-show appointments use `scheduled_start` and current appointment status.
- Money definitions: `billed_amount` sums non-void invoice snapshots by `issued_at`; `payments_received` sums captured payments by `received_at`; `refunds` sums completed refunds by `refunded_at`; `net_collections = payments_received - refunds`. `visit_linked` separately applies encounter-date filters. The two date bases are not expected to reconcile.
- Money availability: results include `availability.amount_unit="minor"`; mixed currencies return money fields `null` with `MIXED_CURRENCIES`. With no invoice/payment/refund and no configured fee currency, they return `null` with `PRICE_DATA_UNAVAILABLE`; a configured single currency returns recorded zeroes.
- Exact success shape: `{"success":true,"data":{"timezone":"Asia/Kolkata","range":{"local_from":"2026-09-23","local_to":"2026-09-23","utc_from":"2026-09-22T18:30:00+00:00","utc_to_exclusive":"2026-09-23T18:30:00+00:00"},"availability":{"money":true,"reason":null,"amount_unit":"minor"},"metrics":{"visits_total":1,"completed_visits":1,"unique_visited_patients":1,"scheduled_appointments":1,"cancelled_appointments":0,"no_show_appointments":0,"billed_amount":50000,"payments_received":30000,"refunds":5000,"net_collections":25000,"currency":"INR"},"date_basis":{"visits":"encounter.started_at","appointments":"appointment.scheduled_start","billed_amount":"invoice.issued_at","payments_received":"payment.received_at","refunds":"refund.refunded_at"},"visit_linked":{"billed_amount":50000,"payments_received":30000,"refunds":5000,"net_collections":25000,"currency":"INR"},"items":[]} ,"meta":{"page":1,"page_size":25,"total":1,"total_pages":1}}`.
- Errors: existing 401; 403 `REPORT_ACCESS_REQUIRED`/`BILLING_ACCESS_REQUIRED`; 404 facility/practitioner/encounter/invoice/payment/refund missing; 409 fee/date/currency conflict, missing fee, incomplete encounter, overpayment/over-refund, invalid void order, or reused idempotency key; 422 invalid UUID/date/range/payload. Valid empty report ranges return 200 with zero counts and empty items.
- Development seed: `python -m scripts.seed_billing_demo ...` is dry-run unless `--apply`; it inserts only missing facility/specialty/practitioner example fees and invoices existing completed encounters with signed SOAP notes. It prints counts only and is never run automatically or in production.
- Migrations: `20260923_32` adds reporting indexes; `20260923_33` adds billing tables/indexes/constraints. Apply to the chosen local/dev database from `src/` with `../venv/bin/alembic upgrade head` (or the environment's equivalent Alembic command). Do not run against a shared database without selecting it explicitly.
- Checks: Ruff passed; 21 focused tests passed, covering facility-local fee precedence, multiple payment/refund totals, refund half-open predicates, denied non-admin report access, independent pagination, and existing encounter/appointment reads. Alembic single head is `20260923_33`; its revision-only offline SQL renders successfully.
- Frontend dependency: no billing admin UI was implemented. Use the API contract above later; do not infer amounts from appointment counts or fees, do not merge visit-date and payment-date totals, and show unavailable values distinctly from zero.

## Task: Prompt 12 — Appointment Rescheduling (Backend)
- Status: Implemented & verified; frontend intentionally not started
- Endpoint: `POST /api/v1/appointments/{appointment_uuid}/reschedule`
- Request: `{ "scheduled_start": "<ISO-8601>", "scheduled_end": "<ISO-8601>", "version": 1, "resource_uuid": "<UUID>|null", "room_uuid": "<UUID>|null", "reason": "<string>|null" }`
- Success: `data` is the canonical appointment view with unchanged `uuid`, updated times/resource, and incremented `version`; `meta.affected_ranges.old` and `.new` contain `start`, `end`, and `resource_uuid`.
- Allowed statuses: `booked` and `confirmed`. `checked_in`, `in_consultation`, `completed`, `cancelled`, and other lifecycle states return `409 APPOINTMENT_STATUS_NOT_RESCHEDULABLE`.
- Errors: `404` appointment missing; `403` facility scope denied; `409 STALE_VERSION` for stale/retried requests, `409` availability/resource conflict, or `409 SLOT_UNAVAILABLE` for a database overlap race; `422` invalid/unnormalizable interval.
- Transaction: locks the appointment, facility, and practitioner; validates the complete target interval through `validate_interval(ignore_appointment_id=...)`; commits timing/resource/version/audit together. Existing resource exclusion constraint remains the database race guard.
- Audit: `appointment.rescheduled` stores old/new ranges, actor, reason, and resulting version in `governance.audit_log.details`.
- Migration pending: `src/migrations/versions/20260922_27_add_appointment_version_and_audit_details.py` adds `care.appointment.version` (default `1`) and `governance.audit_log.details`; apply with `alembic upgrade head` in the target dev database.
- Checks: `./venv/bin/python -m pytest -q tests/test_exception_booking.py tests/test_appointment_views.py tests/test_scheduling_availability.py` — 38 passed.
- Next step: stop for Prompt 12; frontend wiring and realtime events belong to later prompts.

## Task: Practitioner Schedule Storage & Read/Write APIs (Backend)
- Status: Completed & Verified
- Next Step: Connect effective-availability validation in Prompt 10

### 1. Endpoints & Paths
- `GET /api/v1/practitioners/{practitioner_uuid}/schedule` (query: `facility_uuid`) & `GET /api/v1/facilities/{facility_uuid}/practitioners/{practitioner_uuid}/schedule`
- `PUT /api/v1/practitioners/{practitioner_uuid}/schedule` & `PUT /api/v1/facilities/{facility_uuid}/practitioners/{practitioner_uuid}/schedule`
- `POST /api/v1/practitioners/{practitioner_uuid}/schedule/reset` & `POST /api/v1/facilities/{facility_uuid}/practitioners/{practitioner_uuid}/schedule/reset`
- Model: `src/app/models/care.py` (`PractitionerSchedule`)
- Schemas: `src/app/schemas/practitioner_schedule.py`
- Router: `src/app/api/v1/practitioner_schedules.py`
- Tests: `tests/test_practitioner_schedules.py`

### 2. Frontend Contract
- **Read Request**:
  `GET /api/v1/practitioners/{practitioner_uuid}/schedule?facility_uuid={facility_uuid}`
- **Read Response (Inherited Practice Defaults / No Override)**:
  ```json
  {
    "success": true,
    "data": {
      "practitioner_uuid": "01a0a022-b31b-728f-b7ae-b06211a65893",
      "facility_uuid": "01a0a022-b2c1-76e3-a91d-49f5568eca0d",
      "organization_uuid": "01a0a022-b2a0-7000-8000-000000000001",
      "timezone": "Asia/Kolkata",
      "slot_interval_minutes": 30,
      "effective_from": null,
      "effective_to": null,
      "is_override": false,
      "inherited": true,
      "version": 0,
      "weekly_hours": [
        {
          "day_of_week": "monday",
          "is_working": true,
          "start_time": "08:00",
          "end_time": "20:00",
          "breaks": []
        }
      ],
      "date_exceptions": [],
      "updated_at": null
    },
    "meta": {}
  }
  ```
- **Update Request (`PUT`)**:
  ```json
  {
    "facility_uuid": "01a0a022-b2c1-76e3-a91d-49f5568eca0d",
    "timezone": "Asia/Kolkata",
    "slot_interval_minutes": 30,
    "effective_from": "2026-10-01",
    "effective_to": null,
    "version": 0,
    "weekly_hours": [
      {
        "day_of_week": "monday",
        "is_working": true,
        "start_time": "09:00",
        "end_time": "17:00",
        "breaks": [
          {"title": "Lunch", "start_time": "13:00", "end_time": "14:00"}
        ]
      }
    ],
    "date_exceptions": [
      {
        "date": "2026-10-15",
        "exception_type": "leave",
        "reason": "Conference"
      }
    ]
  }
  ```
- **Error Responses**:
  - `403 Forbidden`: `{"code": "FORBIDDEN", "message": "Doctors are only permitted to manage their own schedule."}`
  - `409 Conflict`: `{"code": "STALE_VERSION", "message": "The schedule was modified by another transaction. Please reload and try again.", "details": [{"current_version": 2, "submitted_version": 1}]}`
  - `422 Unprocessable Entity`: `UNSUPPORTED_OVERNIGHT_INTERVAL` when overnight shift or break crossing midnight is submitted.

### 3. Migrations & Storage
- **Alembic Migration**: `src/migrations/versions/20260922_26_create_practitioner_schedule.py` (Revises `20260922_25`).
- **SQL Script**: `sql/healthos_practitioner_schedule.sql`.
- **Zero-Backfill Inheritance**: Unmodified practitioners generate no duplicate database rows.
- **Reset to Defaults**: Ends active override (`is_active = false`, `effective_to = reset_date`) preserving historical rows for audit.
- **Audit Logging**: Actions recorded: `practitioner_schedule.created`, `practitioner_schedule.updated`, `practitioner_schedule.reset`.

### 4. Verification
- 14 focused unit tests in `tests/test_practitioner_schedules.py` + 74 full suite tests passing.
- Verified: dynamic facility inheritance, doctor own-vs-other permission barrier, optimistic locking stale version rejection, break containment and overlap rules, overnight interval rejection, and history-preserving reset.

## Task: BE01 — Salutation Identity Support (2026-09-29)

### Status
VERIFIED on the configured `ENVIRONMENT=local` database (`neondb`). Next step: FE01 Phase B.

### Migration
- Revision `20260929_41` (`src/migrations/versions/20260929_41_add_salutation_master.py`, revises `20260925_40`); single Alembic head. Rollback/downgrade implemented.
- Applied to the configured local database: `cd src && ../venv/bin/alembic upgrade head` → `20260925_40` → `20260929_41`. Verified via `alembic current` and `information_schema` (table, nullable columns, FKs); `identity.person` retained all 124 existing rows.
- New `identity.salutation` (`id`, `code UNIQUE`, `display_name`, `abbreviation`, `sort_order`, `is_active`, `created_at`, `updated_at`). `identity.person.salutation_id` nullable FK → `identity.salutation.id` (ON DELETE SET NULL). Link added to `identity.user_account.person_id` nullable FK → `identity.person.id` (staff/doctor person read-back). `identity.practitioner` unchanged.

### Seed
- Migration-based reference seed (matches the existing medical-council pattern) plus re-runnable `scripts/seed_salutations.py`; both keyed on stable `code`.
- Seeded codes: DR/Doctor/Dr./10, MR/Mister/Mr./20, MS/Ms/Ms./30, MRS/Mrs/Mrs./40, MX/Mx/Mx./50, PROF/Professor/Prof./60, `is_active=true`. Deterministic UUIDs `uuid5("healthos:salutation:<code>")`.
- Idempotency verified: `python -m scripts.seed_salutations` run twice → "inserted: 0" each time; DB count 6, duplicate codes 0.

### Contract
- `GET /api/v1/masters/salutations` (public, no auth; optional `is_active=true`), envelope `{success,data:{items:[{id,uuid,code,display_name,abbreviation,sort_order,is_active}]},meta:{count}}`, ordered `sort_order ASC, code ASC`; static fallback list on DB error.
- `POST /api/v1/organizations` accepts optional `salutation_id`; `POST /api/v1/staff/invitations` accepts/stores optional `salutation_id` and `accept` persists it; `POST/PATCH /api/v1/patients` accept optional `salutation_id` on the person.
- Persistence: `identity.person.salutation_id`. Person/legal name is never auto-prefixed or stripped; salutation is never derived from role/profession/designation/specialty/gender/name.
- Validation: omitted/null accepted; malformed UUID → 422 `VALIDATION_ERROR`; nonexistent/inactive → 422 with `"Salutation does not exist or is inactive."`.

### Files
`src/app/models/{identity,organization,__init__}.py`, `src/app/domains/identity/{__init__,salutations}.py` (new), `src/app/domains/organization/service.py`, `src/app/api/v1/{masters,organizations,staff,patients}.py`, `src/app/schemas/{access,staff,patients}.py`, `src/migrations/versions/20260929_41_add_salutation_master.py` (new), `scripts/seed_salutations.py` (new), `tests/test_salutation.py` (new).

### Tests
- `python -m pytest -q tests/test_salutation.py tests/test_masters.py tests/test_staff_invitation.py tests/test_patient_schema.py` → 41 passed.
- Full suite (`--ignore=tests/test_user.py`, pre-existing collection error): 319 passed, 5 skipped, 5 failed — all 5 are pre-existing `appointment.version` SimpleNamespace failures in `test_encounter_resume.py`/`test_walk_ins.py`, unrelated to salutation.
- Migration offline render (`alembic upgrade 20260925_40:20260929_41 --sql`) rendered the DDL and seed inserts (offline binds show NULL, expected). Ruff clean on new files; remaining repo lint warnings pre-existing.
- Legacy prefixed names (e.g. "Dr. Vikram Sen") left untouched; no automatic normalization or inference.

### Handoff
BE -> FE handoff written to `/Users/ganeshsawant/Documents/work/healthos/handoffs/BE01_SALUTATION_BE_TO_FE.md`. Do not mark FE01 Phase B complete.

## Hotfix: staff_invitation.salutation_id missing (2026-09-29)

### Problem
`POST /api/v1/staff/invitations` raised `UndefinedColumnError: column "salutation_id" of relation "staff_invitation" does not exist` (`src/app/api/v1/staff.py:362`). BE01 declared the model/schema/API support but migration `20260929_41` only added `salutation_id` to `identity.person`, never to `organization.staff_invitation`. The frontend was already correct (`InviteStaffDrawer.tsx` fetches `GET /masters/salutations` and posts `salutation_id`), so no FE change was required.

### Migration
- Revision `20260929_42` (`src/migrations/versions/20260929_42_add_staff_invitation_salutation.py`, revises `20260929_41`); single Alembic head, downgrade implemented.
- Adds nullable `organization.staff_invitation.salutation_id UUID`, FK `fk_organization_staff_invitation_salutation` → `identity.salutation.id` (ON DELETE SET NULL), index `ix_organization_staff_invitation_salutation_id`.
- Applied to configured local Neon DB: `cd src && ../venv/bin/alembic upgrade head` → `20260929_41` → `20260929_42`. Verified via `alembic current` and `information_schema`/`pg_constraint`/`pg_indexes`. No backfill needed: pre-existing invitations stay NULL (salutation was never captured for them).

## Hotfix: production CORS origin missing (2026-10-05)

### Root cause
- Production frontend moved to `https://healthos.medikai.in`, but the effective `CORS_ORIGINS` did not include it, so Cloud Run `CORSMiddleware` answered the preflight with `400 Disallowed CORS origin`. The allow-methods/headers/credentials in that 400 response match the code defaults, and no `.env` ships in the image, so the service was relying on defaults (or a stale env value).
- The `CORS_ORIGINS` list field was JSON-decoded by pydantic-settings: a comma-separated or bare value would raise `SettingsError` at startup, while trailing slashes, whitespace, or quotes parsed but could never match the browser `Origin` header.

### Files changed
- `src/app/core/config.py`: `CORS_ORIGINS` now defaults to localhost origins plus `https://healthos.medikai.in`; field uses `NoDecode` with a before-validator that accepts JSON arrays, comma-separated strings, or a single origin, and normalizes whitespace/quotes/trailing slashes and de-duplicates. `CORS_METHODS`, `CORS_HEADERS`, and `allow_credentials=True` unchanged.
- `.env.example`: documented production sample now includes the production origin and localhost, with a note that origins take no trailing slash.
- `tests/test_cors.py` (new).

### Tests performed
- `./venv/bin/python -m pytest -q tests/test_cors.py` → 6 passed: defaults include production/localhost; JSON, comma-separated, and single-origin env values parse; `OPTIONS /api/v1/auth/login` from `https://healthos.medikai.in` returns 200 with matching `Access-Control-Allow-Origin` and `Access-Control-Allow-Credentials: true`; `https://evil.example.com` returns 400 with no allow-origin.
- End-to-end local reproduction with `create_application` + default settings confirmed 200 for the production origin and 400 for the evil origin; `test_client_cache_middleware.py` passed; Ruff clean on changed files.

### Configuration / environment
- Cloud Run uses no `.env` file; if the service already defines `CORS_ORIGINS`, it overrides the new default: set it to `["https://healthos.medikai.in","http://localhost:5173","http://127.0.0.1:5173"]`, preserving any other live origins. No LB, DNS, or frontend change.

### Deployment requirement
- New Cloud Run revision required (code default changed). Env update required only if `CORS_ORIGINS` is already defined on the service.

### Verification command
`curl -i -X OPTIONS 'https://api.medikai.in/api/v1/auth/login' -H 'Origin: https://healthos.medikai.in' -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: content-type,authorization'` → expect `HTTP/2 200`, `access-control-allow-origin: https://healthos.medikai.in`, `access-control-allow-credentials: true`.

### Handoff status
- Backend hotfix complete and locally verified; live Cloud Run deploy and curl verification pending.

### Next step
- Deploy the revision, apply the `CORS_ORIGINS` value if the variable is already set, run the verification curl, then confirm frontend login preflight succeeds.

## Task: PA-DISCOVERY-01 — Patient application discovery (2026-10-05)

### Status
READ-ONLY discovery complete. No code, config, migration, seed or deployment changed. No database connected; no migrations or tests executed. `healthos-patient` does not exist, so no patient app was created or modified.

### Inspected paths
`src/app/main.py`, `src/app/core/{config,setup,security}.py`, `src/app/api/v1/{auth,patients,scheduling,queue,encounters,clinical,documents,billing,staff,bootstrap,events,events_stream}.py`, `src/app/api/dependencies.py`, `src/app/middleware/csrf_middleware.py`, `src/app/models/{identity,care,organization,billing,communication,platform,user}.py`, `src/app/schemas/*`, `src/app/domains/**`, `src/migrations/versions/*` (43 revisions), `scripts/*`, `src/scripts/*`, `tests/*`, `.env.example`, `docs/healthos-upgrade-progress.md`, plus (read-only, outside repo) `~/Downloads/05102026.sql`.

### Findings
- Authentication: Logto BFF opaque cookie `healthos_session` (HttpOnly, path `/`, SameSite lax, secure default true) + CSRF, local email/password JWT HS256 (default 30 min), email OTP recovery. **No patient authentication, patient role, patient invitation/link or phone OTP login exists.** Principal `get_current_identity_account` returns `UserAccount`; all patient/clinical routes require `_staff_context`/`_scope`.
- Schema bridge for a future portal: `UserAccount.person_id → identity.person` and `Patient.person_id → identity.person`; `Patient` has no `user_account_id`. Staff invitation accept (`staff.py:468-642`) is the reusable linking blueprint.
- Capability classes: A existing/correct (practitioner payouts, staff-only); B domain services requiring patient-scoped APIs (booking/reschedule/cancel, availability next-slots, queue token/self check-in, vitals read, SOAP/signing, prescriptions, documents, invoices/balances/transactions, audit recording); C missing (patient auth, patient invite/link, appointment request/approval/alternatives, released visit summary, prescription PDF, document release policy/patient upload, receipts/discounts/self-pay, patient notifications/push/realtime, patient-vs-staff cancel distinction, `confirmed` writer); D unverified (`/admin/audit-logs` authorization, `confirmed` lifecycle).
- Request/response/error contracts: success `{success,data,meta}`; errors `{success:false,error:{code,message,details},meta:{request_id}}` (`core/setup.py:153-176`); pagination keyset cursors for patients (`patients.py:138-212`) and page/page_size for billing; idempotency unique `(org,idempotency_key)` for appointments/payments (payments replay, appointment create maps conflict to 409); booking conflicts 409 `SLOT_UNAVAILABLE`; version-optimistic reschedule 409 `STALE_VERSION`.
- CORS defaults currently `["http://localhost:5173","http://127.0.0.1:5173","https://healthos.medikai.in"]` (`config.py:151-165`); patient `http://localhost:5174` / `http://127.0.0.1:5174` not yet present. Cookies are not port-isolated; distinct patient cookie name or Bearer-only patient sessions recommended.

### Migration status
VERIFIED from source: single Alembic head `20260929_42` (43 linear revisions; recent 33 billing, 34 earnings, 35-38/40 communication, 39 recovery, 41 salutation, 42 staff-invitation salutation). UNKNOWN/UNVERIFIED against any database: applied revision and schema state. `HealthOS_API` has no `.env` and no DB environment variables are set in this environment, so no authorized read-only lookup was possible.

### Account verification
`ganesh.sawant@citycareclinic.demo` is not defined by any backend seed. It exists only in frontend mock code (`healthos-frontend/src/lib/mock-client.ts:605-623`) as owner/organization_admin staff. Database existence: PENDING. It was not modified and must not be treated as a patient login. Minimal mock identifiers recorded in the handoff for later synthetic-seed comparison.

### Schema reference
`~/Downloads/05102026.sql` is a 1.5 MB, 63,233-line schema-only snapshot with 47 application tables and zero row data (`COPY`/`INSERT` = 0). It omits billing (33-34), communication (35-38/40), recovery (39), salutation (41) and `staff_invitation.salutation_id` (42); it must never be executed and cannot verify the demo account.

### Checks executed
Read-only source inspection; `venv/bin/python --version` (3.12.8); requirements review; Alembic revision-head inspection; SQL table-name extraction and row-data absence; shell env check for DB variables. NOT executed: migrations, seeds, pytest, app startup, DB connections.

### Handoff
Canonical: `/Users/ganeshsawant/Documents/work/healthos/handoffs/patient-app/discovery/` (README.md, workspace-paths.json, frontend-design-and-reuse.md, design-tokens.json, backend-api-and-schema.md, screen-api-matrix.md, gaps-and-decisions.md, discovery-bundle.md). Copy verified by SHA-256 at `HealthOS_API/docs/patient-app/discovery/`. Sanitized ZIP: `/Users/ganeshsawant/Documents/work/healthos/handoffs/patient-app/healthos-patient-discovery.zip`.

### Next step
Use the discovery bundle to produce ONE authoritative healthos-patient implementation plan and prompt sequence (patient auth + ownership, patient-scoped APIs over existing storage, CORS 5174, cookie/session isolation, token reconciliation, missing screens). Do not begin implementation in the discovery stage.

## PA-FE00 — healthos-patient bootstrap (2026-10-05)

### Status
**HANDOFF_READY (frontend stage).** No backend code, config, migration, seed or deployment was
changed in PA-FE00. This repository changed only by adding
`docs/patient-app/handoffs/EXECUTION_RULES.md` and `docs/patient-app/handoffs/00-bootstrap.md` and
this progress append. No DB connection was made; applied migration state remains UNKNOWN; head is
still `20260929_42`.

### Frontend delivered
Standalone `healthos-patient` app on `localhost:5174` with proposed patient contracts only:
- `POST /api/v1/patient/auth/otp/request` `{phone}`; `POST /api/v1/patient/auth/otp/verify`
  `{phone, code, request_id?}` → `{access_token, token_type:"bearer", patient}`.
- `GET /api/v1/patient/me`; `POST /api/v1/patient/auth/logout`.
- `GET /api/v1/patient/appointments?scope=upcoming|past`; `POST /api/v1/patient/appointment-requests`.
  Errors use `{success:false,error:{code,message,details},meta:{request_id}}` with proposed codes
  `OTP_INVALID`, `OTP_EXPIRED`, `OTP_RATE_LIMITED`, `RECORD_LINK_REQUIRED`, `RECORD_NOT_LINKED`,
  `VALIDATION_ERROR`. Full request/response examples: `00-bootstrap.md` §5.

### Backend-facing semantics requested
- Patient auth must be separate from staff `UserAccount`; Bearer-first patient session; the staff
  `healthos_session` cookie must never authenticate patient endpoints. A distinct patient cookie or
  Bearer-only session is required (cookies are not port-isolated).
- Record access requires a clinic-verified link between the authenticated patient principal and the
  patient record; phone/name/DOB alone are insufficient.
- If direct-origin calls are chosen instead of the dev proxy, add `http://localhost:5174` and
  `http://127.0.0.1:5174` to `CORS_ORIGINS` while keeping staff origins.
- Patient-scoped release policy is required for visit summaries/vitals/prescriptions/documents;
  read-only bills/receipts; patient notifications/preferences. No self-check-in, dependents,
  uploads, online payments, video or AI.

### Checks
No backend checks were run (out of stage scope). Frontend checks are recorded in
`handoffs/patient-app/implementation/00-bootstrap.md` §7.

### Next step
**PA-BE01** (backend) after the FE → BE contract review: confirm/revise the proposed patient OTP +
session + record-link contracts, author the migrations and publish OpenAPI. Do not start until the
preceding FE stage handoff is reviewed.

## PA-FE01 — Patient FE handoff ready for PA-BE01 (2026-10-05)

### Status
**HANDOFF_READY.** No backend code, migration, seed or config changed by this frontend stage. The
1 pre-existing porcelain entry (PA-DISCOVERY append) is preserved; PA-FE01 adds only handoff copies
and this append.

### Frontend delivered (patient app, proposed contracts only)
- Complete patient UI in six bounded groups; typed gateway defaulting to real
  `/api/v1/patient/...` HTTP with a dev-only preview mode that is impossible in production
  (verified: 0 fixture strings in the production bundle).
- `api-contract.proposed.yaml` (33 paths, 40 schemas) and `screen-api-matrix.md` copied here under
  `docs/patient-app/handoffs/`; companion handoff `01-fe-to-be.md`.

### Backend-facing semantics requested
- Separate patient principal/session: preferred HttpOnly `healthos_patient_session` + CSRF, or
  patient-scoped opaque bearer; staff `healthos_session`/JWT must never authenticate patient routes.
- Patient-owned projections for every object (no staff list/search endpoint reused by hiding UI).
- Request-first appointments (`requested` default; auto-confirm only as explicit clinic policy),
  request-only cancel/reschedule with `version` + `Idempotency-Key`, original valid until approval.
- Release flags for summaries/prescriptions/documents; read-only bills with staff-applied discount
  and receipt availability; patient notifications/preferences model.
- Staff actions to schedule in PA-BE01: invitations/link review, request approve/decline/alternative,
  record release/revoke, discounts/payments/receipts, queue tokens, notification delivery.
- If direct-origin calls replace the dev proxy: add `http://localhost:5174` and
  `http://127.0.0.1:5174` to `CORS_ORIGINS` preserving staff origins.

### Checks
No backend checks (out of stage scope). Frontend checks and exact results: `01-fe-to-be.md` §9.
Backend Alembic head at inspection `20260929_42`; applied state UNKNOWN. No seed run.

### Handoff
`01-fe-to-be.md` SHA-256 `4ab4cda6983720260eeedaaa1738e74e0893caac1bd05c499699328d04182596`
plus the two companions, hash-verified here and in the staff/patient handoff dirs.

### Next step
**PA-BE01, Prompt 3**: review/confirm the proposed contract, implement the patient backend and the
staff actions, publish OpenAPI, then return a BE → FE handoff for PA-FE02.

## PA-BE01 — Patient identity/auth/link slice implemented (2026-10-05)

### Status
**HANDOFF_READY** for the auth/link slice. Later domain slices **PLANNED** (PA-BE02); real SMS
delivery **BLOCKED** on vendor credentials. No production change and no real SMS send.
Repo `feature/salutation_1` @ `038ac04f724b94ad621e7e5ee2ad0e2272c509c8`, dirty (24 porcelain
entries: PA-BE01 work plus the pre-existing PA-FE01 progress append).

### Architecture (normative, `identity-architecture-decisions.md`)
- New patient principal `identity.patient_portal_account`, separate from staff `UserAccount`;
  staff `UserAccount.person_id` is never overwritten. Patient routes resolve only the
  `healthos_patient_session` cookie via `get_current_patient`; staff cookies/JWTs/dev bypass and
  `Authorization` headers are never consulted.
- Explicit mapping `identity.patient_record_link` (account → organization-scoped clinical patient)
  with status (`pending|verified|rejected|revoked`), `request_kind`, `verification_source`,
  reviewer/time, optional `facility_id`, revocation; partial unique indexes enforce one active link
  per account+organization and per clinical patient. Staff-authorized, expiring, single-use,
  phone-bound `identity.patient_link_invitation` (keyed HMAC token). No auto-linking from
  name/DOB/phone.
- Sessions `identity.patient_portal_session` store only a SHA-256 digest plus per-session CSRF;
  login/phone change rotate, logout revokes, phone change revokes all. OTP challenge/throttle
  tables reuse recovery cryptography primitives (HMAC verifier, atomic single-use consumption,
  aggregate throttling) without recovery grant semantics.
- Provider adapter: `dev` (allowlisted synthetic phones, inert in production, local-only code
  echo behind `PATIENT_OTP_DEV_EXPOSE_CODE`) and `unconfigured` (fail-closed 503). No universal
  master OTP, staff bypass, debug endpoint or OTP logging.
- `organization.organization.portal_enabled` (default false) gates `GET /patient/clinics` and
  `POST /patient/link/request`; organization admins toggle it via audited
  `PATCH /organizations/{uuid}/portal-settings`.

### Endpoints mounted
Patient: `POST /patient/auth/otp/request|resend|verify`, `GET /patient/auth/session`,
`POST /patient/auth/logout`, `POST /patient/auth/phone/change/request|verify`, `GET /patient/me`,
`PATCH /patient/profile`, `GET /patient/linked-clinics`, `POST /patient/link/activation`,
`POST /patient/link/request`, `GET /patient/clinics`.
Staff: `GET|POST /patient-links/invitations`, `POST /patient-links/invitations/{uuid}/revoke`,
`GET /patient-links/requests`, `POST /patient-links/requests/{uuid}/approve|reject`,
`POST /patient-links/{uuid}/revoke`, `GET /patient-links`, `PATCH /organizations/{uuid}/portal-settings`.
CSRF: `X-CSRF-Token` required for cookie-authenticated patient writes (OTP bootstrap exempt); the
patient namespace is evaluated before the staff Bearer shortcut, so a bogus Bearer cannot bypass CSRF.
Staff behavior and the `healthos_session` cookie are unchanged.

### CORS
Code defaults now include `http://localhost:5174` and `http://127.0.0.1:5174`, preserving
5173/127.0.0.1:5173 and `https://healthos.medikai.in`. `src/.env` overrides `CORS_ORIGINS`
explicitly, so direct-origin local testing requires adding the 5174 origins to that variable
(defaults are bypassed when the env var is set). `.env.example` documents this.

### Migration
Authored and **applied** to the configured `ENVIRONMENT=local` DB (the authorized local dev DB):
`20261005_43` revises `20260929_42`, additive only. Before: `alembic current` = `20260929_42`
(applied). Offline SQL previewed (exit 0), then `alembic upgrade head` → `20261005_43 (head)`.
Verified `identity.patient_portal_account|session|otp_challenge|otp_throttle|record_link|
link_invitation` and `organization.portal_enabled` default false; all new tables 0 rows after tests;
no existing rows/relations altered. No `create_all`, no SQL export replay, no seed.

### Checks executed
- `ruff check` on all new/changed files → clean (pre-existing repo-wide `RUF012 __table_args__`
  pattern remains in `identity.py`).
- Unit/middleware/CORS: `tests/test_patient_phone.py tests/test_patient_otp_service.py
  tests/test_patient_csrf.py tests/test_patient_authorization.py tests/test_cors.py` → 37 passed
  (OTP expiry/replay/attempt fail-closed/cooldown/throttle, dev-provider production inertness and
  allowlist, cookie coexistence, patient-vs-staff CSRF, bogus-Bearer bypass denial).
- DB integration, unique synthetic rows with cleanup: `HEALTHOS_ALLOW_LOCAL_DB_TESTS=1 pytest -q
  tests/test_patient_portal_db.py` → 4 passed (session fixation/rotation/revocation, HTTP cookie
  flow + profile CSRF + logout, invitation recipient mismatch/replay/expiry/revocation, staff
  approval with wrong/correct clinical patient, cross-organization isolation, unlinked/revoked,
  same-browser coexistence with an overlapping staff/patient identity). Also runs against a
  distinct `TEST_POSTGRES_ASYNC_URL` when provided.
- Existing auth regressions (post-login, salutation, staff invitation, recovery, CORS, cache
  middleware, patient schema, masters) → 69 passed.
- Full suite `pytest -q --ignore=tests/test_user.py` → 356 passed, 8 skipped, 5 failed, 21 subtests;
  the 5 failures are the pre-existing `appointment.version` SimpleNamespace failures documented
  under BE01 salutation. No new regression.
- Actual OpenAPI export `scripts/export_openapi.py` → 176 paths, 30 patient/portal paths.
- Not verified: real SMS delivery (blocked), browser visual pass.

### Handoff / contract
`handoffs/patient-app/implementation/`: `02-be-auth-to-fe.md`,
`identity-architecture-decisions.md`, `api-contract.accepted.yaml`, `openapi.patient-be01.json`.
Copied to `healthos-frontend`, `HealthOS_API`, `healthos-patient`
`docs/patient-app/handoffs/` and SHA-256 verified identical. `api-contract.proposed.yaml` remains
the draft for PLANNED slices.

### Next step
**PA-BE02, Prompt 4** in this backend: implement patient-owned projections (home, portal clinic
directory, appointment request workflow, queue token read, released records/vitals/prescriptions/
documents, read-only bills, notifications) over the existing clinical storage behind the verified
link gate; staff-side approval/release flows; then PA-FE02 switches the patient gateway to the
cookie session (`credentials:'include'` + `X-CSRF-Token`).


## PA-BE02 — Patient domain slices implemented (2026-10-06)
- Status HANDOFF_READY. Migration head `20261006_45` (new `20261006_44` + follow-up `45`), applied to the configured `ENVIRONMENT=local` DB; additive only. Real SMS/push credentials and GCS document bytes remain BLOCKED.
- Implemented patient cookie-session endpoints: clinic specialties/practitioners/sanitized availability, request-first appointment workflow (requested/approved/rejected/alternative_proposed/withdrawn/expired; idempotency replay + key-reuse 409; no capacity reservation; atomic approval with alternative/rejection on conflict; auto-confirm only via `facility.portal_auto_confirm`), own appointments/status polling/queue-token read, released visit summaries/vitals, signed-and-released prescriptions with real generated PDF, released document authenticated streaming, read-only bills with staff discounts and payment-backed PDF receipts, in-app notifications/preferences/push-device binding (push truthful), clinic support data.
- Staff: `/api/v1/appointment-requests` review (approve/reject/propose-alternative), `/api/v1/record-releases` release/revoke with frozen snapshots, `POST /invoices/{uuid}/discounts`, facility portal policy. Roles and facility scope enforced; existing staff endpoints unchanged.
- Checks: ruff clean; 6 unit + 1 DB integration (gated) passed; full suite 362 passed/10 skipped/5 pre-existing failures; `scripts/seed_patient_demo.py` dry-run then apply twice on the local DB with no duplicates (Asha Kulkarni fixtures, manifest at `docs/patient-app/handoffs/demo-fixture-manifest.json`).
- Handoffs copied and SHA-256 verified in canonical + staff + patient repos: `03-be-domain-to-fe.md`, `03-be-to-staff.md`, `api-contract.accepted.be02.yaml`, `openapi.be02.json`, `demo-fixture-manifest.json`. Next: **PA-STAFF01, Prompt 5 in healthos-frontend**.


## PA-STAFF01 — Staff frontend consumed the PA-BE02 staff contract (2026-10-06)
- `healthos-frontend` implemented the staff workflows against the exact accepted contract (appointment-request review, portal link/invitation review and revocation, record release/revoke, invoice discounts, payment-backed receipts) and published `docs/patient-app/handoffs/04-staff-to-patient.md` (+ `pa-staff01-live-verify.py`), SHA-256 verified across all three repositories.
- Live verification against the configured local DB (synthetic rows cleaned) passed 36/37 checks, including staff approval → patient-visible `confirmed` appointment, link approve/revoke authorization and patient visibility, release/revoke visibility, discount projection and payment-backed receipt PDF, and role denials.
- **Defect found (not fixed here):** `POST /api/v1/appointment-requests/{uuid}/propose-alternative` raises `TypeError("normalize_range() got an unexpected keyword argument 'facility_timezone'")` — `src/app/api/v1/appointment_requests.py:248` calls `core/timezones.normalize_range(start, end, facility_timezone=...)` while `src/app/core/timezones.py:27` defines `normalize_range(start, end, tz)`. Repro in the handoff §6.1.
- Requested read/contract additions recorded in the handoff §6: gross/discount fields on staff invoice list/detail, a staff receipt endpoint (or confirmation that receipts stay patient-only), and read exposure of `organization.portal_enabled` / `facility.portal_auto_confirm`.

## PA-FE02 — Patient FE integration results + backend issues for PA-BE03 (2026-10-06)

### Status
No backend code, migration, seed or config was changed by PA-FE02. The frontend integrated against
the ACTUAL mounted patient API (source inspection + live HTTP probes with a real cookie session on
the configured local DB). **Backend issues remain; next is PA-BE03, Prompt 7.**

### Integration results (patient side, live)
- PASS: `auth/session`, `me`, profile CSRF enforcement, `linked-clinics`, clinic activation/request,
  clinics/specialties/practitioners/availability, appointment list/detail/status-query, cancel-request
  (`meta.original_preserved`), withdraw, respond, queue-token, released visits/detail, prescriptions
  + generated PDF, bills/detail + payment-backed receipt PDF, notifications/read, preferences,
  support, 401/404 isolation, and staff↔patient cookie coexistence.
- BLOCKED: OTP request/verify/resend (dev provider unconfigured; B2), document bytes (no GCS; B4),
  push device binding (no client FCM config; B3).
- Staff loop re-run: 36/37 PASS (only propose-alternative fails).

### Backend defects to fix in PA-BE03
1. **B1 (P0)** `normalize_range()` is called with `facility_timezone=` (a timezone string) but
   `core/timezones.normalize_range(start, end, tz: ZoneInfo)` expects a `ZoneInfo` positionally.
   Sites: `src/app/api/v1/patient_domain.py:370` (create request), `:583` (reschedule-request),
   `src/app/api/v1/appointment_requests.py:248` (staff propose-alternative). All three return 500.
   Repro: `POST /api/v1/patient/appointment-requests` with any valid slot; server log shows
   `TypeError: normalize_range() got an unexpected keyword argument 'facility_timezone'`.
2. **B2** `PATIENT_OTP_DEV_ALLOWLIST` is `Annotated[list[str], NoDecode]` with no validator;
   `.env.example` documents `PATIENT_OTP_DEV_ALLOWLIST='["+919999900001"]'`, but setting it raises
   `ValidationError: Input should be a valid list`. With the default empty allowlist the dev provider
   is disabled and every local `otp/request` returns 503. Add a JSON/comma parser like
   `CORS_ORIGINS` and a local-only warning when the provider is enabled but empty.
3. **B2b** `src/.env` `CORS_ORIGINS` omits `http://localhost:5174`/`http://127.0.0.1:5174`; direct
   preflight from the patient dev origin returns 400 while 5173 returns 200. The Vite dev proxy path
   works. Add the 5174 origins locally and in any deployed value.
4. **B3** `GET /patient/push/status` returns `{available:true, provider:"fcm"}` although delivery is
   unverified and no patient FCM client exists. Consider `configured` vs `delivery_ready`, or a
   truthful `reason`, so clients never imply push works.

### Handoff
`05-fe-integration-to-be.md` SHA-256
`0a1213d5334fd8540899a9db7290e644584979449863725bc7b6c85ee8980c84`, copied and hash-verified here
and in the staff/patient handoff dirs.

### Next step
**PA-BE03, Prompt 7 in this backend**: fix B1 (all three routes), B2, B2b; decide B3. Then PA-FE02
follow-up live verification of booking + OTP, then PA-QA01 Prompt 8.


## PA-BE03 — PA-FE02 defect resolution (2026-10-06)
- **B1 FIXED (reproduced first)**: `normalize_range()` TypeError (wrong kwarg/string helper) in patient create-request, patient reschedule-request and staff propose-alternative; now uses `scheduling._facility_timezone`. Regression test reproduces pre-fix failure and passes post-fix; original appointment preserved on reschedule request.
- **B2 FIXED**: `PATIENT_OTP_DEV_ALLOWLIST` parses documented JSON/comma values (validator + disabled-provider warning); local dev config allowlists the demo synthetic phone, and `POST /patient/auth/otp/request` now returns 200 with local-only `dev_code`. Production stays fail-closed.
- **B2b FIXED**: local `src/.env` CORS_ORIGINS includes 5174 origins (existing origins preserved); real-settings preflight 5174 → 200, 5173 → 200.
- **B3 FIXED (contract revision 1.1.1-be03)**: `/patient/push/status` reports truthful `available = delivery_ready` plus `configured`/`delivery_ready`/`reason`; preferences `push_available` mirrors it. In-app unaffected.
- **B4 remains BLOCKED**: no GCS credentials; no document bytes fabricated.
- Migrations: none authored/applied/pending (head `20261006_45`). Checks: ruff clean; gated regression 1/1; full suite 362 passed/11 skipped/5 pre-existing failures. Handoffs copied + SHA-256 verified in all repos: `06-be-resolution-to-fe.md`, `api-contract.accepted.be03.yaml`, `openapi.be03.json` (openapi identical to BE02 because no route/schema signature changed). Next: **Prompt 6 affected rechecks only, then Prompt 8**.

## PA-QA01 — Backend acceptance verification (2026-10-06)

No backend code, migration or seed changed by QA. Verified against the configured local DB.

### Results
- PA-BE03 fixes re-verified live: patient create-request 201 (idempotent replay with the same key),
  patient reschedule 200 with the original preserved until approval, staff propose-alternative 200;
  OTP request 200 with local-only `dev_code`; direct CORS preflight 5174 → 200 with matching
  allow-origin; `/patient/push/status` reports `available == delivery_ready` with consistent
  `configured`/`reason`; `preferences.push_available` mirrors it.
- QA harness **114/114 PASS**: journeys 1–8 plus cross-patient/cross-org isolation, legacy staff
  endpoints (401 with `AUTH_LOCAL_DEV_BYPASS=false`), CSRF/bogus-bearer handling, production OTP
  fail-closed config and `Cache-Control: no-store`.
- Targeted suites `tests/test_pa_be03_regressions.py tests/test_patient_portal_db.py
  tests/test_patient_domain_db.py tests/test_patient_csrf.py tests/test_patient_authorization.py
  tests/test_cors.py` → **27 passed**.
- Migration `alembic current`/`heads` → `20261006_45 (head)`; none pending.
- Seed `seed_patient_demo.py --apply` twice → second run `created: []` (13 reused), idempotent.
- Staff loop script: canonical 36/37 (fixture proposes `datetime.now()`, outside facility hours →
  409 `FACILITY_CLOSED`); QA-patched copy with only that time corrected → 37/37. Recommend updating
  the staff script fixture to a facility-open slot.

### Blocked
- Document bytes: no local GCS credentials (`503 DOCUMENT_STORAGE_NOT_CONFIGURED` state handled).
- Live push/SMS: no verified credentials; `AUTH_LOCAL_DEV_BYPASS=true` in local `.env` is a
  development-only convenience and must stay false outside local.

### Handoff
`07-acceptance.md` + `pa-qa01-live-acceptance.py` + `pa-qa01-results.json` copied here with
SHA-256 verification (`446f71e6…d895`, `c14eafb7…4da6`, `d78a4acd…969d`).

### Next step
Release-readiness: provide document-storage, FCM and SMS credentials; then re-run the affected
acceptance checks. No backend defect remains open for local scope.

## PA-QA01 — acceptance re-verification (2026-10-06)

QA re-ran the acceptance against the same `ENVIRONMENT=local` test DB (synthetic rows only; no
production access, migration, seed or deployment). Targeted suites (`test_pa_be03_regressions`,
`test_patient_portal_db`, `test_patient_domain_db`, `test_patient_csrf`, `test_patient_authorization`,
`test_cors`) → **27 passed**. `alembic current`/`heads` = `20261006_45 (head)`, none pending. Seed
`--apply` twice → second run `created: []` (13 reused). Acceptance harness → **114/114 PASS**,
byte-identical `pa-qa01-results.json` (`d78a4acd…969d`). Staff loop canonical → **37/37** (run
inside facility hours; the earlier 36/37 was that script's `datetime.now(UTC)` fixture). Live HTTP
probe: OTP request/verify 200, patient `me` 200 `Cache-Control: no-store`, logout 204, post-logout
401; anonymous prescription PDF 401 `no-store`. Legacy staff routes open only under local
`AUTH_LOCAL_DEV_BYPASS=true`; flag-off patient/staff isolation is covered by the passing
authorization tests. `07-acceptance.md` §11 records the re-run; new SHA-256
`b4e3f1795d1503e53044ffcfe38a93ca01e4c059caf5073f6bffa9b9f53225a6` recorded in canonical
`implementation/status.json`. No backend defect open for local scope.

## PA-BE03b — dev-only OTP sandbox opt-in (2026-10-06)

Local OTP testing locked the allowlisted seeded number (5/phone/hour + 60 s resend cooldown) with no
honest bypass. Additive `sandbox` opt-in: honored only when `ENVIRONMENT=local` and the canonical
phone is in `PATIENT_OTP_DEV_ALLOWLIST`; skips phone/IP throttle increments and the resend cooldown,
echoes `meta.sandbox=true`, and fails closed otherwise. No new setting, migration or seed.

### Paths
`src/app/domains/patient_auth/otp.py` (`OtpSandboxUnavailable`, provider `allows_sandbox`,
`sandbox` on `request_code`/`resend_code`, `_response` marker), `src/app/domains/patient_auth/
service.py`, `src/app/schemas/patient_portal.py`, `src/app/api/v1/patient.py` (`_otp_envelope`, 403
translation), `tests/test_patient_otp_service.py`.

### Contract (additive; accepted contract 1.0.1-be01)
- `POST /patient/auth/otp/request` `{phone, sandbox?:bool=false}`; `POST /patient/auth/otp/resend`
  `{challenge_id, phone, sandbox?:bool=false}`.
- 200 with `meta:{sandbox:true}` when effective (`data` unchanged; `dev_code` still governed by
  `PATIENT_OTP_DEV_EXPOSE_CODE`); 403 `OTP_SANDBOX_UNAVAILABLE`
  `details:[{reason:"sandbox_not_allowed"}]` when requested but not permitted; strict behavior
  unchanged (429/410/503 as before).

### Checks (exact)
- `pytest tests/test_patient_otp_service.py tests/test_patient_schema.py tests/test_patient_phone.py`
  → **22 passed**; `ruff check` on changed files → clean.
- Live local: 7 consecutive sandbox requests 200 (strict limit 5/h), immediate sandbox resend 200,
  non-allowlisted sandbox 403, strict request after a sandbox challenge 429 (cooldown intact).
- Handoff `08-sandbox-toggle.md` copied to all three docs dirs (SHA-256 `4278b1c3…8adb`) together
  with `api-contract.accepted.yaml` (`ac05bfd6…53f8`).

### Next step
Unchanged: real SMS/FCM/document-storage credentials. Dev OTP testing can stay strict or opt into
sandbox from the patient sign-in checkbox (DEV only).

## PA-BE03c — demo fixture completes portal opt-in and availability (2026-10-06)

The seeded demo blocked the patient booking wizard: `GET /patient/clinics` returned `[]` and
`patient_domain._link_for_org` rejected the demo org (`CLINIC_NOT_AVAILABLE`) because the seeded
organization had `portal_enabled=false`; additionally no practitioner schedule existed for most
practitioners, so availability was empty. Root cause was demo seed data, not API behavior.

### Paths
`scripts/seed_patient_demo.py`.

### Changes (idempotent; `--apply` required to commit)
- Enables `organization.portal_enabled` for the actor's demo organization (marks
  `organization:portal_enabled`).
- Publishes `care.practitioner_schedule` rows for every **active practitioner lacking an active
  schedule at the demo facility**: Mon–Sat 09:00–17:00 Asia/Kolkata, 30-minute slots, lunch
  13:00–14:00 break, `effective_from` = reference date. Existing active schedules are untouched.
- No migration, no API change, no production data touched.

### Applied state (local dev only)
Dry run first (`created` = 4 schedules, existing 2 reused), then
`venv/bin/python scripts/seed_patient_demo.py --apply --facility-uuid 01a0a022-b2c1-76e3-a91d-49f5568eca0d`
→ `APPLIED`; manifest regenerated at `docs/patient-app/handoffs/demo-fixture-manifest.json`.
Live: demo org `portal_enabled=true`; 6 active schedules; availability 200 with open slots.

### Checks (exact)
- Seed dry run then apply; active schedules for the org = **6**.
- `ruff check scripts/seed_patient_demo.py` → clean.
- Live `GET /patient/clinics` now returns the linked clinic; availability 200 with slots; full
  headless-browser booking created a request (`Appointment request sent`, 6 Oct 09:30–10:00) and
  `/home` lists it under "Requests awaiting the clinic".

### Next step
Unchanged credentials blockers. Optional follow-up: the 30-day availability request takes ~14 s
(31 sequential day evaluations over the remote dev DB) — batch the evaluation window if the demo
latency matters.

## PA-BE-Plan01 — Ably realtime plan published (2026-10-06)

No backend code changed in this task. Published `docs/patient-app/handoffs/09-ably-realtime-plan.md`
(SHA-256 `4c8c0b601f4034de5fa3475639227da9ff8011b09943ac876ffa3a4363a130d2`) plus
`ABLY_REALTIME_PROMPT.md`, covering: patient channel/token endpoints (`/patient/realtime/config|token`,
patient cookie only, CSRF on POST, subscribe-only), patient realtime outbox delivery enqueued from
`create_patient_notification` in the same transaction, `appointment_request.created` staff
notification through the existing `record_notification` workflow plus the SSE invalidation,
`PatientRealtimeChannelState` generation revocation, `ABLY_*` documentation in `.env.example`, and
the security constraints (API key backend-only, opaque events, authorized HTTP refetch).

### Next step
Implement B1–B5 via `ABLY_REALTIME_PROMPT.md`.

## PA-BELY01 — Ably realtime backend B1–B5 implemented (2026-10-06)

### Status
COMPLETE for the backend slice. Migration `20261006_46` applied to the configured `ENVIRONMENT=local` dev DB (additive; `alembic current` → `20261006_46 (head)`); no production access. Live two-identity Ably check **18/18 PASS**. Handoff `10-ably-realtime-backend.md` copied to the canonical, staff and patient handoff dirs.

### Delivered (B1–B5)
- **B1** Patient account-level channel/client builders (`{namespace}:p:{patient_account_id}:g:{generation}`, clientId `patient:{account_id}`, subscribe-only) and `communication.patient_realtime_channel_state` via migration `20261006_46_add_patient_realtime.py` (also adds nullable `delivery_job.recipient_patient_id`).
- **B2** `GET /api/v1/patient/realtime/config` and `POST /api/v1/patient/realtime/token` (`src/app/api/v1/patient_realtime.py`, `.../realtime/patient_service.py`). Patient cookie only (`get_current_patient`), POST requires `X-CSRF-Token`, signed subscribe-only TokenRequest; `ABLY_API_KEY` never returned. Anonymous 401, CSRF 403 `CSRF_VALIDATION_FAILED`, provider down 503 `PROVIDER_UNAVAILABLE`.
- **B3** `create_patient_notification` now enqueues the realtime outbox row in the same transaction (no commit); worker/provider publishes `patient_notification` with `data={notification_id,kind,organization_uuid}` and `recipient_patient_id`. Dedup key `patient-notification:{notification_id}:realtime`; dedup replay and rollback insert nothing.
- **B4** `EVENT_APPOINTMENT_REQUEST_CREATED="appointment_request.created"` (P2 task) + `emit_appointment_request_created` called inside `create_new_request` for requests left pending; recipients = reception (`receptionist`,`facility_operator`,`billing_staff`) + facility/org admins (practitioner intentionally omitted). Auto-confirmed requests are not sent for review. Optional post-commit SSE invalidation `appointment_request.created` published from the patient route.
- **B5** `ABLY_*` documented in `.env.example` (secret backend-only, no `VITE_*`); patient generation bumped atomically on logout and phone change.

### Checks (exact)
- Gated DB/unit: `HEALTHOS_ALLOW_LOCAL_DB_TESTS=1 pytest -q tests/test_patient_realtime.py tests/test_communication_realtime.py tests/test_communication_delivery.py tests/test_communication_notifications.py tests/test_communication_notifications_db.py tests/test_patient_realtime_db.py tests/test_patient_domain_db.py tests/test_patient_portal_db.py tests/test_patient_csrf.py tests/test_patient_authorization.py tests/test_pa_be03_regressions.py tests/test_patient_phone.py tests/test_patient_otp_service.py tests/test_patient_domain_unit.py tests/test_cors.py` → **104 passed, 1 skipped**.
- Full suite `pytest -q --ignore=tests/test_user.py` → **376 passed, 12 skipped, 5 failed** (all pre-existing `appointment.version` SimpleNamespace failures).
- Live: `PYTHONPATH=. venv/bin/python docs/patient-app/handoffs/pa-bely01-live-check.py` → **18/18**: patient books → staff Ably channel receives `appointment_request.created`; staff approves → patient Ably channel receives `patient_notification` kind `appointment_confirmed`; API key absent from token responses; patient capability subscribe-only; patient HTTP refetch shows the confirmation.
- DB hygiene: `patient_realtime_channel_state` 0 rows after tests; all synthetic `SYNRTP`/`SYNTB03`/`pa-bely01` rows cleaned.

### Decisions / notes
- Patient channel is account-level (one account may link to several orgs); `organization_uuid` travels in the event.
- Realtime remains an accelerator: bounded HTTP polling/refetch is the correctness fallback; generation bump revokes old channels on logout/phone change.
- `DeliveryJob.recipient_patient_id` is nullable and mutually exclusive with `recipient_staff_id`; existing staff jobs/envelopes are unchanged apart from the added null `recipient_patient_id` key.
- Two test-helper cleanups were extended (`tests/test_patient_domain_db.py`) to delete communication outbox/notification rows and staff rows before organization deletion; no product behavior change.

### Handoff
- `docs/patient-app/handoffs/10-ably-realtime-backend.md` SHA-256 `cc1c7f2e989d350dc8e7d2f917502176150b7d5a515469e28ea89e7316c3d301`, identical in `HealthOS_API`, `healthos-frontend`, `healthos-patient` and canonical `handoffs/patient-app/implementation/`.
- Evidence: `pa-bely01-live-check.py` SHA-256 `a52b95465967251402fd0843cfe3fb787b84942df7eea6822c84b7f0f86c2a39`; `_pa_bely01_subscriber.js` SHA-256 `fd86e368509a3a8f2a4f027ec79e0b52b051be4eb2529bb952312805ba625352` (canonical copies).

### Next step
Patient app **P1–P5** against the contract in `10-ably-realtime-backend.md`; optional staff **S2** SSE subscription. No backend blocker remains (real FCM/SMS/document-storage credentials still pending from earlier stages).

## FG-CARDS-ANALYSIS — Foreground notification card handoff published (2026-10-06)

### Status
Documentation-only analysis. **No backend code changed; inspection was read-only.** The canonical
handoff `../handoffs/foreground-notifications/` (`00-analysis.md`,
`01-implementation-plan.md`, `02-acceptance-checklist.md`, `status.json`) was copied
byte-identically to `docs/foreground-notifications/` (SHA-256 verified). `status.json` records
analysis=complete / implementation=not_started and `changeRequired=false` for `HealthOS_API`.
Backend HEAD at publication: `038ac04f724b94ad621e7e5ee2ad0e2272c509c8`
(`feature/salutation_1`, dirty pre-existing work preserved).

### Verified (source)
Staff `appointment_request.created`: `data.notification_id` + P2/appointments/awaiting,
`entity_id` = notification UUID; the optional SSE event with the same name uses the appointment
**request** UUID and must not be conflated. Patient `patient_notification`:
`data={notification_id, kind, organization_uuid}` only, fixed event name, `entity_id` =
notification UUID. Both are opaque invalidations; hydration is authorized HTTP. Worker is
required for Ably delivery (`ABLY_API_KEY` gate).

### Separate gaps reported (not fixed)
Patient `in_app` preference stored but never enforced at creation/delivery; staff realtime
enqueue ignores category `inApp`/`browser`; coalesced `queue.ready` skips push;
`COMMUNICATION_DEV_INPROCESS_DISPATCH` has no runtime path (standalone worker required); patient
unread count ignores `expires_at` and `create_patient_notification` never sets it.

### Next step
Frontend-only implementation per `docs/foreground-notifications/01-implementation-plan.md`
(Part A staff, Part B patient), then `02-acceptance-checklist.md` with real browser verification
against persisted notifications delivered through the real outbox + Ably pipeline. Backend
remains untouched under this task.

## FG-CARDS-IMPL — Frontend foreground cards verified against backend (2026-10-06)

### Status
**No backend code changed.** Read-only delivery verification for the frontend foreground-card
task. Handoff `03-implementation.md`, `04-verification.md`, `05-backend-gap.md` and updated
`status.json` copied to `docs/foreground-notifications/` (SHA-256 verified). `HealthOS_API` HEAD
unchanged (`038ac04f724b94ad621e7e5ee2ad0e2272c509c8`); pre-existing dirty work preserved.

### Verified
Real outbox worker (`--once`) + real Ably + synthetic identities exercised the existing staff
`appointment_request.created` and patient `patient_notification` contracts; cards appeared in
both frontends with ~20,000 ms visible timers, authorized HTTP hydration, drawer-open visibility
(staff) and FIFO queue behavior. No backend request/response shape changed.

### Backend gaps (read-only; no fixes)
G-1 patient `in_app` preference not enforced server-side; G-2 staff realtime delivery ignores
category preferences; G-3 coalesced `queue.ready` skips push; G-4 standalone worker required and
the in-process flag is unused; G-5 patient unread count ignores `expires_at`; G-6 patient
realtime payload intentionally omits priority/facility name. Details and proposed narrow
follow-ups in `docs/foreground-notifications/05-backend-gap.md`.

### Next step
No backend action unless G-1/G-2 semantics are re-authorized as a separate task.

## AVAIL-PERF — batch practitioner availability window + lookup indexes (2026-10-06)

### Status
Patient `GET /api/v1/patient/practitioners/{practitioner_uuid}/availability` returned in
17–22 s for a 31-day window (reproduced against the backend, remote Neon DB). Root cause was
`directory.availability` calling `evaluate_availability` once per day, each re-running the
6-query `_context` loader: ~189 sequential round trips. Tables are small (53 appointments,
6 schedules, 174 rules); this was query count/RTT, not missing indexes.

### Changes
`src/app/core/availability.py`: extracted slot building into `_evaluate_context`; added
`preload_availability_range` (one query per dataset for the window), `context_for_day`
(in-memory per-day slicing), and `evaluate_availability_range`; `evaluate_availability`
signature/behavior unchanged. `_protected` uses per-day precomputed windows when present.
`src/app/domains/patient_portal/directory.py`: calls `evaluate_availability_range`; response
shape unchanged. `tests/test_availability_range.py`: parity vs sequential days, date-scoped
schedule/exception/appointment slicing, constant query count (≤7 for any window).

### Indexes
Migration `20261006_47` (applied to configured dev DB, now Alembic head) adds
`ix_care_appointment_practitioner_status_start`,
`ix_care_practitioner_schedule_lookup`, `ix_care_practitioner_availability_exception_lookup`,
`ix_care_practitioner_availability_rule_lookup`; matching `Index(...)` entries added to
`src/app/models/care.py`. Index-only; row counts verified unchanged (53/6/174).

### Checks
`pytest tests/test_availability_range.py tests/test_scheduling_availability.py
tests/test_practitioner_schedules.py tests/test_exception_booking.py tests/test_patient_domain_unit.py
tests/test_appointment_views.py tests/test_patient_authorization.py` → 71 passed. `ruff check` on
changed/new files → only the 6 pre-existing `availability.py` findings (head parity). Live
endpoint 17–22 s → 1.8–2.6 s (40967-byte response). Live DB parity: batched vs sequential
`_sanitize_day` output identical for all 31 days (27 AVAILABLE / 4 CLOSED).

### Next step
Optional: reuse `evaluate_availability_range` in `calendar.py` (days × practitioners) and
`/scheduling/next-slots`; same pattern, not required for the patient endpoint.
