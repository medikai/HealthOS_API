# Backend — Ably realtime: write-up + prompt

Run in: `HealthOS_API` (do this repo **first**; staff S2 and patient P1–P5 depend on it).

## Write-up

Goal: publish realtime events for both directions using the Ably stack that already exists here.

Starting state (verified):
- Ably provider signs TokenRequests and publishes: `src/app/domains/communication/realtime/providers/ably.py:45-107`.
- Staff config/token routes exist (`/api/v1/communication/realtime/config|token`); channel builders are
  staff-only: `src/app/domains/communication/utils/channels.py:19-49`.
- Durable outbox worker has a realtime provider: `.../delivery/worker.py:38-165`,
  `.../realtime/providers/notify.py:31-175`.
- Patient notifications are written inside the workflow transaction
  (`src/app/domains/patient_portal/notifications.py:27-79`; callers in
  `patient_portal/appointments.py:282,510,560,625,748`, `record_releases.py:224`, `billing.py:1037`)
  but are never delivered over realtime.
- `create_new_request` (`patient_portal/appointments.py:297-367`) notifies nobody.

Deliver B1–B5 exactly as specified in `docs/patient-app/handoffs/09-ably-realtime-plan.md`:
1. Patient channel/client-id builders + `PatientRealtimeChannelState` generation table (migration).
2. `GET /patient/realtime/config` and `POST /patient/realtime/token`: patient cookie only, CSRF on
   POST, subscribe-only capabilities, key material never leaves the server.
3. Patient recipient in the outbox and worker delivery; enqueue from `create_patient_notification`
   in the same transaction (the helper must not commit).
4. `appointment_request.created` in `EVENT_CATALOG` + `record_notification` in
   `create_new_request` (+ optional `core.events.publish` SSE invalidation for staff pages).
5. `ABLY_*` documented in `.env.example`; bump the patient generation on logout and phone change.

Guardrails: never expose `ABLY_API_KEY`; opaque events (UUIDs/kinds only, HTTP refetch for details);
apply the migration to local/dev only; follow `AGENTS.md` + `docs/healthos-agent-rules.md`; read then
append `docs/healthos-upgrade-progress.md`; no real secrets in examples.

Done when: targeted pytest passes, a two-identity live check shows patient→staff and staff→patient
delivery, the progress entry is appended, and the handoff is copied to the patient/staff docs dirs
with SHA-256.

## Prompt

Read `ABLY_REALTIME_PROMPT.md` and `docs/patient-app/handoffs/09-ably-realtime-plan.md`, then
implement the backend slice (B1–B5) in this repository.

Follow `AGENTS.md` and `docs/healthos-agent-rules.md`. Read `docs/healthos-upgrade-progress.md`
before starting and append the completed stage after. Work in small vertical changes; apply any
migration to a clearly identified local/dev database only and never to production. Never expose
`ABLY_API_KEY` to a browser.

Verify with targeted pytest plus a two-identity live check (patient books → staff bell/list updates;
staff approves → patient notification arrives). Update the progress doc and copy the handoff to the
patient and staff doc directories with SHA-256. Stop and report if a required fact or credential is
missing.
