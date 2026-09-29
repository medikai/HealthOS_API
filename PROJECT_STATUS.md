# HealthOS API — project status

Snapshot: 2026-09-26, `feature/notifications` at `001df40`. This is a backend repository status, based on committed code, Git history, and the linked project notes. It does not certify a production rollout. `main` is at `7ab0a7c`; the three communication commits are on the feature branch.

## Achieved so far

- **Platform and access:** FastAPI/PostgreSQL base, Logto and local authentication flows, organization/facility scoping, staff roles and invitations. [API inventory](docs/API_STATUS.md).
- **Care operations:** patient records and history, practitioner/facility schedules, availability and exception booking, appointments and rescheduling, queue/walk-ins, encounter SOAP, vitals, diagnoses and prescriptions. [API inventory](docs/API_STATUS.md) · [upgrade notes](docs/healthos-upgrade-progress.md).
- **Masters and geography:** specialties, staff designations, medicine/prescription options and location masters; India geography import has a reviewed plan and manual apply path. [ETL notes](docs/india-geography-etl.md).
- **Billing and reports:** consultation fee snapshots, invoices, payments/refunds, visit and weekly reports, operational and billing reads, practitioner earning policies and recorded payouts. Historical demo billing is explicitly synthetic. [Progress](docs/healthos-upgrade-progress.md) · [API contract](docs/healthos-business-reporting-contract.md).
- **Communication on this branch:** durable notifications, chat, email, push, Ably realtime, worker delivery and local password recovery; subsequent commits repair realtime delivery and add notification changes. [Local communication status](docs/communication-delivery/STATUS.backend.md) (this directory is Git-ignored and may be absent in another clone).

## Version and commit milestones

| Version / state | Git record | Main additions |
|---|---|---|
| Foundation | `b267ac3` (2026-05-06) → `981f24a` (2026-09-14) | API base, identity/organization work, Logto, staff invitations and clinical groundwork. |
| `v1.0.0-alpha.1` | `1f3f578` (2026-09-21) | Booking, report uploads, masters, and scheduling fixes through the third slots PR. |
| After 1.0, before 1.1 | `edfab8c`, `40fc531`, `b868ced`, `9cd1723` (2026-09-22–23) | Availability/live events, new masters, patient clinical history, weekly reporting and consultation billing. |
| `v.1.1.0-alpha.1` | `c9e558b` (2026-09-23) | Business reporting reads, practitioner earnings/payouts, and billing backfill tools via reporting PRs. |
| Main after 1.1 | `9127de6` → `7ab0a7c` (2026-09-23) | State/city flow and India geography ETL improvements, merged to `main`. |
| Untagged feature branch | `ec86782` → `74d91d8` → `001df40` (2026-09-25–26) | Communication foundation, incoming chat realtime repair, notification refinements. Not yet on `main`. |

## Open checks / next work

- **Database rollout:** older progress notes mark billing/earnings migrations pending; verify the target database revision before claiming deployment. India geography full import was planned but not applied in the documented check. [Migration rules](AGENTS.md) · [ETL notes](docs/india-geography-etl.md).
- **Communication rollout:** keep the API and durable worker running together; browser updates, FCM display, email inbox delivery, and isolated database checks still need their own verification. The local communication status files describe later checks but are Git-ignored, so they are not release evidence in a fresh clone.
- **Frontend:** this repo does not establish frontend completion. The reporting and communication contracts are handoffs, and the old [API inventory](docs/API_STATUS.md) predates the latest reporting/communication work; avoid treating its endpoint count as current.

Update this file at the next release or major milestone with the actual tag/commit, completed scope, and outstanding checks. Verify deployment and migration state separately from Git history.
