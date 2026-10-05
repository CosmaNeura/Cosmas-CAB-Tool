# Flight plan: activate email-only Card Chase for a single-practice Beta

Prepared as Beta-0 preparation for the Card Chase (F9) workstream. It defines
the founder-only manual activation path from today's dark/`record_only` state to
an email-only, per-policy-approval outreach Beta for one practice, and records a
durable-topology rehearsal performed against a disposable PostgreSQL 16. It does
**not** authorize activation; it makes the activation reviewable and reversible.

## Outcome and boundaries

- **Problem and measurable outcome:** MCD holds 300–500 claims monthly on
  missing insurance cards (billers, 8/6). Activate the built F9 outreach so an
  approved email cadence chases missing/expired/eligibility cases for one
  practice, measured by card-submission completion rate and staff-minutes-per-card
  against a captured baseline.
- **In scope:** the founder-only manual activation cascade for email-only
  outreach; the ordered policy create → approve → activate lifecycle; the
  scheduled `chase.worker --once` job; go/no-go gates; rollback; and the
  rehearsal that proves the durable topology.
- **Explicit non-goals:** SMS and outbound voice (no transport implemented; no
  authoritative TCPA-consent feed — phone stays a staff task); card image
  capture/OCR and any Athena/EHR writeback (F9c ends in a human-reviewed manual
  handoff and lives in the separate CardCapture/athenaNet repo); real-PHI beyond
  the executed agreement gates; any auto-capture or auto-write; changing coding
  adjudication.
- **Affected roles, practices, services, environments, data classes:** `Admin`
  (policy lifecycle), `BillingStaff`/`Coder` (worklist projection, staff
  actions); one pilot practice; the API and the scheduled chase-worker Container
  Apps job; staging then a single production cohort; patient contact metadata
  (destinations remain PHI, held only in `chase_contact_routes`, never in the
  worklist projection or outbox JSON).

## Risk and dependencies

- **Tier and reason:** **Tier 2** — patient outreach activation and messaging
  transport. Urgency never lowers this tier.
- **High-blast-radius decisions:** every message is gated behind an
  Admin-approved, activated policy bound to an exact fingerprint; the
  direct-contract hard gate fails closed on match or unknown; any ambiguous
  provider submission halts new outreach; no card data ever enters a message;
  the worklist projection omits patient references, destinations, and assignees.
- **Dependency-ordered prerequisites (each can be held safely):**
  1. Track P — staging rebuild (Container Apps + Postgres + Entra) and Auth/IAM
     memberships. Holding it leaves outreach dark; nothing sends.
  2. `state_store=postgres` **and** `chase_store=postgres` on the same
     `DATABASE_URL`; migrations `002`–`006` applied; worker role provisioned.
  3. ACS email operational approval + the applicable Microsoft/ACS agreement,
     and P5 log-scan coverage. Holding it keeps `record_only`, which still
     advances cadence and creates staff phone tasks but contacts no one.
  4. Founder authorization of the three latches. Holding it keeps the safe dark
     default.
- **Cross-repo contract/version and receiver-before-sender order:** N/A for
  transmission — F9c completion is a human-reviewed handoff, not a wire contract
  to CardCapture/athenaNet. This plan sends no data to another repo/service
  beyond ACS email.

## Current deployed state

- **Live configuration:** all outreach latches default false; transport
  `record_only`; the API is transport-inert (no ACS connection string on the
  API). Current `main` is not deployed (per the deployment-containment plan);
  the live digest is unreconciled.
- **Difference from `main` and deployed-only behavior:** none introduced here —
  this plan adds documentation and a rehearsal record only; it changes no
  runtime code and flips no latch.
- **Reconciliation decision:** retain the live deployment unchanged; assimilate
  this activation only through reviewed `main` and a separate founder-run.
- **Previous known-good artifact and data-recovery boundary:** the current live
  digest remains the recovery artifact; this plan creates no persistent data.

## Release control (reversible user-visible behavior only)

- **Control/latch, owner, expiry, cohorts, activation authority:** the reversible
  control is the trio `COSMANEURA_CHASE_SCHEDULER_ENABLED`,
  `COSMANEURA_PATIENT_MESSAGING_ENABLED`,
  `COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED` plus
  `COSMANEURA_CHASE_TRANSPORT`, and — at the data plane — the Admin-activated
  policy pointer. Owner: founder. First cohort: one practice. Activation
  authority: founder-only manual, bound to an independent-agent review record.
- **Safe/dark default and removal condition:** the safe state is all latches
  false and `record_only`; `POST /api/chase/policy/revoke` removes the active
  policy and cancels only queued work that has provably not called the provider.
- **N/A reason:** not applicable — this is genuine reversible user-visible
  behavior with a latch-based control and a revoke path.

## Schema/data sequence

| Change | Rung | PR | Invariant | Recovery |
|---|---|---|---|---|
| Chase persistence (`chase_items`, attempts, tasks, resolutions) | already in tree | migration `002` | commits atomically per page | revoke policy; forward-only |
| Delivery runtime (contact routes, policy revisions, approvals, releases, cadence, outbox, delivery events, forced RLS, immutability triggers) | already in tree | migration `004` | destinations only in `chase_contact_routes`; SMS fail-closed | revoke; drain worker |
| Accepted-attempt function | already in tree | migration `005` | worker records accepted attempts only | forward-only |
| Policy revocation cleanup + partial unique indexes on live outbox | already in tree | migration `006` | revocation cancels only un-dispatched work | forward-only |

- **Rehearsal runbook/evidence and PHI-boundary status:** rehearsed against a
  disposable PostgreSQL 16 in a dev VM (synthetic only; no PHI). See "Rehearsal
  evidence" below. Migrations are forward-only during normal rollback; checked-in
  down migrations are for a disposable database only.

## Rollout and promotion

- **Ordered environments/cohorts and hold points:**
  1. `record_only` durable rehearsal (**done**, synthetic) — hold for review.
  2. Staging synthetic ACS email acceptance once ACS is approved — hold.
  3. Single-practice production email cohort under founder activation — hold.
- **Health signals and measurable abort thresholds:** worker exit code must be
  `0`; any `ambiguous` delivery halts new outreach (worker exits non-zero) and
  is investigated, never blindly retried; direct-contract review queue must be
  drained by a human, not bypassed; delivery-event counts must reconcile with
  reserved outbox rows. Abort on any non-zero worker exit that is not a clean
  drain, any unexpected outbound attempt in `record_only`, or any worklist
  response carrying a patient reference.
- **Canary journey (anti-inflation/evidence/role-isolation):** seed the synthetic
  four-case pack (Admin), run the worker in `record_only`, confirm a durable
  staff phone task is created and no message is sent; confirm a direct-contract
  match is withheld for human review; confirm `BillingStaff` sees only the Card
  Chase tab and `Provider` cannot reach chase routes (403); confirm the worklist
  projection contains no destination, assignee, or patient reference.
- **Founder-only manual promotion record / control gap:** production activation
  is a founder-only manual run bound to the exact merged `main` commit and its
  independent-agent review record. Open gap: promotion still rebuilds the image
  and Azure identity/OIDC is not yet wired (see PR #35 and the
  deployment-containment plan) — resolve before a real cohort.

## Security and recovery

- **Open security acceptances:** synthetic/de-identified only remains the
  ceiling; this plan grants no real-PHI acceptance.
- **Required BAA/service/data-handling approvals:** the applicable Microsoft/ACS
  agreement covering patient email; P5 logging/redaction acceptance; deployment
  acceptance (401 on protected routes, forged principal rejected, HTTPS/HSTS).
- **Recovery move:** set every switch to the safe-state values, revoke the active
  policy, wait for any running worker execution to finish (never retry an
  ambiguous delivery), and redeploy the last accepted immutable digest. No schema
  repair is required; migrations are forward-only.
- **Observation window before irreversible cleanup:** N/A — no irreversible
  cleanup; disposable rehearsal database is torn down separately.

## Activation cascade (exact configuration)

All of the following must hold for a single real email to be sent; each is
enforced in `settings.py` / `worker_config.py` and fails startup or dispatch
otherwise:

| Setting | Beta value | Notes |
|---|---|---|
| `COSMANEURA_ENV` | `staging` \| `production` | messaging enable rejected in dev |
| `COSMANEURA_AUTH_MODE` | `container_apps` | Entra easy-auth; dev bypass rejected |
| `COSMANEURA_STATE_STORE` | `postgres` | same `DATABASE_URL` as chase |
| `COSMANEURA_CHASE_STORE` | `postgres` | in-memory rejected in prod |
| `COSMANEURA_CHASE_SCHEDULER_ENABLED` | `true` | latch 1 |
| `COSMANEURA_PATIENT_MESSAGING_ENABLED` | `true` | latch 2 |
| `COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED` | `true` | latch 3 (explicit ops decision) |
| `COSMANEURA_CHASE_TRANSPORT` | `acs_email` | only non-`record_only` value |
| `COSMANEURA_CHASE_APPROVAL` | `per_policy` | Beta scope; `per_message` optional |
| `COSMANEURA_CHASE_GATE_HMAC_KEY` | ≥ 32 bytes | required even in `record_only` when scheduler on |
| `COSMANEURA_ACS_EMAIL_ENDPOINT` / `_SENDER_ADDRESS` / `_REPLY_TO_ADDRESS` | set | API + worker fingerprint metadata |
| `COSMANEURA_ACS_EMAIL_CONNECTION_STRING` | set | **worker job only**; forbidden on the API |
| `COSMANEURA_UPLOAD_URL` | real HTTPS | patient-facing secure upload link |
| `COSMANEURA_CHASE_WORKER_DB_ROLE` / `_PASSWORD` / `_DATABASE_URL` | set | least-privilege worker DSN, never on API |
| `COSMANEURA_DIRECT_CONTRACT_LIST` | populated | maintained roster; unknown fails closed |

Then, at the data plane (Admin): `POST /api/chase/policy/create` →
`/approve` → `/activate` (exactly one active policy, bound to the immutable
fingerprint), and schedule the Container Apps job running
`python -m cosmaneura_billing.chase.worker --once`.

## Rehearsal evidence (this VM, synthetic, disposable PostgreSQL 16)

Executed with `COSMANEURA_TEST_DATABASE_URL` pointed at a disposable
`cosmaneura_test` database. All green:

- Durable topology (82 passed): `test_chase_postgres`,
  `test_chase_scheduler_postgres`, `test_p4_worker_role_postgres`,
  `test_p4_migration_safety`, `test_p4_revocation_migration`,
  `test_pg16_role_membership_postgres`, `test_persistence_postgres`,
  `test_source_import_postgres`, `test_synthetic_chase_demo`,
  `test_synthetic_demo_immutability_postgres`.
- Chase logic and isolation (281 passed): `test_chase`,
  `test_chase_scheduling`, `test_chase_worker`, `test_chase_worker_config`,
  `test_p4_worker_settings`, `test_direct_contract`, `test_acs_email`,
  `test_policy_fingerprint`, `test_chase_api`, `test_chase_store`,
  `test_p4_api_integration`.

This exercises migrations `002`–`006`, the least-privilege worker-role grants
and isolation, policy fingerprint/approval/activation/revocation, the
direct-contract fail-closed gate, the `record_only` vs `acs_email` transport
split, and the worker's reserve/quarantine/ambiguous handling.

## Open decisions this plan does not resolve (must be answered before activation)

- Card-chase cadence, tone, and email templates (content) — `HOLD` (synthesis §6).
- Direct-contract list **source** (Q5) — currently a versioned local file,
  fail-closed; a maintained source must be chosen.
- Practice baseline numbers (Q2/Q9) — the month-end window was missed; re-ask
  before claiming a completion-rate improvement.
- ACS/BAA operational approval and P5/deployment acceptance — external gates.

## Completion

- **Evidence to call it complete:** the cascade above satisfied in staging; a
  synthetic ACS email acceptance run; the persona matrix and PHI-safe log scan
  green; the direct-contract review queue exercised; independent non-implementer
  agent review of the final commit.
- **Follow-up cleanup PRs and owners:** worker exit-code / ambiguous-delivery
  monitoring (Track B3); direct-contract list source adapter (Track B2, Tier 2);
  baseline capture surface (Track B5).
- **Records to update:** deployment source-of-truth map and post-activation
  evidence record; the pilot operations runbook owner table.
