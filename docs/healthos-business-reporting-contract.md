# HealthOS business reporting API contract — Prompts 1 and 3

All paths below are under `/api/v1`. Responses use `{ "success": true, "data": ..., "meta": ... }`. Authentication is required. Dates are `YYYY-MM-DD` in the facility timezone; instants are ISO 8601 UTC. Money is integer minor units, never a float. All lists and aggregates are scoped by organization and facility in SQL. There is no report cache.

## Access

- `administrator`, `organization_admin`, `owner`: facility Reports if their active assignment is for that facility or organization wide; facility Billing reads with the same scope.
- `billing_staff`: Billing reads only for an explicitly assigned facility. This includes invoices, transactions, and the outstanding snapshot. No management Reports or compensation.
- `doctor`, `clinical_practitioner`: `/reports/my-practice`, `/reports/my-visits`, and `/reports/my-earnings` when an active staff assignment covers the facility and an active practitioner belongs to the same organization and user account. `practitioner_uuid`, if supplied, must match that identity. No facility totals or peer data.
- `receptionist` and `facility_operator`: no new reporting or billing-read permission. Existing daily dashboard and other existing routes retain their own authorization.
- Existing `/reports/visits` and `/reports/weekly` retain their administrator gate and prior parameters. The facility assignment is now checked for the requested facility.

## Operational Reports

### `GET /reports/overview`

Required: `facility_uuid`, `date_from`, `date_to`. Maximum 366 inclusive days. Selected metrics are server aggregates over the entire requested range, independent of pagination. `trend` covers the selected range, except a one-day selection covers that day and six preceding days. `previous_period` uses the same duration shifted back, with the current period truncated at server time; it is `null` for a wholly future selection. `as_of_utc` is server time and `selected_cutoff_utc` is the aggregation cutoff.

`data.metrics`: `visits_total`, `completed_visits`, `unique_visited_patients` (distinct patients in completed encounters), `unique_patients_all_visits` (distinct across all encounters), `scheduled_appointments`, `cancelled_appointments`, `no_show_appointments`, `billed_amount`, `payments_received`, `refunds`, `net_collections`, `currency`. Billing metrics use invoice issue, captured payment receipt, and completed refund dates respectively. `data.availability` is `{money, reason, amount_unit:"minor"}`; unavailable money fields are `null`. `data.trend.daily[]` holds `{date, metrics}` including zero days. `data.previous_period` contains comparable `metrics` and `availability` when supported. `data.outstanding` is a separate current ledger snapshot; never subtract the period metrics to derive it.

`data.attention` contains up to five entries. `unpaid_invoice` has `invoice_uuid`, `amount_minor`, `destination:"/billing/invoices"`, and `filters:{outstanding_only:true}`. `billing_review` has `encounter_uuid`, `amount_minor:null`, `destination:"/reports/visits"`, and exact local-day `date_from`/`date_to` filters. These are review candidates, not debt or proven lost revenue. Known zero-fee service candidates are excluded. `data.demo_provenance:"unknown_or_synthetic_demo"` flags that aggregates may contain synthetic historical records; it does not claim all records are synthetic.

Example (abbreviated):
```json
{"success":true,"data":{"timezone":"Asia/Kolkata","as_of_utc":"2026-09-23T10:00:00+00:00","selected_cutoff_utc":"2026-09-23T10:00:00+00:00","selected_range":{"local_from":"2026-09-23","local_to":"2026-09-23","utc_from":"2026-09-22T18:30:00+00:00","utc_to_exclusive":"2026-09-23T18:30:00+00:00"},"metrics":{"visits_total":2,"completed_visits":1,"unique_patients_all_visits":2,"unique_visited_patients":1,"payments_received":30000,"refunds":0,"net_collections":30000,"currency":"INR"},"availability":{"money":true,"reason":null,"amount_unit":"minor"},"trend":{"local_from":"2026-09-17","local_to":"2026-09-23","daily":[]},"previous_period":{"local_from":"2026-09-22","metrics":{"visits_total":1},"availability":{"money":true,"reason":null,"amount_unit":"minor"}},"outstanding":{"as_of_utc":"2026-09-23T10:00:00+00:00","balances":[{"currency":"INR","amount_minor":70000}],"historical_available":false},"attention":[],"demo_provenance":"unknown_or_synthetic_demo"},"meta":{}}
```
The example omits some metric keys and trend days for space; production responses include them.

### `GET /reports/practitioners`

Required: `facility_uuid`, `date_from`, `date_to` (maximum 366 inclusive days). `data.items[]` holds `{practitioner_uuid,name,metrics}`; names come from one scoped lookup. `data.unallocated` contains metrics with no practitioner link. Money uses the invoice's stored practitioner attribution; an unassigned invoice is never split or assigned by guess. `data.range`, `timezone`, and `availability` follow Overview. There is no pagination; one row per practitioner in the bounded range.

### `GET /reports/my-practice`

Required: `facility_uuid`, `date_from`, `date_to` (maximum 366 inclusive days). Optional `practitioner_uuid` may equal only the authenticated active practitioner's UUID. Shape is the Overview period `metrics`, `availability`, `trend`, `previous_period`, `selected_range`, `as_of_utc`, `selected_cutoff_utc`, plus `practitioner:{uuid,name}`, `collections_label:"Collected for my services"`, `compensation_available` (true when any policy is configured for this practitioner/facility), and `demo_provenance`. There is no facility outstanding snapshot or peer breakdown. Collections are **not** earnings.

### `GET /reports/my-visits`

Required: `facility_uuid,date_from,date_to`; optional `practitioner_uuid` (own UUID only), `patient_query`, `page` default 1, `page_size` default 25/max 100. Range maximum 366 inclusive days. `data.items[]` contains `encounter_uuid,visit_started_at,completed_at,status,patient:{uuid,display_name,mrn}` in most recent encounter order. `data.practitioner:{uuid,name}` and `timezone` identify scope. `meta:{page,page_size,total,total_pages}`. Both count and page queries enforce the authenticated practitioner, organization, and facility. No facility-wide totals are returned.

### Existing endpoints

`GET /reports/visits`: required `facility_uuid,date_from,date_to`; optional `practitioner_uuid,patient_query,page,page_size,sort_by,sort_order,include_daily`. Default page 1/size 25, max 100. Sort allowlist: `visit_started_at|completed_at|patient_name|practitioner_name|status`; order `asc|desc`. Response `data.items`, full-filter `data.metrics`, `data.visit_linked`, `data.availability`, optional `data.daily`, and `meta:{page,page_size,total,total_pages}`. `GET /reports/weekly` preserves `facility_uuid,week_of,practitioner_uuid,patient_query` and its full response, including seven `daily` rows, practitioner aggregates, and previous-week comparison. Both continue to use encounter start for visits and transaction dates for finance.

## Billing reads

### `GET /billing/invoices`

Required `facility_uuid`. Optional `date_from,date_to` filter `issued_at`; `patient_query` searches MRN/first/last name; `status=issued|partially_paid|paid|voided`; `outstanding_only`; `sort_by=issued_at|amount_minor|balance_minor|status|patient_name`; `sort_order=asc|desc`; `page` default 1; `page_size` default 25, max 100. Sort and filter happen before pagination. `data.items[]` includes the existing invoice fields `uuid,encounter_uuid,facility_uuid,patient_uuid,practitioner_uuid,consultation_fee_uuid,amount_minor,currency,status,issued_at,paid_minor,refunded_minor,net_collected_minor,balance_minor` plus `patient:{uuid,display_name,mrn}`, `invoice_number:null`, `due_date:null`, and `demo_provenance:"synthetic_demo"|"unknown"`. Balances use captured payments minus completed refunds, clamped at zero. `meta:{page,page_size,total,total_pages}`. No due-date filter, overdue flag, or invoice number exists in the current schema.

`GET /billing/invoices/{invoice_uuid}?facility_uuid=<UUID>` returns the same item in `data` for one invoice, with `meta:{}`. Existing `GET /encounters/{encounter_uuid}/invoice` and issue/payment/void actions remain available under their prior permissions.

### `GET /billing/outstanding`

Required `facility_uuid`. `data:{as_of_utc,timezone,amount_unit:"minor",balances:[{currency,amount_minor}],historical_available:false}`. Sums nonvoid invoices issued by `as_of_utc`, less captured payments received by then, plus completed refunds by then. Includes older unpaid invoices regardless of selected report dates. Empty `balances` means no invoiced currency is present; currencies are never combined. There is no reconstructable historical balance endpoint.

### `GET /billing/transactions`

Required `facility_uuid`. Optional `date_from,date_to` on payment `received_at` and refund `refunded_at`, `patient_query`, `page` default 1, `page_size` default 25/max 100. `data.items[]` has `uuid,invoice_uuid,patient_uuid,amount_minor,currency,occurred_at,kind,reference,method:null,demo_provenance`. `kind` is `payment` or `refund`; refunds carry a negative signed amount. `reference` is the stored idempotency key, not a bank reference. `demo_provenance` is `synthetic_demo` only for the known `demo-encounter-` seed key, otherwise `unknown`. `data.totals[]` has `{currency,kind,amount_minor}` for all filtered rows before pagination. `data.method_available:false`: the current schema stores no method, so method filters and by-method totals are unavailable. `meta` is standard pagination.

## Errors, availability, and migration

- Missing authentication: existing `401`. Missing facility report permission: `403 REPORT_ACCESS_REQUIRED`; missing billing permission: `403 BILLING_ACCESS_REQUIRED`; self role missing: `403 PRACTITIONER_ACCESS_REQUIRED`; another practitioner requested: `403 PRACTITIONER_SCOPE_DENIED`.
- Unknown/inactive facility: `404 FACILITY_NOT_FOUND`; unknown billing invoice: `404 INVOICE_NOT_FOUND`; existing visits/weekly practitioner lookup: `404 PRACTITIONER_NOT_FOUND`.
- Invalid UUID/date/query enum or pagination: FastAPI `422`; reversed range: `422 INVALID_DATE_RANGE`; report range over 366 days: `422 REPORT_RANGE_TOO_LARGE`. Existing weekly future-week error remains.
- Money availability: `MIXED_CURRENCIES` or `PRICE_DATA_UNAVAILABLE` makes report money `null`, distinct from recorded zero. Billing snapshot returns per-currency balances. No compensation policy or payout data exists in Prompt 1.
- Prompt 1 reads required no migration. Prompt 3 migration and data status appear below. Historical demo payments remain synthetic; other old rows remain `unknown`, not verified real.

## Practitioner earnings and recorded payouts — Prompt 3

All paths below are under `/api/v1`; every response retains `{success,data,meta}`. `administrator`, `organization_admin`, and `owner` need an active assignment covering the facility for policy, facility earnings, catch-up, and payout APIs. `doctor` and `clinical_practitioner` can read only their own earnings through the active linked practitioner assignment. `billing_staff`, receptionists, and facility operators cannot read compensation or record payouts. Organization and facility are enforced in every query. Money is signed integer minor units and is never combined across currencies.

### Configured policies

`POST /facilities/{facility_uuid}/compensation-policies` body: `{practitioner_uuid,currency,basis_points,effective_from,effective_to?}`. `basis_points` is 1–10000; dates are facility-local and the start must be today or later. `currency` is a three-letter code. The response `data` is `{uuid,practitioner_uuid,currency,basis_points,effective_from,effective_to}`. `GET /facilities/{facility_uuid}/compensation-policies?practitioner_uuid=<UUID>` returns `data.items[]` of the same shape, ordered by start date. There is no update/delete API. Effective ranges for the same organization, facility, practitioner, and currency cannot overlap. This MVP accepts only percentage of collected consultation receipts; salary, tiers, and arbitrary invoice splits are unsupported.

### Earnings reads

`GET /reports/my-earnings?facility_uuid=&date_from=&date_to=&currency=&practitioner_uuid=` is self-scoped. `GET /billing/practitioner-earnings?facility_uuid=&practitioner_uuid=&date_from=&date_to=&currency=` is admin-only. Dates are facility-local, inclusive, maximum 366 days. `data` contains `practitioner:{uuid,name}`, `currency`, `timezone`, `amount_unit:"minor"`, `as_of_utc`, `period:{utc_from,utc_to_exclusive}`, `availability:{earnings,reason,coverage}`, `earned_in_period_minor`, `signed_adjustments_minor` (currently 0; manual adjustments unsupported), `paid_in_period_minor`, `opening_balance_minor`, `closing_balance_minor`, `current_balance_minor`, `entries[]`, `payouts[]`, `excluded:{count,amount_minor,items[]}`, and `demo_provenance`. Entries include `uuid,source_type,source_uuid,occurred_at,base_minor,basis_points,amount_minor`; payout rows include `uuid,kind,paid_at,amount_minor,method,reference`. Excluded items include `payment_uuid,amount_minor,reason`. `availability.earnings=false` with `reason:"POLICY_NOT_CONFIGURED"` or `"PARTIAL_POLICY_COVERAGE"` makes `earned_in_period_minor:null`; visible ordinary collections remain in the Prompt 1 reports, separate from this statement. `current_balance_minor` covers recorded eligible history through `as_of_utc`, whereas the opening/closing balance is for the selected period. A negative balance after a refund is shown as carry-forward. There is no salary, payroll tax, or inferred historical entitlement.

`GET /billing/practitioner-payouts?facility_uuid=<UUID>` is admin-only and returns `{as_of_utc,amount_unit:"minor",items:[{practitioner_uuid,name,currency,earned_minor,paid_minor,balance_minor}]}` for configured policy scopes. It has no filters or pagination; one row per practitioner/currency with a configured policy. `GET /billing/practitioner-earnings` supplies statement detail and payout history for a selected row.

Example statement (abbreviated):
```json
{"success":true,"data":{"practitioner":{"uuid":"01a0a022-b31b-728f-b7ae-b06211a65893","name":"Dr Example"},"currency":"INR","timezone":"Asia/Kolkata","amount_unit":"minor","as_of_utc":"2026-09-23T10:00:00+00:00","period":{"utc_from":"2026-09-22T18:30:00+00:00","utc_to_exclusive":"2026-09-23T18:30:00+00:00"},"availability":{"earnings":true,"reason":null,"coverage":"recorded eligible consultation receipts only"},"earned_in_period_minor":34,"signed_adjustments_minor":0,"paid_in_period_minor":0,"opening_balance_minor":0,"closing_balance_minor":34,"current_balance_minor":34,"entries":[],"payouts":[],"excluded":{"count":0,"amount_minor":0,"items":[]},"demo_provenance":"historical_unknown_and_synthetic_excluded"},"meta":{}}
```
The example omits its earning entry for brevity; production includes the entry in `entries`.

### Catch-up and mutations

`POST /billing/practitioner-earnings/catch-up?facility_uuid=&practitioner_uuid=&currency=` replays only captured payments marked `recorded` for the requested scope, then their completed refunds. Response `data:{created,excluded:{reason:count}}`. Source uniqueness makes repeat calls idempotent. It does not backfill unknown or synthetic historical receipts and does not guess missing policies. Read endpoints never generate entries.

`POST /billing/practitioner-payouts?facility_uuid=<UUID>` body: `{practitioner_uuid,currency,amount_minor,paid_at,method,reference,idempotency_key}`. The amount is positive; `paid_at` includes an offset and cannot be future. This records an external payment; no bank transfer occurs. The practitioner row is locked while current and paid-at balances are checked, then settlement allocations, payout entry, and audit event commit together. Exact retries return `meta.idempotent:true`. Success: `data:{uuid,amount_minor,currency,paid_at,balance_minor}`. `POST /billing/practitioner-payouts/{payout_uuid}/reverse` body `{reason,idempotency_key}` writes a negative reversal entry and audit event; it never edits/deletes the original. Success `data:{uuid,reverses_uuid,amount_minor}`. Exact retries return `meta.idempotent:true`.

Example payout request and response:
```json
{"practitioner_uuid":"01a0a022-b31b-728f-b7ae-b06211a65893","currency":"INR","amount_minor":34,"paid_at":"2026-09-23T10:00:00+00:00","method":"bank_transfer","reference":"EXT-123","idempotency_key":"payout-20260923-001"}
```
```json
{"success":true,"data":{"uuid":"01a0cc68-0ad8-7580-bd96-01db22db6250","amount_minor":34,"currency":"INR","paid_at":"2026-09-23T10:00:00+00:00","balance_minor":0},"meta":{"idempotent":false}}
```

### Eligibility, errors, and migration status

- Earning entries are immutable snapshots of the source event, original policy UUID/rate, consultation base, and rounded entitlement. Only a completed encounter with one matching practitioner, linked consultation fee, matching invoice amount/currency, and `recorded` receipt provenance qualifies. Matching the fee snapshot excludes tax or mixed lines because this schema has no line allocations, discount, or tax fields. Unsupported/ambiguous invoices appear in `excluded`; they never become payout balance. Receipt entitlement uses nearest minor unit, ties upward. Partial refunds reverse proportional original entitlement using cumulative rounding; full refunds reverse the exact remainder. Payment/refund voids create opposite signed entries.
- New API receipts get `recorded` provenance; `demo-encounter-` keys remain synthetic. Migration labels only exact known demo seed keys; all other preexisting receipts stay `unknown`. Unknown/synthetic receipts are excluded from payable earnings, including in live environments. Ordinary collection reporting remains unchanged.
- Existing auth errors are `401`; missing admin or self access is `403 REPORT_ACCESS_REQUIRED` or `403 PRACTITIONER_ACCESS_REQUIRED`; another practitioner requested is `403 PRACTITIONER_SCOPE_DENIED`; unknown practitioner/payout is `404 PRACTITIONER_NOT_FOUND`/`PAYOUT_NOT_FOUND`. Policy overlap is `409 POLICY_OVERLAP`; absent or wrong-currency policy is `409 COMPENSATION_NOT_CONFIGURED`/`CURRENCY_MISMATCH`; payout over balance or unavailable allocation is `409 PAYOUT_EXCEEDS_BALANCE`/`PAYOUT_ALLOCATION_UNAVAILABLE`; duplicate key with changed request is `409 IDEMPOTENCY_KEY_REUSED`; second distinct reversal is `409 PAYOUT_ALREADY_REVERSED`. Invalid dates/body/currency/UUID return `422`; future payout returns `422 PAYOUT_IN_FUTURE`. Rejected writes commit nothing.
- Migration `20260923_34` adds provenance and the policy, earning, payout, and allocation tables, constraints and scoped indexes. It is **pending**; no shared or production database migration was executed. Apply after choosing an authorized local/dev database: from `src/`, `../venv/bin/alembic upgrade head`. Existing row counts are unchanged. Before/after classification to verify on the selected database: `SELECT provenance, count(*) FROM care.payment GROUP BY provenance`; payable earnings and payout rows start at zero. Old unknown payments need a trusted source/mapping before any separate eligibility backfill. No unknown amount is silently treated as zero earned.
