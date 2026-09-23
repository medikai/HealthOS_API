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
