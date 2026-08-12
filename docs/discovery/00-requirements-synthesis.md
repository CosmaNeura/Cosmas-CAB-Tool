# CAB User Requirements Synthesis — What Users Need & How Features Should Work

**Version:** 1.1 · **Date:** August 12, 2026 · **Owner:** John Clemente
**Suggested repo path:** `docs/discovery/00-requirements-synthesis.md` (sorts first in the discovery folder)
**Evidence base:** Five customer conversations — Scott Weeman/Fiat Health (Jul 21), Maura Buete/MCD (Jul 24), MCD billing team: Grace Targonski, Johanna Adriano, Edgar Morgan (Aug 6), Christa Thornberry/MCD (Aug 6), Sherin George/MCD (Aug 10). Full syntheses live alongside this file in `docs/discovery/`. A sixth conversation (biller follow-up) happens **Aug 13** — see §8 for what it will change.
**Relationship to other docs:** `CLAUDE.md` (repo root) is authoritative for hard constraints; `docs/CAB-groundwork-brief.md` is authoritative for tiering and draft data contracts. This file consolidates *user-voiced requirements* into build-ready form and flags alignment points. Where a contract here differs from the groundwork brief, reconcile toward the brief and note the change in §9.

**Suggested `CLAUDE.md` pointer to add:** *"Before building any feature, read `docs/discovery/00-requirements-synthesis.md` for consolidated user requirements and the current build/hold split."*

---

## 0. Preflight — verify before writing any code

1. **Leaked prototype credentials (OpenAI + Pinecone) rotated.** Standing P0; blocks safe repo work. Never commit secrets; `.env` in `.gitignore`.
2. **The June 10 engine package is present in this repo** and all six test suites pass — including the 2,000-case anti-inflation fuzz test. If the package is missing, regenerate before feature work.
3. **The five discovery syntheses are committed** to `docs/discovery/` so evidence links below resolve.

## 1. Hard constraints (user needs never override these)

- **Anti-inflation invariant:** the compliance gate can only **hold / downgrade / remove** codes. No code path raises an E/M level or adds a code as an adjudication action. Enforced in deterministic Python, never prompts. Fuzz test stays green on every change.
- **E/M level is an *output* of documented MDM facts, never a target.** Under-coding recovery (suggesting a higher level than the provider selected) is legitimate **only** with documentation evidence, and every suggestion carries evidence spans.
- **Provider-facing billing suggestions route through the engine pipeline** (CodingAgent → DocumentationIntegrityAgent → ComplianceGuardrailAgent → DenialRiskAgent). No prompt-only implementation. This is load-bearing: John has told at least two customers (Maura 7/24, Sherin 8/10) the suggestions are safe/conservative — only the deterministic pipeline makes that sentence true.
- **PHI boundary:** no real patient data (notes, transcripts, cards, labs) touches the system until the BAA and deployment layer (auth, encryption at rest, access controls) exist. Until then: **synthetic and de-identified data only.** LLM calls fail closed on schema/parse errors.
- **No patient steering; no revenue-motivated documentation prompts.** UI copy uses "capture documented, medically necessary care" — never "increase revenue" / up-billing framing.
- **Direct-contract hard gate:** any pathway that could charge or dun a patient must check the direct-contract flag first and hard-stop on match (see F4).
- **All Athena protocol knowledge lives in `athena_client.py`.** Mock-first (existing mock athenaNet harness); real integration gated on sandbox → Marketplace.
- **Human verification before persistence** everywhere a value enters a record: coder review for claims, staff confirmation for patient match/package, provider confirmation for extracted lab values. Never auto-select, never auto-write.

## 2. Who the users are & how they measure success

| Persona | People (evidence) | What success looks like to them | Metric to instrument |
|---|---|---|---|
| **Biller/coder** | Grace, Johanna, Edgar (8/6) | Claims arrive clean and complete; cards collected; self-pay recovered; **AI must not add review work** ("my gut is it's going to create more time for us… we have to review what you're sending in and the modifiers") | **Net coder minutes per claim** (must go down); telehealth denial rate (Optum/UHC); card submission completion rate |
| **Provider — RRM/fertility** | Sherin (8/10), Christa (8/6) | Copy-pasteable ICD/CPT candidates from the visit; A/P split to code granularity; coverage-aware ordering; clinical note untouched; labs viewable over time | **Provider minutes locating/splitting codes**; under-coding recovery rate |
| **Provider — family med** | Maura (7/24) | Coverage of a very wide coding surface ("hangnail to fertility awareness… a million different things"); trust that suggested codes are criteria-met and never over-billed | Same as above + suggestion acceptance rate |
| **Therapist (behavioral health)** | Scott (7/21) | *Out of billing scope* — time-based psychotherapy CPT is a different domain; revisit at first paying therapist org | — |

Thesis validation on record: under-billing confirmed independently by billers (8/6), Christa (8/6, "we're continually under-billing," 3–4 admin hrs per billable hr), and implicitly Sherin (8/10). Demand for the pipeline exists on **both sides** of the workflow.

---

## 3. Feature requirements (priority order)

### F1. Provider Billing Tab — *committed deliverable; demo promised to Sherin ~Aug 17–24*

**Demand:** Sherin explicit request (8/10); Maura positive with the strong-safety expectation (7/24); Christa frames value as "double-checking your work" (8/6); Grace's constraint binds it (no new coder burden).

**Behavioral spec (what users described):**
1. **Separate tab** (dollar-sign icon, Cosmos left nav). Same visit, same note content, reorganized into a billing reference view. **The clinical note is never altered** and goes to the EMR unchanged (Sherin: "clinical tool first").
2. **Inputs:** visit transcript + note (Cosmos already holds both = ⅔ of a VisitBundle). Demo runs on de-identified/synthetic encounters only.
3. **Output:** ICD candidates and CPT/E/M candidate, each carrying: code, plain description, **evidence span(s)** (verbatim quote + source/location), confidence, status (`suggested | held | removed`) with reason when not suggested, coverage note where known, and one-click **copy** text (plus copy-all, Athena-paste-friendly).
4. **A/P decomposition to code granularity:** lumped assessment/plan items are split so each documented condition carries its own code, with plan tasks/orders grouped under the justifying code (her examples: semaglutide instructions under obesity; lab orders under insulin resistance). This is the DenialRiskAgent's primary-dx-linkage check, rendered provider-side.
5. **Multi-code mapping with dedupe:** a finding that legitimately maps to multiple codes appears **once**, with alternates listed — never duplicate rows (Sherin's progesterone-deficiency example).
6. **Coverage-aware ordering:** lead with documented, covered conditions; never lead with a non-covered label (e.g., infertility) when documented underlying conditions are the billable reality. This is **ordering and labeling of legitimate evidence-backed codes — never adding codes** to reach coverage.
7. **E/M:** suggested level is computed from documented MDM facts (2-of-3, deterministic) and shown with the facts and evidence. If it exceeds the provider's selection, that is legitimate under-coding recovery — evidence displayed. **Time-based pathway is an open question (§8); build MDM-only now with a clean seam** (`pathway: "mdm_2of3"` field reserved for `"total_time"`).
8. **Audit framing (acceptance test):** the practice could hand a payer this view and defend every code on it.

**Draft output contract** (align with groundwork brief; adapter over existing ProviderCard):
```json
{
  "visit_id": "…", "generated_at": "…", "engine_version": "…",
  "em": {
    "suggested_level": "99214", "provider_selected_level": "99213",
    "pathway": "mdm_2of3",
    "mdm_facts": [{"element": "problems|data|risk", "finding": "…", "evidence_span": {"source": "note", "text": "…", "loc": "…"}}],
    "guardrail_actions": []
  },
  "icd_candidates": [{
    "code": "E66.9", "description": "…", "status": "suggested",
    "status_reason": null, "confidence": 0.92,
    "evidence_spans": [{"source": "note|transcript", "text": "…", "loc": "…"}],
    "coverage_note": "…", "alternates": ["E66.01"],
    "linked_plan_items": ["semaglutide 0.25 mg weekly"],
    "copy_text": "E66.9"
  }],
  "cpt_candidates": [ /* same shape */ ],
  "completeness": { "rulepack": "telehealth.v1", "results": [], "attestation_block": null }
}
```

**Acceptance criteria:** guardrail fuzz test green; **no candidate renders without ≥1 evidence span**; held/removed always carry a reason; dedupe verified (multi-mapped finding renders once); golden test on the demo encounter (26-y/o rectal bleeding: 99214 surfaced with evidence over selected 99213; patient-denied family-history code **rejected**; documented-but-unaddressed obesity **held**); zero mutation of the clinical note object.

---

### F2. Note Completeness Checker + Telehealth Attestation (first rule pack)

**Demand:** Grace, verbatim (8/6): "run something that… reviews their note and says, hey, you forgot this, you forgot this… and maybe they can override it." Root cause on record: Optum/UHC **denials because notes didn't clearly say it was a telehealth visit, how long, and where** (~16:00). Maura's copy-paste workflow corroborates notes reaching Athena missing structure (7/24).

**Behavioral spec:**
1. Deterministic **rule pack** evaluated over the note pre-submission; LLM used only to *locate* candidate spans, deterministic Python decides `present | missing | ambiguous`. Fail closed.
2. `telehealth.v1` rules: modality stated; visit duration stated; patient location; provider location; (consent language where payer-required — confirm list Aug 13).
3. Each result carries the found snippet (or none), a plain-language fix hint, and a **provider override** control; overrides require a reason and are logged → FeedbackAgent.
4. When all rules pass (or are overridden), emit a ready-to-insert **attestation header block** for the note top.
5. Runs provider-side *before* the claim reaches billers — this is how it reduces, not adds, coder minutes.

**Rule-result contract:**
```json
{"rule_id": "telehealth.duration", "status": "missing",
 "evidence": null, "fix_hint": "State total visit time in minutes.",
 "provider_override": {"overridden": false, "reason": null}}
```

**Acceptance criteria:** on synthetic telehealth notes with seeded gaps, the checker flags exactly the seeded gaps; **zero false "complete"** on missing duration or location; override without reason is rejected; attestation block renders only on pass/override.

---

### F3. Direct-Contract Guard — *hard gate, small, build now*

**Demand:** Grace (8/6, ~22:45): "If we could somehow ensure that we know if it's a direct-contract patient so that we never… charge them a self-pay fee." Encoded as a hard gate in `CLAUDE.md`.

**Behavioral spec:** a standalone deterministic module every charging/dunning pathway must call before acting:
```python
check_direct_contract(patient_ref) -> {"is_direct_contract": bool, "source": "practice_list.v1", "checked_at": "…"}
```
- Blocking semantics: `is_direct_contract == True` → hard stop; the caller cannot proceed to any self-pay charge or outreach. Unknown/missing list → **fail closed** (treat as blocked, surface for human review).
- List source of truth is an Aug 13 question (§8); build against a versioned local list file + mock now; the gate semantics won't change, only the source adapter.

**Acceptance:** unit tests for match, non-match, missing-list fail-closed; F5 (self-pay chase) integration test proving a direct-contract patient can never enter a chase sequence.

### F4. Note Section Splitter (Athena-shaped) — *zero-gate interim, two requesters*

**Demand:** Christa (8/6): pastes note into HPI then hand-cuts ROS/assessment/plan into sections. Maura (7/24): copy-paste into chief complaint/subjective, assessments added separately. Full auto-populate is gated on Athena sandbox (F7); this interim kills most of the manual chopping **today**.

**Behavioral spec:** deterministic splitter over the generated note → labeled blocks matching Athena's paste targets (Chief Complaint, HPI/Subjective, ROS, Assessment, Plan), each with its own copy button; content moved **unaltered** — sectioning/formatting only (clinical-first principle). Unmapped content lands in a visible "unplaced" block, never dropped.

**Acceptance:** round-trip test — concatenating the blocks reproduces the source note text exactly (modulo whitespace); goldens across the existing note-type templates.

### F5. Self-Pay Chase Agent — *step-0; data contract now, mechanics after Aug 13*

**Demand:** billing team, unprompted (8/6). **The Aug 13 follow-up is expected to define mechanics — do not build behavior in advance; it will be invalidated.**

**What's safe to lock tonight:** the shape and the safety rails. **F5 and F9b run on the same follow-up-agent chassis** (deterministic detector → work queue → approval-gated sequence state machine → contact log → metrics). Build the chassis once under F9; F5 then becomes a second queue type on it, differing in trigger (balance vs. missing card), gate semantics (F3 is *blocking* here, review-routing there), and templates.
```json
{"balance_id": "…", "patient_ref": "…", "amount_cents": 12500, "aging_days": 41,
 "direct_contract_check": {"is_direct_contract": false, "checked_at": "…"},
 "sequence_state": "draft", "contact_log": []}
```
- State machine: `draft → staff_approved → sent → …` — **every patient contact requires explicit staff approval** (human-in-the-loop is the workflow), tone/cadence/escalation are Aug 13 inputs.
- F3 gate at sequence entry, enforced in code, with the integration test above.
- Mock data: synthetic balances generator, consistent with the groundwork brief's mock plan.

### F6. Coder Review Surface refinements (BillerWorksheet / CoderReview)

**Demand:** Grace's net-minutes constraint (8/6); the coder-side tuning loop was promised to Maura on record (7/24); the "brittle hair" precedent (Grace, ~18:30) — she uses documented provider observations to support coverage-appropriate diagnoses.

**Requirements from feedback:** (1) queue pre-sorted clean vs. needs-review so trivial claims cost near-zero minutes; (2) **modifier work surfaced explicitly** (her stated review worry — mechanics detail arrives Aug 13); (3) surface *documented-but-uncoded findings* to the coder as evidence-linked candidates (brittle-hair pattern — coder judges, engine never auto-adds); (4) keep the timer and structured reason codes — the timer is the instrument for the net-coder-minutes metric; capture per-claim review time from day one to compare against tomorrow's month-end baseline.

### F7. Athena Note Auto-Populate — *gated: sandbox → Marketplace*

**Demand:** Christa (8/6), Maura's friction corroborates (7/24). **Gate:** encounter-section write API is *unverified* (repo knowledge covers the insurance write only). No build beyond F4 until sandbox access exists; then validate payload shapes before promising behavior. All protocol code in `athena_client.py`; extend the mock harness first.

### F8. Longitudinal Lab Tracker — *step-0, gated (PHI + Athena read)*

**Demand:** Christa (8/6) — replaces shadow spreadsheets; fertility segment ("their whole thing is labs"); Sandy same pattern. **Rails:** every extracted value requires provider confirmation before entering a persisted series; analyses are review surfaces, never auto-written to the chart. **Tonight at most:** data contract (analyte, value, unit, date, source, provider_verified) + synthetic lab fixtures, mirroring the CardCapture synthetic-cards pattern. Real labs blocked on BAA + deployment layer + Athena read scope.

### F9. Insurance-Data Chase Agent — *the billers' #1 stated pain; zero-gate core builds now*

**Demand:** Grace (8/6): card collection is the team's self-declared top pain — "I would be ecstatic about the cards… It's my mantra." The validated finding: **the bottleneck is patient submission, not OCR performance.** CardCapture's downstream (extract → staff verify → idempotent Athena write) is already built; what's missing is the upstream system that gets the card *to* it and takes the chasing off the billers. Johanna's Athena-sync ask (8/6) is the closing leg of the same loop.

**What it is:** a **biller-facing worklist plus follow-up agent** that (a) detects patients/encounters with missing, expired, or inactive insurance data, (b) runs a staff-approved reminder sequence pointing the patient at the existing CardCapture submission link, and (c) auto-closes items when a verified card lands — with chase metrics instrumented from day one.

**F9a — Missing-insurance detector** (deterministic; build now against the mock athenaNet harness). Each trigger is a pure-Python rule with tests:
- new/upcoming appointment with no insurance package on file
- coverage marked expired / plan-year rollover
- eligibility (271) returned INACTIVE or payer-timeout — *the mock harness already emits ~10% INACTIVE / 5% timeout by design; this trigger is demo-ready today*
- staff manual flag ("patient says new insurance")
- (later) claim-denial reason codes mapping to coverage/ID problems, fed from DenialRiskAgent
- **F3 consult at detection:** a direct-contract patient likely shouldn't be card-chased at all (the practice doesn't bill insurance for them) — confirm Aug 13 (§8 Q11). Until answered, F9a routes direct-contract matches to a **human-review bucket**, never the auto-sequence.

Queue-item contract:
```json
{"chase_id": "…", "patient_ref": "…",
 "trigger": "no_card_on_file|expired|eligibility_inactive|eligibility_timeout|manual|denial_coverage",
 "detected_at": "…", "appointment_at": "…",
 "priority": "computed from appointment proximity",
 "direct_contract": {"is_direct_contract": false, "routed_to_review": false},
 "sequence_state": "open", "attempts": [], "resolution": null}
```

**F9b — Outreach sequence** (shared chassis with F5). State machine: `open → staff_approved → sent → reminded(n) → submitted → verified_closed | escalated_to_staff | unreachable`. Rails: **every outbound message is staff-approved before send** (human-in-the-loop is the workflow); the message contains a secure link to the existing CardCapture upload page and **never any insurance/card data**; every attempt is logged (channel, timestamp, template id). Cadence, tone, templates, and channel/consent policy (email vs. SMS vs. portal) are **Aug 13 inputs — ship them as configuration, not code.**

**F9c — Completion loop.** Patient submits via the link → existing CardCapture pipeline (extraction → staff verification → idempotent Athena write incl. `ADMIN_INSURANCECARD` attach → eligibility re-check) → on verified write, the queue item auto-closes with resolution metadata, and re-running the eligibility trigger confirms ACTIVE. **Metrics from day one:** cards outstanding, attempts-per-completion, completion rate, median time-to-card, staff minutes per card — tomorrow's month-end numbers become the baseline these are measured against.

**Gates & data:** build and demo entirely against the mock athenaNet harness + synthetic patients (zero PHI, zero Athena credentials — the harness already seeds one fictional patient per synthetic card). Real messaging and deployment are gated on the deployment layer/BAA; real Athena on the sandbox → Marketplace clock (F7).

**Acceptance criteria:** each detector trigger fires on seeded mock states and stays silent on clean states; a direct-contract patient can never enter the auto-sequence (integration test with F3 — routed to review instead); no outbound message body contains an insurance value or card field; end-to-end mock demo runs: INACTIVE 271 → queue item → staff-approved outreach → simulated patient submission → CardCapture verify → mock Athena write → item auto-closes → metrics emitted.

**Why build the core tonight:** tomorrow's meeting is *with the billers*, about *their* #1 pain, in the one week their real baseline numbers exist. Walking in with the mock demo running converts the interview from Q&A into a design review of the exact feature they asked for — the strongest validation artifact available.

### Out of scope (recorded, not built)

Behavioral-health/psychotherapy coding (time-based CPT — revisit at first paying therapist org); session-prep bot billing angle (Scott, non-billing, PHI-gated); payers beyond Medicare NGS J-K remain out of engine scope — note that MCD's live denials are **commercial** (Optum/UHC), so F2 attacks them via documentation completeness rather than payer-rule adjudication. Commercial payer rules = post-validation roadmap, do not build now.

---

## 4. Cross-cutting behavioral requirements (apply to every feature)

1. **Evidence spans on everything** a user sees suggested — no span, no render.
2. **Short outputs.** Scott (7/21): ChatGPT's prep output was "actually a little bit too much." Default to compact; expand on demand. Set explicit length budgets in prompts and views.
3. **Override affordances with logged reasons** wherever the system asserts something (completeness rules, coder decisions, lab values) — overrides feed the FeedbackAgent.
4. **Copy-paste-ability** as a first-class affordance (per-item and bulk, Athena-friendly formatting).
5. **Verification before persistence** (constraint §1) — no auto-select, no auto-write, anywhere.
6. **Language discipline in UI copy and prompts:** "capture documented, medically necessary care"; suggested-code microcopy may say criteria-checked *only because* the deterministic pipeline checked them.
7. **Determinism boundary:** LLMs extract; Python adjudicates. Any new check lands on the deterministic side with tests.
8. **Instrument the user metrics from day one:** net coder minutes/claim, provider minutes-to-codes, telehealth denial flags caught pre-submission, suggestion acceptance rate.

## 5. Build BEFORE Aug 13 (tonight/tomorrow-morning order)

1. **Preflight (§0)** — keys, engine package, six suites green (fuzz included).
2. **F3 direct-contract guard** module + fail-closed tests (pulled forward: F9a depends on it).
3. **F9 core — the demo for tomorrow:** F9a detector + queue, the shared F5/F9 follow-up chassis (approval-gated state machine, contact log), F9c closure hook into the existing proxy flow, and the metrics counters — all against the mock athenaNet harness, ending in a runnable end-to-end demo script for the biller meeting.
4. **F1 adapter + goldens:** engine output → `BillingTabView` JSON (deterministic transform: dedupe, coverage ordering, copy_text), golden test on the rectal-bleeding demo encounter. Serves the Sherin demo clock (~Aug 17–24) no matter what happens tomorrow.
5. **F2 checker skeleton + `telehealth.v1`** with seeded synthetic notes. Tomorrow can only *add* rules — the frame is stable.
6. **F4 section splitter** + round-trip goldens.
7. **F1 React Billing Tab** consuming `BillingTabView` (frontends need regeneration anyway) — synthetic data only, dollar-sign nav slot, per-code copy.
8. **F5 as a second queue type on the F9 chassis** — contract + synthetic-balance generator only; mechanics stop until Aug 13.

Each item ships as a complete, tested unit before the next (standing working rhythm).

## 6. HOLD until after Aug 13 (building now = rework)

Self-pay chase mechanics/tone/escalation; **card-chase cadence, tone, templates, channel & consent policy, priority weights, and the direct-contract chase policy** (F9's §8 questions — the F9 *core* is build-now, only its configuration waits); modifier workflow specifics (explicitly deferred by Grace to this meeting); coding/bundling mechanics; the direct-contract **list source** adapter; baseline-metric schemas (capture the real month-end numbers first — one-time window); time-based E/M pathway (ask first, seam already reserved); anything requiring Athena payload shapes (sandbox first).

## 7. Traceability index (requirement ← who/when)

| Requirement | Source |
|---|---|
| Billing tab, copy-paste ICD/CPT, A/P decomposition, dedupe, coverage-aware ordering, clinical-note-untouched | Sherin 8/10 |
| Suggestions must be criteria-met / never over-billing; coder-side tuning loop; wide family-med surface; minutes↔codes | Maura 7/24 |
| Completeness checker w/ override; telehealth attestation (modality/duration/location); direct-contract guard; self-pay chase; net-coder-minutes; brittle-hair pattern; card-submission bottleneck → **insurance-data chase agent (F9)**; Athena card sync | Billers 8/6 |
| Note section splitting; longitudinal labs w/ provider verification; "double-checking your work"; under-billing + 3–4:1 admin ratio | Christa 8/6 |
| Short outputs; org-pays buyer; behavioral-health scope boundary | Scott 7/21 |

## 8. Open questions the Aug 13 interview answers (do not pre-commit code)

1. Commercial: will the practice pay (anchor $59/user/mo; economic buyer org vs. provider)?
2. Baseline metrics from month-end close: coder minutes/claim, denial counts by reason, cards chased/month, self-pay balances outstanding.
3. Self-pay chase mechanics: sequencing, tone, escalation, who approves sends.
4. Modifier workflow + coding/bundling mechanics (deferred from 8/6).
5. Direct-contract list: where it lives, who maintains it.
6. Athena screen-share: real section names/paste targets (feeds F4 labels, F7).
7. Time-coded vs. MDM-coded claims — does the team re-level on time? (feeds F1 seam).
8. What the billing team has heard about Athena's own AI features.
9. **Card chase today (F9 baseline):** who does it, on what channels, how many touches, minutes per card, how many cards outstanding right now?
10. **Patient-contact channels & consent (F9b config):** can the practice text patients? Is portal messaging available? What tone/cadence do they use today?
11. **Direct-contract chase policy (F9a):** should direct-contract patients ever be card-chased, or suppressed entirely? (Routed to human review until answered.)

## 9. Changelog

- **v1.0 (Aug 12, 2026):** initial consolidation of five conversations (Jul 21 – Aug 10).
- **v1.1 (Aug 12, 2026):** owner review caught that v1.0 parked the billers' #1 pain as a placeholder. F9 rebuilt as the **insurance-data chase agent** (F9a detector / F9b outreach chassis / F9c completion loop) with contracts and acceptance criteria; its zero-gate core elevated to build-now (§5 items 2–3) targeting a mock end-to-end demo for the Aug 13 meeting; F5 restated as a second queue type on the shared chassis; §8 Q9–Q11 added. *Next revision expected Aug 13–14 after the biller follow-up: fill §8 answers, promote/adjust F5, F6-modifiers, F9 configuration; record time-pathway decision.*
