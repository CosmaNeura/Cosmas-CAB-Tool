# Handoff — what was built 2026-08-17, and the Azure lane

**Repo:** `CosmaNeura/Cosmas-CAB-Tool` · **Branch:** `claude/platform-handoff-codex-obnlqb`
**Working folder:** the tool is `cosmaneura_billing/` (repo root still holds the 2023
FluencyMed prototype)
**State:** 358 tests pass (was 156), guardrail invariant proof green, both demos run,
frontend builds. CI covers all of it.

This supersedes `HANDOFF_CODEX_2026-08-16.md`, which described the platform-stabilization
scope *before* any of it was done. Read that one only for background.

---

## 0. The one-line division of labour

**The engine's judgment stays with the coding work. Everything between the engine and a
real user is the Azure lane.** Concretely: if a wrong answer produces a wrong *claim*,
it is not in this handoff. If a wrong answer produces an outage, a leak, or a lost
record, it is.

The seams for that split already exist and are named in §4. None of the Azure work
requires reading the coding logic.

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

## 4. The Azure lane — Codex's work

Everything here is infrastructure. None of it needs clinical or coding judgment, and all
of it gates PHI. Ordered by what blocks what.

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
| 10 | Patient-contact channels & consent | **Open — and it gates half the cadence.** SMS/phone are consent-gated and fail closed, so with no recorded consent those steps drop out and the sequence runs on email alone |
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

## 7. What is deliberately not built

- **Automated outbound voice.** Phone is a staff task the system schedules and a person
  executes and logs. A robocall about insurance is a TCPA problem; this keeps call work
  visible in the metrics without becoming one. A telephony adapter can be added later
  without touching the state machine.
- **Modifier proposals.** `CodeKind.MODIFIER` exists and no agent emits one, so
  `worksheet.modifiers` is always empty. Blocked on Q4.
- **Claim export (837P/CSV).** `docs/CAC_DATA_SOURCES.md` §16 surveys the parsers.
- **CPT descriptors.** `cptDescriptors: 0` without an AMA license; the turnkey auto-fetch
  path is built and documented, it just needs credentials.
- **F4 Athena section splitter**, **F7 auto-populate**, **F8 lab tracker** — all gated.

---

*Prior context: `HANDOFF_CODEX_2026-08-16.md` (pre-work scope),
`docs/discovery/00-requirements-synthesis.md` (authoritative for feature scope),
`docs/discovery/2026-08-13-mcd-biller-followup.md` (what the follow-up did and did not
answer), `/CLAUDE.md` (hard constraints).*
