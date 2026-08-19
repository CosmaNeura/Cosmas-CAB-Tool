# Handoff — what was built 2026-08-17, and the Azure lane

> **Historical snapshot.** The test counts, unbuilt-infrastructure statements,
> and Anthropic-specific configuration below describe the repository on Aug 17
> and must not be used as current deployment instructions. Platform deployment
> has since advanced, and the live-inference decision changed to a dedicated,
> private Azure OpenAI deployment with managed identity only. Read
> [`HANDOFF_CODEX_2026-08-18.md`](HANDOFF_CODEX_2026-08-18.md) and the
> [Azure OpenAI cost-plan addendum](docs/deployment/2026-08-18-azure-openai-cost-plan.md)
> for the current plan. The anti-inflation, evidence, PHI, and UI-lane
> constraints in this document remain binding.

**Repo:** `CosmaNeura/Cosmas-CAB-Tool` · **Branch:** `claude/platform-handoff-codex-obnlqb`
**Working folder:** the tool is `cosmaneura_billing/` (repo root still holds the 2023
FluencyMed prototype)
**State:** 358 tests pass (was 156), guardrail invariant proof green, both demos run,
frontend builds. CI covers all of it.

This supersedes `HANDOFF_CODEX_2026-08-16.md`, which described the platform-stabilization
scope *before* any of it was done. Read that one only for background.

---

## 0. The division of labour

**The goal is a deployable pilot.** Two lanes run in parallel toward it.

**§4 — the Azure lane.** Everything between the engine and a real user: deployment, auth,
persistence, transport, scheduling, observability. None of it needs clinical or coding
judgment, and the seams already exist (named per item).

**§7 — the feature backlog.** Things not built today that the pilot needs anyway: claim
export, licensed CPT descriptors, the Athena section splitter, modifier proposals. These
**do** affect claims, so they carry the engine's obligations — deterministic Python with
tests, evidence spans on anything a user sees suggested, and the anti-inflation invariant
green. Read `/CLAUDE.md` before starting any of them.

What stays with the coding work either way: the guardrail itself, E/M leveling, ICD/CPT
selection rules, the completeness rule packs, and the accuracy/validation protocol. If
you are unsure which lane something is in, the test is whether getting it wrong produces
a wrong **claim** (coding lane) or a wrong **deployment** (Azure lane) — and §7 items sit
on that line, which is why they carry the coding lane's rules while being yours to build.

### Pilot critical path

The shortest route to something Cindy, Maggie and Grace can actually use:

0. **Start the Athena sandbox application — day one.** The only long-lead item with a
   third party on the other side, and it blocks nothing while pending. Everything below
   proceeds without it; §7.8 is what lands when it arrives.
1. **§4 P0** — chase worklist off the in-memory singleton (it loses data today)
2. **§4 P1–P2** — deployment + auth (gates every real user)
3. **§4 P3–P4** — Postgres, then ACS **email** + the scheduler. This is what turns F9 from
   a demo into the thing the two staff actually work from
4. **§7.2 CPT descriptors** — credentials only; turns generic labels into real ones
5. **§7.4 F4 splitter** — kills most of the manual note-chopping today, zero gates
6. **§7.1 CSV export** — batch fallback; **not** a blocker (copy-paste already works)
7. **§4 P5** — logging + PHI redaction, before real PHI flows
8. **§7.8 Athena read integration** — when the sandbox lands

---

## 1. What was built today

Seven pieces, in dependency order. Every one is deterministic Python with tests; nothing
below is prompt-only.

| # | Piece | Where | Tests |
|---|---|---|---|
| 1 | Repo foundations — `CLAUDE.md` hard rules, `docs/discovery/` corpus | root, `docs/discovery/` | — |
| 2 | Anti-inflation fuzz suite (2,000 adversarial cases) | `guardrail_fuzz.py` | 10 |
| 3 | Offline-mode honesty + inference-path reporting | `llm.py`, `orchestrator.py` | 11 |
| 4 | Procedure/add-on codes, CPT 99058 | `knowledge/procedures.py` | 13 |
| 5 | E/M total-time pathway with an evidence gate | `knowledge/em_ruleset.py` | 25 |
| 6 | Note completeness checker, `telehealth.v1` | `completeness.py` | 28 |
| 7 | Provider billing tab (F1) | `billing_tab.py` | 19 |
| 8 | Direct-contract gate (F3) | `direct_contract.py` | 19 |
| 9 | Insurance-data chase (F9) | `chase/` | 76 |

### Six real bugs this found

Worth reading, because they are the argument for the tests rather than a changelog.

1. **`"99212-95"` upcoded.** E/M codes were levelled by their last character, so a
   modifier-suffixed 99212 read as level 5; clamping it to a 99213 ceiling *raised* a
   level-2 visit to level 3.
2. **`"99214 (telehealth)"` bypassed the ceiling.** It parsed as level 0, never tripped
   the check, and was captured above documented support.
3. **`PROCEDURE`/`MODIFIER` codes skipped the guardrail entirely** — a code pre-marked
   captured with no evidence stayed captured.
4. **Time-based E/M levelled on an unverified number.** `total_time_minutes` alone raised
   the level, and the guardrail could not catch it because its own ceiling came from the
   same number. A hallucinated time turned a 99212 into a 99215. Now requires a quote
   that verifies against the chart, checked before anything levels on it.
5. **The production LLM adapter would have failed on first contact.** Stale model
   default, and `max_tokens` sized for output alone when thinking now shares the budget —
   extraction truncated before the forced tool call completed, which looked like a
   malformed reply and was retried five times before failing.
6. **Chase: an open call task was re-issued every pass** (staff would see the same call
   land on the worklist daily), a cadence with no usable step reported *"escalated after 0
   touches with no card received"* (reads as the patient ignoring messages that never went
   out), and `SequenceRunner`'s default clock was a 1970 placeholder that would have
   stamped production rows and corrupted time-to-card.

### Two judgment calls recorded, not hidden

- **F1 has two acceptance criteria that collide on removed codes**: "no candidate renders
  without an evidence span" vs. "the patient-denied family-history code is rejected." A
  contradicted code has no *supporting* evidence by construction. Resolution: actionable
  codes (suggested, held) require a span; removed codes are admitted on their **reason**,
  because deleting *"we considered this and here is why not"* is what would make the view
  un-auditable.
- **`confidence` in the billing-tab contract is `null`, deliberately.** This engine is
  rule-based and produces no calibrated probability. A fabricated `0.92` next to a real
  evidence span would misrepresent how the code was chosen. If a number is wanted there,
  it has to be a real one.

---

## 2. The baseline — there are two, and they are not the same number

This was being said ambiguously. Pinning it down:

### 2a. The chase baseline (F9's)

How the insurance-chasing job performs **today, before the tool**. Only Cindy and Maggie
have these:

| Number | Why it matters |
|---|---|
| Cards outstanding **right now** | The denominator. Without it, "we closed 40" means nothing |
| Touches per card | What the cadence has to beat |
| **Minutes per card** | The only number showing work came *off* the two staff |
| Cards chased per month | Volume, to size the thing |

`chase/metrics.py::baseline_comparison()` takes two of these
(`baseline_minutes_per_card`, `baseline_claims_held_monthly`) and returns
`baseline_available: false` with a re-ask note until they are supplied. **It does not
estimate**, on purpose — a made-up baseline becomes a number someone quotes back later.

### 2b. The coder baseline (F6 / validation's)

**Net coder minutes per claim**, and **denial counts by reason**. This is *Grace's*
constraint metric — the one that proves the billing tab and completeness checker do not
add review work. Different persona, different surface, unrelated to F9.

### What is actually on record

**Only one figure exists in the whole corpus: 300–500 claims held monthly**, from the
Aug 6 Fathom recap. Neither baseline appears in anything in this repo or in the Aug 13
follow-up summary, and Grace could not produce a recording of that call. The Aug 12 plan
reserved a one-time month-end window for these numbers; whether they were captured
elsewhere is unknown here. **Re-ask explicitly** — do not let a demo proceed on the
assumption they exist.

---

## 3. The persona correction that shaped F9

The requirements synthesis §2 lists biller/coder, provider, therapist. **The two people
whose whole job is tracking down insurance data are none of those.** The corpus names
Cindy and Maggie entering missing cards into Athena after appointments.

This matters because Grace's standing constraint — *net coder minutes must go down* — is
about the **coder** persona. It still binds on F1/F2/F6. It is **not** the metric for the
chase surface, which is measured on cards closed, touches per card and minutes per card.
Building F9 for the coder persona would have optimised the wrong thing.

The other correction: **"HITL but automated"** resolved as *policy-level* approval — a
named person approves the policy once, the system works the list, exceptions escalate.
Per-message approval remains available as one config field
(`COSMANEURA_CHASE_APPROVAL=per_message`). At 300–500 held claims a month, a queue
needing a click per message is a queue nobody drains.

---

## 4. The Azure lane — infrastructure

Everything here is infrastructure. None of it needs clinical or coding judgment, and all
of it gates PHI. Ordered by what blocks what. §7 is the feature backlog that runs
alongside it; the interleaved order for a pilot is in §0.

### P0 — the bug that will bite in Azure specifically

**`get_chase_context()` in `api.py` is an `@lru_cache(maxsize=1)` singleton holding the
worklist in memory.** On multi-replica Container Apps each replica gets its own copy: the
list silently diverges per replica, and a restart loses every contact log and every
metric. This must be Postgres before anyone real touches the chase surface. It is the
first thing to do.

### P1 — deployment layer (gates literally everything else)

Nothing exists today: no Dockerfile, no compose, no Bicep, no served frontend, no config
module. Per the expansion plan the target is Azure-native — Container Apps, Postgres
Flexible Server, Key Vault, private endpoints, App Insights.

- The API should serve the built frontend from the same origin. `frontend/src/api.js`
  already assumes same-origin in production; nothing implements it.
- **Cold start is ~14s** on the first request (NCCI/ICD-10/coverage load, measured).
  `get_orchestrator()` is lazily cached — warm it at startup and add a readiness endpoint
  so a load balancer does not route to a cold replica.
- **Consolidate config.** 23 scattered `os.environ` reads (`ANTHROPIC_*`, `AMA_*`,
  `CPT_*`, `COSMANEURA_*`, `ICD10_CCSR_CSV`). One settings module with validation, plus a
  `.env.example` — there isn't one, and the AMA turnkey path alone needs four variables.

### P2 — access control

- `CORSMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])`.
- **No authentication of any kind.** Entra ID for staff.
- **Coder identity is a query parameter.** `?user_id=` and the `coder_id` in the feedback
  POST body are caller-supplied. Any caller can act as any coder — and because
  `PreferenceStore` derives the adaptive layer from that log, poison another coder's
  learned preferences. Identity must come from the session.
- No rate limiting, no request-size cap (`POST /api/billing` accepts unbounded text).
- Access log of view/decide events — the start of the HIPAA audit posture.

### P3 — persistence

| Today | Needs |
|---|---|
| `coder_feedback.jsonl`, unlocked `"a"`-mode append; every summary re-parses the whole file | Postgres table + migration path for existing records |
| `chase.Worklist` — in-memory dict (see P0) | Postgres: items, attempts, pending tasks, resolutions |
| `DirectContractList` — local CSV/JSON file | Real source (see §5 Q5); **swap the loader only, the gate semantics must not change** |
| `migrations/001_init_pgvector.*` — dialogue-RAG vector table only | Migrations for encounters, feedback, preferences, users, audit, chase |

### P4 — the chase runtime (this is what makes F9 real)

Two things are missing, and both are pure infrastructure:

1. **A transport.** `chase.sequence.Transport` is a one-method injected callable, and the
   default `record_only_transport` records without contacting anyone. **Drop in ACS**
   (email + SMS) under the Microsoft BAA. Do not change the state machine.

   ```python
   SequenceRunner(policy=policy, transport=acs_transport, now=utc_now)
   ```

   Two constraints the code already enforces and the adapter must not defeat: never send
   under an unapproved policy, and never put insurance data in a body
   (`chase.policy.assert_no_phi` runs at config time *and* send time).

2. **A scheduler.** `SequenceRunner.advance()` has no caller in production — today only
   the demo and the API drive it. Needs a worker (Container Apps job, or Durable
   Functions per the expansion plan) that walks the worklist on a cadence, respects
   `policy.quiet_hours`, and is idempotent per item per day.

### P5 — observability

No structured logging, no request ids, no exception middleware; an unhandled error
returns a bare 500. App Insights, plus a **PHI-redaction layer at the logging boundary**
and a log scanner in CI. Add per-request token/latency accounting to
`AnthropicLLM.complete_json` — without it nobody can price a chart.

### Gated on something other than Azure

- **athenaNet adapter** behind `chase.models.CoverageSnapshot` — blocked on sandbox
  access, not on Azure. All protocol knowledge belongs in `athena_client.py`.
- **CardCapture verify-and-write** — `close_verified(athena_write_id=...)` is the seam;
  the pipeline lives in the other workstream.
- **Real messaging and real PHI** — blocked on the BAA and the deployment layer, which is
  why the default transport is inert.

---

## 5. Open questions — nobody should build past these

Status after the Aug 13 follow-up. **Q7 is the only one it resolved.**

| Q | Question | Status |
|---|---|---|
| 1 | Will the practice pay ($59/user/mo; org vs provider buyer)? | Open — deferred to Greg & Kathleen |
| 2 | Coder baseline: minutes/claim, denials by reason | **Open — re-ask (§2b)** |
| 3 | Self-pay chase mechanics: sequencing, tone, escalation, approver | Open — F5 mechanics on hold |
| 4 | Modifier workflow + coding/bundling | **Deferred twice.** Blocks F6 modifier surfacing |
| 5 | Direct-contract list: where it lives, who maintains it | Open — gate ships against a local file |
| 6 | Athena paste-target section names | Partial — billing-slip flow shown, targets not enumerated |
| 7 | Time-coded vs MDM-coded; does the team re-level on time? | **Resolved:** build the time pathway (done) |
| 8 | What has billing heard about Athena's own AI? | Open — Greg forwarded Athena's Ambient AI announcement Aug 12 |
| 9 | Chase baseline: touches, minutes, cards outstanding | **Open — re-ask (§2a)** |
| 10 | Patient-contact channels & consent | **Mostly resolved, 2026-08-17 (owner).** **Email: yes** — build it, and an email to an established patient needs no per-patient opt-in. **SMS: yes as a channel**, but each patient still needs recorded TCPA consent and the gate stays fail-closed. **Outbound voice: no** (see §7.7); the staff call task stays. Still open, and narrower: **where the SMS opt-in flag is populated from** — Acuity, Athena, or a form the practice runs. Until that feed exists every `consented(SMS)` answer is False and the cadence runs on email alone, which is a working configuration, not a blocked one |
| 11 | Should direct-contract patients be chased at all? | Open — routed to human review until answered |

Plus one new: **who approves the chase policy, and how often do they want to be asked?**

---

## 6. Running it

```bash
cd cosmaneura_billing
pip install -e ".[dev]"
python -m pytest                 # 358 tests
python prove_no_upcode.py        # invariant proof + 2,000 fuzz cases — must stay green
python run_demo.py               # billing pipeline, offline
python demo_chase.py             # insurance chase, end to end, synthetic patients
uvicorn cosmaneura_billing.api:app --port 8000
cd frontend && npm install && npm run dev
```

`demo_chase.py` is the artifact for the staff interview — seven synthetic patients through
detection → worklist → policy approval → four unattended passes → a logged staff call →
verified close → metrics, ending with the questions to ask. It runs in CI so a break fails
the build rather than surfacing mid-conversation.

### Non-negotiable

The compliance guardrail may only **hold, downgrade, or remove** a code. Never inflate.
`prove_no_upcode.py` and `tests/test_guardrail_fuzz.py` are the proof and run in CI.
Nothing in this handoff is worth breaking that for. The full hard-rule list is in
`/CLAUDE.md`; read it before touching the engine.

### New endpoints added today

```
GET  /api/encounters/{id}/billing-tab   provider billing view (F1)
POST /api/completeness                  note rule pack, pre-submission (F2)
GET  /api/chase/worklist                the master list
POST /api/chase/detect                  detection over coverage snapshots
GET  /api/chase/metrics                 outstanding, touches/card, minutes/card
POST /api/chase/{id}/action             release|suppress|assign|log_attempt|submitted|verify
```

---

## 7. Not built by me — the feature backlog, assigned to Codex

These are pilot-critical and they are yours. For each: why it wasn't built today, what is
buildable **now**, and what (if anything) it is genuinely waiting on. Where an item
affects a claim it carries the coding lane's rules (§0).

### 7.1 CSV export — build it, but it is not a blocker. **837P: dropped**

**Correction to an earlier version of this doc**, which called claim export "the gap that
makes the whole thing academic". That was wrong: the billing tab already carries
`copy_text` per code and `copy_all`, so a coder can act on the engine's output today. CSV
is a **batch multiplier** — it removes twenty copy-paste cycles, not a wall.

**837P is dropped from the backlog** (decision, 2026-08-17). Emitting 837P means becoming
a claim *submitter*: clearinghouse enrollment, payer enrollment, EDI testing, and
submission liability. MCD already runs Athena → clearinghouse → payer. Building 837P
bypasses a submission path that works in order to take on risk nobody asked for. Revisit
only if MCD leaves Athena's submission path.

**Build CSV.** Roughly a day. It earns its place as three things: the batch path, the
fallback when an integration is down, and an audit artifact. Source it from
`BillerWorksheet`, **not** the billing tab — the tab is a provider-facing *view* that
deliberately drops codes whose evidence didn't verify.

Two rules: export **only** `Disposition.CAPTURED` codes, and never let export be the thing
that promotes a code. If an item is held, it stays off the claim.

### 7.2 Licensed CPT descriptors — build now, credentials only

**Why not today:** needs the practice's AMA license, which I don't have.

`/api/health` reports `cptDescriptors: 0` and a `cptStatus` explaining why. The turnkey
auto-fetch path is **already built and tested** (`knowledge/cpt.py`, verified against the
production gateway, CPT 2026 Update 4, 10,988 descriptors). Set the four `AMA_*` variables
from the Developer Portal and it populates; `scripts/fetch_cpt_zip.py --check` validates
credentials in two API calls before downloading. Production credentials are capped around
10 calls/day, so the cache under `~/.cache/cosmaneura/cpt` matters — do not defeat it.

**Never commit descriptor files, the ZIP, or the credentials.** CPT content stays under
the practice's license, outside the repo.

### 7.3 Modifier proposals (F6) — build the objective half now

**Why not today:** Grace deferred the modifier workflow twice (Aug 6 and Aug 13), and it
is her stated review worry. Guessing at it would add coder minutes, which is the one thing
her constraint forbids.

That is a reason to **split** it, not to wait on all of it:

- **Modifier 95 / 93 (telehealth) — build now.** Objective, and the engine already
  *detects* it: `DenialRiskAgent` (`agents/denial_risk.py`, the telehealth POS block)
  raises a flag on POS 02/10 telling the biller to confirm the E/M carries modifier 95,
  or **93 for audio-only** — so the audio-only distinction is already modelled. All that
  is missing is promoting that flag into a `CodeKind.MODIFIER` suggestion carrying the POS
  as its evidence. The guardrail gates PROCEDURE/MODIFIER kinds on evidence as of today,
  so it lands held-for-review rather than auto-captured, which is the right posture for a
  modifier regardless. `completeness.telehealth.v1` gives you the modality signal to pick
  between 95 and 93.
- **Modifier 25 and bundling — still needs Q4.** Judgment-heavy, payer-specific, and the
  coders keep adding it by hand for reasons nobody has written down. Ask before building.

`worksheet.modifiers` is always empty today, so 95 alone is a visible win.

### 7.4 F4 Athena section splitter — build now, zero gates

**Why not today:** ran out of day.

Deterministic split of the generated note into Athena's paste targets (Chief Complaint,
HPI/Subjective, ROS, Assessment, Plan), each with its own copy button, content moved
**unaltered** — sectioning only. Unmapped content lands in a visible "unplaced" block,
never dropped. Acceptance test is a round-trip: concatenating the blocks reproduces the
source note exactly, modulo whitespace.

Two requesters (Christa Aug 6, Maura Jul 24), and it kills most of the manual chopping
*today* without waiting on the Athena sandbox. The one open detail is the exact paste-
target names (§5 Q6) — build against the labels above and make them configurable.

### 7.5 F7 Athena auto-populate — build the harness, not the integration

**Waiting on:** Athena sandbox access. The encounter-section **write** API is unverified;
repo knowledge covers the insurance write only. Do not promise behaviour against
unvalidated payload shapes.

**Buildable now:** extend the mock athenaNet harness and put all protocol knowledge in
`athena_client.py`, so that when the sandbox lands it is credentials plus payload
validation rather than new architecture.

### 7.6 F8 longitudinal lab tracker — contract and fixtures now, real labs gated

**Waiting on:** the BAA, the deployment layer, and Athena read scope. Christa's shadow
spreadsheet is the thing this replaces, and it is *currently* the HIPAA risk — which is an
argument for building it properly, not quickly.

**Buildable now:** the data contract (`analyte, value, unit, date, source,
provider_verified`) plus synthetic lab fixtures. **Rail that cannot move:** every extracted
value needs provider confirmation before entering a persisted series, and analyses are
review surfaces, never auto-written to the chart.

### 7.7 Automated outbound voice — **decided: not building it**

Decision, 2026-08-17 (owner): no automated outbound voice. Do not build a telephony
adapter, an IVR, or automated calling of any kind.

**The staff call task stays.** Phone remains a task the system schedules and a person
executes and logs into the same contact history — that is what makes call work visible in
minutes-per-card instead of being invisible, and the original ask was for one integrated
list that covers calls. Removing it would remove that.

If this is ever revisited: `chase.models.STAFF_TASK` / `SYSTEM_SENDS` are the switch and
the state machine does not change. Counsel review and a recorded-consent source first.

### 7.8 Athena integration — read first, write later

**Waiting on:** sandbox access. Start the application now (§0 step 0); it is the only
long-lead item and it blocks nothing while pending.

**Read is much cheaper to get approved than write, and unlocks more than it looks like.**
Pull slips, claims and denial outcomes and you get three things:

1. Whether the engine's suggestions were actually *used* — the acceptance signal the
   validation protocol needs and currently has no source for.
2. Denial outcomes by reason, which is the coder baseline's second number (§2b).
3. **The only thing that would populate the `denial_coverage` trigger** in
   `chase/detector.py`. That trigger is built and nothing feeds it today.

**Write is the harder ask, and two facts argue against dating the pilot on it:** the
encounter-section write API is unverified (repo knowledge covers the insurance write
only), and Grace reported Athena is *worse than Tebra* at claim modification — if the API
inherits that limitation you may read fine and not usefully write. Prove read first.

All protocol knowledge goes in `athena_client.py`, mock-first, per `/CLAUDE.md`. The
adapter target for coverage state is `chase.models.CoverageSnapshot`.

---

**Not in either lane:** commercial payer rule adjudication (post-validation roadmap), and
behavioural-health/psychotherapy coding (different CPT domain; revisit at the first paying
therapist org).

---

*Prior context: `HANDOFF_CODEX_2026-08-16.md` (pre-work scope),
`docs/discovery/00-requirements-synthesis.md` (authoritative for feature scope),
`docs/discovery/2026-08-13-mcd-biller-followup.md` (what the follow-up did and did not
answer), `/CLAUDE.md` (hard constraints).*
