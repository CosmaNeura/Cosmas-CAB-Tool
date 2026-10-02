# CosmaNeura — Agentic Billing Layer

A multi-agent pipeline that turns a finished visit (intake text + transcript +
signed note) into accurate, fully-auditable billing codes, with two role-scoped
coding outputs, an insurance follow-up worklist, and a feedback loop back into
the rest of the platform. It is built on
top of Homen's **FluencyMed** prototype and consumes that prototype's existing
outputs directly.

## Product boundary

CAB provides evidence-linked, human-reviewed billing intelligence. It does not
generate or transmit 837P, submit claims, enroll with a clearinghouse or payer,
or write to an EHR. Public signup and onboarding do not request or require NPI;
NPI is not identity, licensure, credential, or tenant proof. The separately
governed, default-off Card Chase outreach workflow remains subject to its
existing consent, direct-contract, authorization, and activation gates and does
not transmit claims. See
[`NPI_AND_CLAIM_SUBMISSION_SCOPE.md`](docs/NPI_AND_CLAIM_SUBMISSION_SCOPE.md).

Provider credential/licensure status is unverified informational metadata only;
it never grants or blocks signup, trial, billing, continued use, entitlement,
CAB capability, or real-PHI access. Practices authorize workforce membership
and roles. Core independently enforces identity, membership/role, legal,
subscription, tenant, consent, and rollout controls.

## Run it

No API key and no network needed — the demo runs on a deterministic offline
model and an authentic (public-domain) ICD-10 subset:

```bash
pip install pandas pyarrow          # pyarrow only needed for the full code set
python run_demo.py                  # prints the provider view + biller worksheet + feedback
python prove_no_upcode.py           # proves the guardrail cannot upcode
```

## Pull request change budget

Pull requests receive a secretless `change-budget` status check from the
trusted default-branch workflow. It rejects more than 2,000 changed lines
(additions plus deletions) or 50 changed files and fails closed when GitHub's
diff metadata is missing, malformed, or negative. There is no label, checkbox,
or actor waiver; oversized changes must be split into dependency-ordered pull
requests. The workflow does not change human-approval settings. Repository
administration must separately require the `change-budget` status context for
it to prevent a merge.

## The pilot demo (API + web UI)

Two terminals:

```bash
# 1. the engine API (FastAPI; first request loads the NCCI edits, ~18s)
pip install fastapi uvicorn
uvicorn cosmaneura_billing.api:app --port 8000

# 2. the web UI (provider, coder review, and insurance follow-up)
cd frontend && npm install && npm run dev     # -> http://localhost:5173
```

`GET /api/session` gives the UI server-owned view capabilities. `Provider` sees
only Provider; `BillingStaff` sees only Insurance follow-up; `Coder` and `Admin`
see all three tabs. Every underlying route repeats that authorization check, so
hiding a tab is never the enforcement boundary. The UI shows a live-engine
status pill (NCCI / coverage / CPT), lets authorized users pick
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
# Set COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY to a new random secret of at least
# 32 ASCII characters; use the same value across API replicas.
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
distinct practice-bound database credential. That credential and the ACS
connection string must never be injected into the API container. The default
chase transport remains inert.

Azure Bicep templates and the reviewed deployment sequence are in
`../infra/README.md`. They create a VNet-injected Container Apps environment,
private PostgreSQL, private ACR and Key Vault endpoints, separate managed
identities, Log Analytics, App Insights, a one-shot migration job, and the
bounded chase job. Application ingress
defaults private and must remain private until P2 authentication is verified.
No Azure resources are created by tests or by building the repository.
The reviewed Azure OpenAI resource shape, fixed network-cost delta, and token
scenarios are in
[`../docs/deployment/2026-08-18-azure-openai-cost-plan.md`](../docs/deployment/2026-08-18-azure-openai-cost-plan.md).
The difference between current `main` and the accepted live image, plus the
final disabled-latch audit, is recorded in
[`../docs/deployment/2026-08-19-demo-release-candidate-evidence.md`](../docs/deployment/2026-08-19-demo-release-candidate-evidence.md).
The Entra trust boundary, role matrix, request controls, audit event contract,
and local-development exception are in `docs/ACCESS_CONTROL.md`.
The read-first Athena sandbox architecture, including why it uses a private
outbound sync job rather than a second public API, is in
`docs/ATHENA_SANDBOX_INTEGRATION.md`; the exact Preview, synthetic/read-only
enrollment request is in `docs/ATHENA_SANDBOX_ACCESS_REQUEST.md`. The four-case
mock now exercises source-agnostic durable receipts/checkpoints and atomic
encounter persistence. Live OAuth/network execution remains deliberately
unimplemented and all deployment gates default off. Provider lab analysis is a
separate Sovereign integration and must not share this billing importer's OAuth
client, secrets, tokens, or storage contract.

Pilot ownership and operations are recorded in:

- [`docs/ATHENA_SYSTEM_BOUNDARIES.md`](docs/ATHENA_SYSTEM_BOUNDARIES.md)
- [`docs/MCD_INGESTION_SOURCE_INVENTORY.md`](docs/MCD_INGESTION_SOURCE_INVENTORY.md)
- [`docs/PILOT_PERSONA_PROVISIONING.md`](docs/PILOT_PERSONA_PROVISIONING.md)
- [`docs/PILOT_OPERATIONS_RUNBOOK.md`](docs/PILOT_OPERATIONS_RUNBOOK.md)

Staging and production are PostgreSQL-only: set both
`COSMANEURA_STATE_STORE=postgres` and `COSMANEURA_CHASE_STORE=postgres`, and use
the same runtime `DATABASE_URL` for encounter, feedback, audit, membership, and
chase state. JSONL and in-memory stores are rejected there. The migration job
bootstraps only the explicitly named initial Entra administrator; all later
membership changes use the reviewed owner-only CLI documented in
`migrations/README.md`. Login never creates a user or membership.

The durable encounter store is also empty by default; production never imports
the local `ENC-100237` fixture. A synthetic-only release demo may load exactly
the four cases in `cosmaneura-demo-rc-v1` plus the eight source-only cases in
`cosmaneura-demo-source-extension-v1` with
`python -m cosmaneura_billing.seed_demo_release_candidate` through the private
migration job, following the bounded procedure in `../infra/README.md`. The
command accepts no payload, verifies both packaged whole-pack digests, all
per-case digests, the combined seed digest, the exact `ENABLE ALWAYS` database
immutability guards and function shape, the table-owning migration identity,
and the practice binding. It inserts all twelve cases atomically, is an
exact-replay no-op, and rolls back
without overwriting if any case conflicts. The extension has no embedded coding
labels; its blinded evaluation workflow is in
`docs/SYNTHETIC_EXTENSION_EVALUATION.md` and is excluded from the image.

**Offline mode covers one encounter.** Unless
`COSMANEURA_AZURE_OPENAI_ENABLED=true` and the complete Azure endpoint,
deployment, model, API-version, and managed-identity configuration are present,
the engine runs a deterministic model that only knows the bundled GI-bleed demo
visit. On any other encounter it extracts nothing and proposes nothing —
correctly, and by design. Every response says which inference path ran
(`inferenceMode`, `inferenceModel`) and, when a run produced nothing for that
reason, carries a `degraded` object explaining it, so an empty claim is never
presented as a finding. Invalid enabled configuration fails startup; it never
silently falls back to a live provider.

For an externally reachable synthetic demo, also set
`COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY=true`. This is an independent,
default-off safety latch: every model-backed API route then requires a stored
encounter whose server-owned provenance is exactly
`synthetic_demo_release_candidate`. Ad-hoc `/api/billing` documentation,
operational stored rows, caller-supplied labels, and suggestive encounter-ID
prefixes are rejected before the pipeline can call a model. The engine boundary
rechecks the same provenance token as defense in depth. The Azure template
refuses to combine external ingress with OpenAI unless this latch is on.

P5 observability is deliberately narrower than general-purpose tracing. The API
logs one closed-schema `http_request` event using a server-generated request ID
and declared route template; Azure OpenAI calls log per-attempt latency and
provider-reported input/output/cached-input token counts; the chase worker logs
one terminal aggregate event. A fail-closed scanner rejects expanded schemas,
PHI/secret markers, raw request targets, and synthetic patient canaries in CI.
Azure exports only the explicit numeric LLM instruments to App Insights through
the API managed identity. Generic HTTP/SQL/exception/logging
auto-instrumentation is not enabled because it can observe chart content before
redaction. See `../infra/README.md` for synthetic acceptance and retention
details.

The foundation has provisioned a dedicated North Central US Azure OpenAI
account with public access and local authentication disabled, a private
endpoint/DNS zone, the pinned `gpt-5.6-sol` 2026-07-09 `DataZoneStandard`
deployment at capacity 200, and an account-scoped inference role for the API
managed identity. Provisioning does not enable application inference. A
synthetic data-plane run accepted strict JSON Schema and the JSON-object path on
the `1c5d7e7` image, then returned the application to inference-off. The
2026-08-19 release-candidate baseline `de2b16f` was not deployed; the eventual
merged manifest digest must be accepted separately. Do not transfer acceptance
across source identities.

Real chart text remains prohibited until the Microsoft agreement/BAA is
verified to cover the Azure OpenAI service and US data zone, the service's
data-processing and retention posture is documented, and private-network,
managed-identity, P5 redaction, and deployment acceptance are green. Resource
creation or parameter enablement is not evidence that those gates are complete.

## Provider billing tab and note completeness

```
POST /api/encounters/billing-tab   provider-facing view; `encounterId` is JSON (F1)
POST /api/completeness             run a note rule pack pre-submission (F2)
```

All protected stored-encounter operations use fixed POST paths and carry the
validated `encounterId` in JSON; chart identifiers never appear in request
targets. Stored-encounter responses expose server-derived `dataClass` and
`readOnly` fields. Only persisted provenance equal to
`synthetic_demo_release_candidate` marks a fixture read-only; external ID
prefixes have no authority over that policy.

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

Azure OpenAI inference is explicit and managed-identity-only. For a controlled
synthetic/de-identified run from an Azure-authorized environment, configure the
reviewed deployment and opt in; no `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`, or
static bearer token is accepted:

```bash
COSMANEURA_AZURE_OPENAI_ENABLED=true \
AZURE_CLIENT_ID=<api-uami-client-id> \
AZURE_OPENAI_ENDPOINT=https://<account>.openai.azure.com/ \
AZURE_OPENAI_DEPLOYMENT=gpt-5-6-sol-2026-07-09 \
AZURE_OPENAI_MODEL=gpt-5.6-sol \
AZURE_OPENAI_API_VERSION=2025-04-01-preview \
python run_demo.py

ICD10_CCSR_CSV=/path/to/DXCCSR_v2023-1.csv   python run_demo.py
```

`run_demo.py` is a local direct-engine tool and therefore cannot prove stored
server-owned provenance; do not set the synthetic-only API latch for that
command. Exercise the deployed latch only through the fixed stored-encounter
POST routes after the fixed seeder has established provenance.

The deployment acceptance command is intentionally narrower than `run_demo.py`
and accepts no chart/prompt input:

```bash
python -m cosmaneura_billing.azure_openai_acceptance > /tmp/azure-openai-acceptance.jsonl
python -m cosmaneura_billing.log_scanner /tmp/azure-openai-acceptance.jsonl
```

Restore the application inference latch to false immediately after the bounded
synthetic run and verify the final revision contains no Azure OpenAI variables
and emits no LLM events.

Pipeline-result reuse across the provider, billing, and coder surfaces remains
a follow-on. A safe cache needs practice ID, opaque encounter resource/version,
engine configuration, and reviewer-preference revision in its key, immutable
values, a strict bound, and no raw documentation in shared cache metadata.
Recomputing is safer than cross-user or stale-result reuse until that contract
is implemented and tested.

The adapter selects strict Structured Outputs for locally compatible schemas
and selects JSON-object mode up front for schemas outside the supported strict
subset; both paths are parsed and schema-validated fail-closed in deterministic
Python. A provider 4xx is terminal and never causes a weaker retry. OpenAI
documents those API behaviors in the
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
Azure resource, networking, Entra, quota, and data-zone behavior is operated and
validated through Microsoft Azure.

The separate legacy `api.openai.com` embedding/indexing seam is development-only
and is not part of the pilot runtime. It is prohibited in staging and production
unless separately migrated to Azure OpenAI, private-networked, managed-identity
authenticated, and accepted under the same PHI controls.

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

## Second extraction engine (Azure Text Analytics for health)

The repository contains an optional second extraction engine, disabled by
default. Azure's Text Analytics for health
contributes clinical NER, assertion detection (negated / hypothetical /
family-member) and UMLS entity links to ICD-10-CM; it never levels E/M. The
isolated shadow mode queues TA4H on a bounded background worker, reduces its
output to PHI-safe aggregate counts, and returns the primary result byte-for-byte
without waiting for TA4H. Agreement badges and reviewer-facing
provenance remain deferred. The separately gated active-consensus mode may hold
single-engine codes or apply a TA4H veto.

Off by default, on the same terms as inference, and managed-identity only — there
is no key-based path. The API's existing user-assigned identity needs a
*Cognitive Services Language Reader* role assignment on the Language resource;
no new secret enters the deployment.

```bash
# offline deterministic mock (development only; refused in staging/production)
COSMANEURA_AZURE_LANGUAGE_MOCK=1 uvicorn cosmaneura_billing.api:app --port 8000

# observation-only deployment; requires primary OpenAI + reserved synthetic input
export COSMANEURA_AZURE_OPENAI_ENABLED=1
export COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY=1
export COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED=1
export COSMANEURA_AZURE_LANGUAGE_ENABLED=0
export AZURE_LANGUAGE_ENDPOINT='https://<resource>.cognitiveservices.azure.com'
export AZURE_CLIENT_ID='<api-managed-identity-client-id>'
uvicorn cosmaneura_billing.api:app --port 8000

# fixed synthetic deployment acceptance (accepts no chart input)
python -m cosmaneura_billing.ta4h_shadow_acceptance
```

Input logging is opted out on every analysis submission (PHI), the transport is stdlib
`urllib` so no HTTP/SDK dependency is added, and a TA4H outage in shadow mode
changes only aggregate health status. The external 40–60-case coder-labelled
gold set is still pending; runtime aggregates are not accuracy evidence.
`prove_no_upcode.py` proves that adding the engine cannot
raise a level or grow the captured set, across 2,000 adversarial cases. See
`docs/TA4H_INTEGRATION.md` for the consensus policy and rollout gates, and
`docs/TA4H_MULTI_MODEL_ASSESSMENT.md` for the cost/HIPAA/value assessment.

## Insurance-data chase (F9)

The billers' #1 stated pain: 300–500 claims a month held for missing insurance
information. The validated finding is that the bottleneck is **patient
submission, not OCR** — card capture already works. What was missing is the
system that gets the card *to* capture: a master worklist, worked across ACS
email when enabled and manual phone tasks, by the staff whose whole job this is
(while coders may view the same operational context). SMS and portal delivery
are not implemented in this slice.

```bash
python demo_chase.py    # end-to-end, synthetic patients, nothing leaves the building
```

```
POST /api/chase/worklist                 master list; filters are strict JSON body fields
POST /api/chase/task                     audited minimum-necessary contact task
POST /api/chase/demo-seed                fixed digest-pinned four-case seed (Admin)
POST /api/chase/detect                   detect and durably merge coverage snapshots
POST /api/chase/metrics                  metrics; baselines are strict JSON body fields
GET  /api/chase/policy/status            configured policy's PHI-free lifecycle status (Admin)
POST /api/chase/policy/create            create the server-configured revision (Admin)
POST /api/chase/policy/approve           append an exact approval event (Admin)
POST /api/chase/policy/activate          activate the latest exact approval (Admin)
POST /api/chase/policy/revoke            append revocation and retire queued work (Admin)
POST /api/chase/release-message          release one exact per-message cadence step (Admin)
POST /api/chase/action                   release | suppress | assign | log_attempt | submitted | verify
```

The visible worklist/task projection is available to `BillingStaff`, `Coder`,
and `Admin`. Detection and state-changing staff actions remain
`BillingStaff`/`Admin`; policy, direct-contract release, exact message release,
and synthetic seed operations remain `Admin`.

Protected resource identifiers are carried in strict JSON bodies rather than
request targets, so platform access logs cannot capture them from URLs. Each
staff action carries `chaseId` plus a client-generated UUID `action_id`. An
exact retry is replayed without adding another contact or minute count; reuse
with a different action payload returns 409.

The worklist response intentionally omits patient references, gate payloads,
assignee identities, attempt notes, and contact destinations. Opening a row uses
`POST /api/chase/task` with the opaque `chaseId` in the JSON body. Operational
rows point staff to the approved practice source-system patient-search workflow
until a safe deep-link contract exists. Only an exact server-owned synthetic
provenance marker can reveal one of the bundled fictional `+1 202-555-01xx`
destinations; a lookalike row receives the operational placeholder.

The Admin-only demo seed requires this exact body:

```json
{"seedDigest":"fc207ef1419882f48dce5b12dae05233de2f99d9ea930ee5eb84d84e659d23ff"}
```

It installs missing-card, expired-plan, inactive-eligibility, and
direct-contract human-review cases. The durable write and its access-audit
completion are one transaction; exact replay is a no-op and partial/lookalike
state conflicts. Seeding refuses to run unless scheduler, patient messaging,
and operational activation are off and transport is `record_only`.

In durable PostgreSQL mode, `POST /api/chase/detect` commits its detection,
contact snapshots, and audit completion as one batch and returns only
`checked` and `detected` counts. Fetch the resulting rows with
`POST /api/chase/worklist`; the detect response deliberately does not duplicate
the worklist or expose partial writes.

Policy cadence, templates, upload URL, transport, ACS endpoint, sender, and
reply-to values come only from server configuration. Clients submit only the
operation UUIDs and, where applicable, a reason; they cannot replace an
approved policy body through an API request. Policy lifecycle endpoints and
the exact per-message or direct-contract release paths require `Admin`.

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
transport is `acs_email`, and the exact configuration has human approval. The
API and worker receive the same `COSMANEURA_ACS_EMAIL_ENDPOINT`, sender, and
reply-to fingerprint metadata; only the worker receives
`COSMANEURA_ACS_EMAIL_CONNECTION_STRING`. Staging and production also require a
shared chase-gate HMAC key of at least 32 bytes, including for `record_only`, so
gate evidence cannot silently drift. Keep both messaging latches off and use
only synthetic/de-identified recipients: real patient messaging remains
blocked until the deployment layer, applicable BAAs, P5 log scanning in the
deployed environment, and deployment acceptance are complete.

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
- **Phone is a route-checked staff task, never an automated call.**
  The scheduler persists the task; `action=log_attempt` completes that exact
  durable task, records the outcome and minutes, and advances its cadence in one
  transaction. This is what makes phone work visible in the metrics. There is
  no outbound voice transport.
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
  llm.py                     LLMClient protocol + offline MockLLM + AzureOpenAILLM adapter
  ta4h.py                    Azure Text Analytics for health client + offline MockTA4H
  orchestrator.py            runs the agents and assembles both role views
  billing_tab.py             provider billing-tab adapter (F1) — a view, never a second engine
  completeness.py            note rule packs + attestation block (F2)
  direct_contract.py         the F3 gate: hard-stop for charges, review for chases
  guardrail_fuzz.py          adversarial cases behind the anti-inflation invariant
  consensus_fuzz.py          the same, with a second extraction engine in the loop
  chase/                     insurance-data chase (F9)
    models.py                CoverageSnapshot (the source-neutral insurance boundary), ChaseItem, states
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
    ta4h_coding.py           optional 2nd extractor: UMLS-linked ICD-10 + assertion vetoes
    consensus.py             reconciles the two extractors; only ever more conservative
    documentation_integrity.py   rejects contradicted codes, flags documentation gaps
    compliance_guardrail.py  the hard gate: can only downgrade or remove, never inflate
    denial_risk.py           specificity / linkage / modifier checks
    feedback.py              emits the typed nudges (patient-benefit framed)
  data/icd10_ccsr_subset.csv authentic CMS ICD-10-CM rows for the demo
demo_visit.py                the GI-bleed sample encounter
run_demo.py                  end-to-end billing demo
demo_chase.py                end-to-end insurance-chase demo (for the staff interview)
prove_no_upcode.py           anti-inflation invariant proof (one engine and two)
DESIGN.md                    architecture + the design decisions and why
docs/TA4H_INTEGRATION.md     the multi-engine path: config, policy, rollout
```

See `DESIGN.md` for the architecture, the compliance stance, one design change
from the original brief, and a security item about the prototype repo.
