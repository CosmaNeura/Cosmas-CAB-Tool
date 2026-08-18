# CosmaNeura — Agentic Billing Layer

A multi-agent pipeline that turns a finished visit (intake text + transcript +
signed note) into accurate, fully-auditable billing codes, with two role-scoped
outputs and a feedback loop back into the rest of the platform. It is built on
top of Homen's **FluencyMed** prototype and consumes that prototype's existing
outputs directly.

## Run it

No API key and no network needed — the demo runs on a deterministic offline
model and an authentic (public-domain) ICD-10 subset:

```bash
pip install pandas pyarrow          # pyarrow only needed for the full code set
python run_demo.py                  # prints the provider view + biller worksheet + feedback
python prove_no_upcode.py           # proves the guardrail cannot upcode
```

## The pilot demo (API + web UI)

Two terminals:

```bash
# 1. the engine API (FastAPI; first request loads the NCCI edits, ~18s)
pip install fastapi uvicorn
uvicorn cosmaneura_billing.api:app --port 8000

# 2. the web UI (provider card, biller worksheet, coder review)
cd frontend && npm install && npm run dev     # -> http://localhost:5173
```

The UI shows a live-engine status pill (NCCI / coverage / CPT), lets you pick
an encounter (the GI-bleed demo plus ACI-Bench visits), edit the documentation,
run the pipeline, and — on the Coder review tab — accept/edit/reject each code
and submit feedback. Local development uses `coder_feedback.jsonl` (override
with `COSMANEURA_FEEDBACK_PATH`), which can be reviewed with
`scripts/analyze_feedback.py`; staging and production persist normalized
feedback in PostgreSQL and expose aggregate analysis through the role-scoped
API. Each draft carries a stable client-generated `review_id`, making an exact
retry idempotent while a different payload under the same ID fails with 409.

## Container and Azure deployment

The production image builds the React frontend and Python API together, installs
the PostgreSQL adapter, verifies the bundled NCCI and coverage datasets from the
installed wheel, runs as a non-root user, and serves one origin. For a synthetic
local deployment:

```bash
cp .env.example .env
# Set distinct long owner/runtime passwords and their RFC 3986 percent-encoded
# *_URL_PASSWORD forms, plus a synthetic practice ID, tenant UUID, and reviewed
# synthetic bootstrap-admin UUID. Compose remains an auth-protected smoke stack.
docker compose up --build --wait
```

The localhost bind is intentional. In the default production-mode Compose
configuration, protected APIs require a trusted Container Apps principal and
ordinary requests therefore return 401. Compose has no auth sidecar of its own,
so it is a packaging smoke topology, not a secure user-facing deployment; never
expose its port beyond localhost. Use the explicit development bypass only with
synthetic data. Compose applies all current migrations to a fresh
PostgreSQL 16/pgvector volume, and `/api/ready` stays unavailable until the
engine, regulatory datasets, database, encounters, explicit memberships,
feedback, chase schema, and access-audit store are ready. The migration
container uses the database-owner credential; the API receives only a separate
least-privilege runtime credential. The scheduled chase worker has a third,
distinct practice-bound database credential; its database and messaging secrets
must never be injected into the API container. The default chase transport
remains inert.

Azure Bicep templates and the reviewed deployment sequence are in
`../infra/README.md`. They create a VNet-injected Container Apps environment,
private PostgreSQL, private ACR and Key Vault endpoints, managed identity,
Log Analytics, App Insights, and a one-shot migration job. Application ingress
defaults private and must remain private until P2 authentication is verified.
No Azure resources are created by tests or by building the repository.
The Entra trust boundary, role matrix, request controls, audit event contract,
and local-development exception are in `docs/ACCESS_CONTROL.md`.

Staging and production are PostgreSQL-only: set both
`COSMANEURA_STATE_STORE=postgres` and `COSMANEURA_CHASE_STORE=postgres`, and use
the same runtime `DATABASE_URL` for encounter, feedback, audit, membership, and
chase state. JSONL and in-memory stores are rejected there. The migration job
bootstraps only the explicitly named initial Entra administrator; all later
membership changes use the reviewed owner-only CLI documented in
`migrations/README.md`. Login never creates a user or membership.

**Offline mode covers one encounter.** Without `ANTHROPIC_API_KEY` the engine
runs a deterministic model that only knows the bundled GI-bleed demo visit. On
any other encounter it extracts nothing and proposes nothing — correctly, and
by design. Every response says which inference path ran (`inferenceMode`,
`inferenceModel`) and, when a run produced nothing for that reason, carries a
`degraded` object explaining it, so an empty claim is never presented as a
finding.

## Provider billing tab and note completeness

```
GET  /api/encounters/{id}/billing-tab   the provider-facing billing view (F1)
POST /api/completeness                  run a note rule pack pre-submission (F2)
```

The **billing tab** reshapes one pipeline result into a billing reference over
the same visit: E/M with its MDM facts and pathway, ICD and CPT candidates each
carrying verbatim evidence spans, plan items linked to the code that justifies
them, multi-mapped findings deduped with alternates listed, coverage-aware
ordering, and per-code plus copy-all clipboard text. The clinical note is never
altered — it is a view over it (`clinical_note_modified` is always `false`).

The **completeness checker** evaluates a note against a deterministic rule pack
before the claim reaches a coder. `telehealth.v1` covers the omissions behind
real Optum/UHC denials: modality, duration, patient location, provider location,
consent. Each result is `present` / `missing` / `ambiguous` with the span it
matched and a plain-language fix hint. Providers can override any rule, but an
override requires a reason (a blank reason is a 422) and is recorded. The
ready-to-insert attestation header renders only once every rule passes or is
overridden — attesting over a known gap would assert something untrue.

```bash
curl -s localhost:8000/api/completeness -H 'content-type: application/json' -d '{
  "note": "Telehealth visit. Patient consented.", "placeOfService": "10"}' | jq
```

Production inference (clinical fact extraction via Claude) turns on automatically
when a key is present, and the full AHRQ code set loads from a path you point at:

```bash
ANTHROPIC_API_KEY=sk-...   python run_demo.py
ICD10_CCSR_CSV=/path/to/DXCCSR_v2023-1.csv   python run_demo.py
```

## Official CPT descriptors (AMA developer account)

Out of the box the engine shows CPT/E&M codes with short generic labels,
because the official descriptors are AMA-licensed. If you hold a CPT license
with API access (AMA Intelligent Platform / CPT Developer Program), the engine
auto-fetches the CPT Standard Data File at startup and swaps in the official
short descriptors. Set the four values from your Developer Portal application
(Keys tab) before starting the API:

```bash
export AMA_TOKEN_URL='https://<gateway-host>/oauth2/token'
export AMA_BASE_URL='https://<gateway-host>'
export AMA_CONSUMER_KEY='...'          # Application Consumer Key
export AMA_CONSUMER_SECRET='...'       # Application Consumer Secret

# optional: validate credentials + subscription first (2 API calls, no download)
python scripts/fetch_cpt_zip.py --check

uvicorn cosmaneura_billing.api:app --port 8000
```

`GET /api/health` then reports the loaded descriptor count in `cptDescriptors`
and a human-readable `cptStatus` (which also explains any fetch failure); the
UI status pill shows `CPT ✓` with the detail on hover. The download is cached
under `~/.cache/cosmaneura/cpt`, so the fetch runs once, not on every boot —
which matters because AMA production credentials are capped at ~10 API calls
per day (staging/sample credentials are uncapped; the endpoint defaults were
verified against the production gateway, CPT 2026 Standard Update 4, 10,988
descriptors). Alternatively, point `CPT_DATA_DIR` at an already-extracted
Standard Data File directory, or set `CPT_AUTO_FETCH=0` to disable fetching.

Never commit descriptor files, the downloaded ZIP, or the AMA key/secret —
CPT content stays under your license, outside the repository.

## Insurance-data chase (F9)

The billers' #1 stated pain: 300–500 claims a month held for missing insurance
information. The validated finding is that the bottleneck is **patient
submission, not OCR** — card capture already works. What was missing is the
system that gets the card *to* capture: a master worklist, worked across ACS
email when enabled and manual phone tasks, by the staff whose whole job this is
(not the coders). SMS and portal delivery are not implemented in this slice.

```bash
python demo_chase.py    # end-to-end, synthetic patients, nothing leaves the building
```

```
GET  /api/chase/worklist            the master list (filter by bucket/assignee/needs_person)
POST /api/chase/detect              run detection over coverage snapshots
GET  /api/chase/metrics             outstanding, touches/card, minutes/card, time-to-card
POST /api/chase/{id}/action         release | suppress | assign | log_attempt | submitted | verify
```

Each staff action carries a client-generated UUID `action_id`. An exact retry
is replayed without adding another contact or minute count; reuse with a
different action payload returns 409.

The demo and tests default to an in-memory worklist. Production fails closed
unless the worklist is backed by PostgreSQL, so replicas cannot diverge and a
restart cannot discard contact history:

```bash
# First run the owner-only migration job documented in migrations/README.md.
export COSMANEURA_ENV=production
export COSMANEURA_STATE_STORE=postgres
export COSMANEURA_CHASE_STORE=postgres
export COSMANEURA_DB_RUNTIME_ROLE=cosmaneura_app
export COSMANEURA_PRACTICE_ID=<stable-practice-id>
# DATABASE_URL is the TLS runtime-role DSN, never the migration-owner DSN.
```

For the pilot, chase and platform state must use the same `DATABASE_URL`; a
separate chase DSN is rejected because it would split a mutation from its audit
completion. The store scopes every primary key, foreign key, query, and
transaction by the configured practice, with forced database row-level
security and a runtime-role-to-practice binding. Changing the session practice
setting cannot expose another practice. The default transport remains inert;
durable storage alone does not authorize patient contact.

### Durable scheduler and one-shot worker (P4)

Migrations `004`–`006` add immutable policy revisions and approval events,
contact snapshots, exact human releases, durable cadence/outbox state, delivery
events, and narrow database functions for accepted-delivery logging and safe
revocation cleanup. Policy approval, activation, revocation, direct-contract
review release, and per-message release are Admin-only actions. Every release
is bound to the exact policy, cadence step, contact snapshot, or gate evidence
it reviewed; changed evidence requires a new human decision. Revoking a policy
cancels only queued work that has not reached the provider and does not turn an
in-flight or ambiguous submission into a retry.

Run one bounded scheduler pass as the dedicated worker, never with the API's
`DATABASE_URL`:

```bash
python -m cosmaneura_billing.chase.worker --once
```

The worker has no daemon mode or internal retry loop. An expired or otherwise
uncertain provider submission becomes `ambiguous`; any unresolved ambiguity
causes the invocation to stop with a non-zero result before scheduling or
sending new outreach. The default `record_only` transport never contacts a
patient, but it still advances the configured cadence far enough to create a
durable manual phone task. A staff member makes that call and records the time;
there is no outbound voice implementation. SMS is not implemented in this
slice.

ACS email is available only when the durable scheduler, the patient-messaging
enable latch, and the separate operational-activation latch are all on, the
transport is `acs_email`, and the exact configuration has human approval.
Staging and production also require a chase-gate HMAC key of at least 32 bytes,
including for `record_only`, so gate evidence cannot silently drift. Keep both
messaging latches off and use only synthetic/de-identified recipients: real
patient messaging remains blocked until the deployment layer, BAA, P5
logging/redaction, and deployment acceptance are complete.

One list, three views — missing, expired, eligibility — matching the buckets
staff already use. Priority comes from appointment proximity: today's visit
outranks next month's, and a visit that already happened outranks both, because
the claim is already being held.

**Automated, with a human behind it.** A named Admin approves the exact *policy*
once; the system then works the list unattended and escalates what it cannot
handle (`COSMANEURA_CHASE_APPROVAL=per_message` requires an exact release for
every touch). Cadence, channel order, templates, quiet hours and max touches are
all configuration — they are the open questions the staff interview will
settle, so they are values, not code.

What the code guarantees, and tests:

- Nothing is sent under a policy no named human approved.
- A direct-contract patient — or one whose status is unknown — never enters an
  automated sequence. The row goes to a person; it never silently disappears.
- No outbound message body contains insurance data. There is no template
  placeholder that could carry a member id, and the check runs at configuration
  time as well as send time.
- The current automated transport is ACS email. SMS is not implemented; if it
  is added later it remains fail-closed without an authoritative opt-in feed.
- **Phone is a route- and consent-checked staff task, never an automated call.**
  The system schedules it, a person makes it and logs the outcome and minutes
  into the same contact history — which is what makes phone work visible in the
  metrics. There is no outbound voice transport.
- A row closes only on a **verified** card plus its write, never on a patient's
  submission.

### The direct-contract gate (F3)

```python
from cosmaneura_billing.direct_contract import guard_patient_charge, chase_routing
```

Grace's ask: never charge a direct-contract patient a self-pay fee. Two callers,
two policies — charging **hard-stops** (raises), chasing **routes to review**.
Both fail closed: a missing or unreadable list makes every answer "unknown", and
unknown blocks. Point it at the practice's list with
`COSMANEURA_DIRECT_CONTRACT_LIST` (CSV with a `patient_ref` column, or JSON).

## What's in the box

```
cosmaneura_billing/
  schemas.py                 dataclasses; the "evidence-or-it-isn't-billable" rule lives here
  llm.py                     LLMClient protocol + offline MockLLM + AnthropicLLM adapter
  orchestrator.py            runs the agents and assembles both role views
  billing_tab.py             provider billing-tab adapter (F1) — a view, never a second engine
  completeness.py            note rule packs + attestation block (F2)
  direct_contract.py         the F3 gate: hard-stop for charges, review for chases
  guardrail_fuzz.py          adversarial cases behind the anti-inflation invariant
  chase/                     insurance-data chase (F9)
    models.py                CoverageSnapshot (the athenaNet boundary), ChaseItem, states
    detector.py              F9a triggers + appointment-proximity priority
    policy.py                cadence/channels/templates as config; the PHI guard
    sequence.py              approval-gated state machine; phone = staff task
    worklist.py              one integrated list, three bucket views
    metrics.py               outstanding, touches/card, minutes/card, time-to-card
  knowledge/
    em_ruleset.py            2021+ E/M leveling (MDM grid + total time), rule-based and explainable
    procedures.py            procedure/add-on candidates from documented language (CPT 99058)
    icd10_ccsr.py            ICD-10 <-> CCSR lookup on the AHRQ data (+ ported embedding search)
  agents/
    coding.py                proposes E/M + ICD-10, always with a documentation span
    documentation_integrity.py   rejects contradicted codes, flags documentation gaps
    compliance_guardrail.py  the hard gate: can only downgrade or remove, never inflate
    denial_risk.py           specificity / linkage / modifier checks
    feedback.py              emits the typed nudges (patient-benefit framed)
  data/icd10_ccsr_subset.csv authentic CMS ICD-10-CM rows for the demo
demo_visit.py                the GI-bleed sample encounter
run_demo.py                  end-to-end billing demo
demo_chase.py                end-to-end insurance-chase demo (for the staff interview)
prove_no_upcode.py           guardrail invariant test
DESIGN.md                    architecture + the design decisions and why
```

See `DESIGN.md` for the architecture, the compliance stance, one design change
from the original brief, and a security item about the prototype repo.
