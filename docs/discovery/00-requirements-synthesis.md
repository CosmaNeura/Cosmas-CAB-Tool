# CAB User Requirements Synthesis — What Users Need & How Features Should Work

**Version:** 1.1 · **Date:** August 12, 2026 · **Owner:** John Clemente
**Evidence base:** Five customer conversations — Scott Weeman/Fiat Health (Jul 21),
Maura Buete/MCD (Jul 24), MCD billing team: Grace Targonski, Johanna Adriano,
Edgar Morgan (Aug 6), Christa Thornberry/MCD (Aug 6), Sherin George/MCD (Aug 10).
A sixth conversation (biller follow-up) happened **Aug 13** — see
`2026-08-13-mcd-biller-followup.md`, which records its answers to §8 and three
new requirements this version predates.

**Relationship to other docs:** `/CLAUDE.md` is authoritative for hard
constraints. This file consolidates *user-voiced requirements* into build-ready
form.

> **v1.1 status note (added on commit, Aug 17):** this document is preserved as
> written on Aug 12. Its §0 preflight assumes repo contents that do not all
> exist here — see "Preflight reality check" immediately below. Its §5 build
> ordering was written for the Aug 13 meeting and has been overtaken by events.
> §1–§4 and §7 remain current and load-bearing.

> **Platform clarification (Aug 18):** references below to "the BAA" mean every
> applicable agreement and service-specific data-handling gate. A BAA with the
> pilot practice does not by itself authorize disclosure to a cloud or model
> service. The Azure pilot's live extraction target is now a private Azure
> OpenAI deployment, but real chart text remains blocked until the Microsoft
> agreement/BAA is verified to cover that service and US data zone and the
> deployment, logging, retention, and access-control acceptance checks pass.

## Preflight reality check (Aug 17)

§0 below asserts three things. Verified state:

1. Leaked prototype credentials — **resolved.** All prototype scripts read keys
   from the environment; no credentials in this repo's git history.
2. "The June 10 engine package is present and all six test suites pass —
   including the 2,000-case anti-inflation fuzz test." — **partly false as
   written.** The engine package is present and its suite passes, but there was
   no 2,000-case fuzz test; `prove_no_upcode.py` was a single-scenario proof.
   A real fuzz suite (`tests/test_guardrail_fuzz.py`) was added Aug 17.
3. "The five discovery syntheses are committed to `docs/discovery/`" — **was
   false.** Partially remedied Aug 17; see `README.md` for what is still
   Drive-only.

## 0. Preflight — verify before writing any code

1. Leaked prototype credentials (OpenAI + Pinecone) rotated. Standing P0. Never
   commit secrets; `.env` in `.gitignore`.
2. The engine package is present and its test suites pass — including the
   anti-inflation fuzz test.
3. The discovery syntheses are committed to `docs/discovery/` so evidence links
   resolve.

## 1. Hard constraints (user needs never override these)

- **Anti-inflation invariant:** the compliance gate can only **hold / downgrade /
  remove** codes. No code path raises an E/M level or adds a code as an
  adjudication action. Enforced in deterministic Python, never prompts. Fuzz test
  stays green on every change.
- **E/M level is an *output* of documented MDM facts, never a target.**
  Under-coding recovery (suggesting a higher level than the provider selected) is
  legitimate **only** with documentation evidence, and every suggestion carries
  evidence spans.
- **Provider-facing billing suggestions route through the engine pipeline**
  (CodingAgent → DocumentationIntegrityAgent → ComplianceGuardrailAgent →
  DenialRiskAgent). No prompt-only implementation. This is load-bearing: John has
  told at least two customers (Maura 7/24, Sherin 8/10) the suggestions are
  safe/conservative — only the deterministic pipeline makes that sentence true.
- **PHI boundary:** no real patient data (notes, transcripts, cards, labs)
  touches the system until the deployment layer (auth, encryption at rest,
  access controls) exists and every receiving service is covered by its
  applicable executed agreement/BAA and approved configuration. Until then:
  **synthetic and de-identified data only.** LLM calls fail closed on
  schema/parse errors.
- **No patient steering; no revenue-motivated documentation prompts.** UI copy
  uses "capture documented, medically necessary care" — never "increase revenue"
  / up-billing framing.
- **Direct-contract hard gate:** any pathway that could charge or dun a patient
  must check the direct-contract flag first and hard-stop on match (F3).
- **All Athena protocol knowledge lives in `athena_client.py`.** Mock-first; real
  integration gated on sandbox → Marketplace.
- **Human verification before persistence** everywhere a value enters a record:
  coder review for claims, staff confirmation for patient match/package,
  provider confirmation for extracted lab values. Never auto-select, never
  auto-write.

## 2. Who the users are & how they measure success

| Persona | People (evidence) | What success looks like to them | Metric to instrument |
|---|---|---|---|
| **Biller/coder** | Grace, Johanna, Edgar (8/6) | Claims arrive clean and complete; cards collected; self-pay recovered; **AI must not add review work** (*"my gut is it's going to create more time for us… we have to review what you're sending in and the modifiers"*) | **Net coder minutes per claim** (must go down); telehealth denial rate (Optum/UHC); card submission completion rate |
| **Provider — RRM/fertility** | Sherin (8/10), Christa (8/6) | Copy-pasteable ICD/CPT candidates from the visit; A/P split to code granularity; coverage-aware ordering; clinical note untouched; labs viewable over time | **Provider minutes locating/splitting codes**; under-coding recovery rate |
| **Provider — family med** | Maura (7/24) | Coverage of a very wide coding surface (*"hangnail to fertility awareness… a million different things"*); trust that suggested codes are criteria-met and never over-billed | Same as above + suggestion acceptance rate |
| **Therapist (behavioral health)** | Scott (7/21) | *Out of billing scope* — time-based psychotherapy CPT is a different domain; revisit at first paying therapist org | — |

Thesis validation on record: under-billing confirmed independently by billers
(8/6), Christa (8/6, *"we're continually under-billing,"* 3–4 admin hrs per
billable hr), and implicitly Sherin (8/10). Demand for the pipeline exists on
**both sides** of the workflow.

## 3. Feature requirements (priority order)

### F1. Provider Billing Tab — *committed deliverable; demo promised to Sherin ~Aug 17–24*

**Demand:** Sherin explicit request (8/10); Maura positive with the strong-safety
expectation (7/24); Christa frames value as "double-checking your work" (8/6);
Grace's constraint binds it (no new coder burden).

**Behavioral spec (what users described):**

1. **Separate tab** (dollar-sign icon, Cosmos left nav). Same visit, same note
   content, reorganized into a billing reference view. **The clinical note is
   never altered** and goes to the EMR unchanged (Sherin: "clinical tool first").
2. **Inputs:** visit transcript + note. Demo runs on de-identified/synthetic
   encounters only.
3. **Output:** ICD candidates and CPT/E/M candidate, each carrying: code, plain
   description, **evidence span(s)** (verbatim quote + source/location),
   confidence, status (suggested | held | removed) with reason when not
   suggested, coverage note where known, and one-click **copy** text (plus
   copy-all, Athena-paste-friendly).
4. **A/P decomposition to code granularity:** lumped assessment/plan items are
   split so each documented condition carries its own code, with plan
   tasks/orders grouped under the justifying code (her examples: semaglutide
   instructions under obesity; lab orders under insulin resistance). This is the
   DenialRiskAgent's primary-dx-linkage check, rendered provider-side.
5. **Multi-code mapping with dedupe:** a finding that legitimately maps to
   multiple codes appears **once**, with alternates listed — never duplicate rows
   (Sherin's progesterone-deficiency example).
6. **Coverage-aware ordering:** lead with documented, covered conditions; never
   lead with a non-covered label (e.g., infertility) when documented underlying
   conditions are the billable reality. This is **ordering and labeling of
   legitimate evidence-backed codes — never adding codes** to reach coverage.
7. **E/M:** suggested level is computed from documented facts and shown with the
   facts and evidence. If it exceeds the provider's selection, that is legitimate
   under-coding recovery — evidence displayed. *(Time-based pathway was an open
   question here; resolved Aug 17 — build it. See the Aug 13 doc §3 Q7.)*
8. **Audit framing (acceptance test):** the practice could hand a payer this view
   and defend every code on it.

**Draft output contract** (adapter over the existing ProviderCard):

```json
{
  "visit_id": "…", "generated_at": "…", "engine_version": "…",
  "em": {
    "suggested_level": "99214", "provider_selected_level": "99213",
    "pathway": "mdm_2of3",
    "mdm_facts": [{"element": "problems|data|risk", "finding": "…",
                   "evidence_span": {"source": "note", "text": "…", "loc": "…"}}],
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
  "completeness": { "rulepack": "telehealth.v1", "results": [],
                    "attestation_block": null }
}
```

**Acceptance criteria:** guardrail fuzz test green; **no candidate renders
without ≥1 evidence span**; held/removed always carry a reason; dedupe verified
(multi-mapped finding renders once); golden test on the demo encounter (26-y/o
rectal bleeding: 99214 surfaced with evidence over selected 99213; patient-denied
family-history code **rejected**; documented-but-unaddressed obesity **held**);
zero mutation of the clinical note object.

### F2. Note Completeness Checker + Telehealth Attestation (first rule pack)

**Demand:** Grace, verbatim (8/6): *"run something that… reviews their note and
says, hey, you forgot this, you forgot this… and maybe they can override it."*
Root cause on record: Optum/UHC **denials because notes didn't clearly say it was
a telehealth visit, how long, and where.** Maura's copy-paste workflow
corroborates notes reaching Athena missing structure (7/24). Reconfirmed 8/13
with placement specified (top-of-note blurb) and a 24–48 hour billability window.

**Behavioral spec:**

1. Deterministic **rule pack** evaluated over the note pre-submission; LLM used
   only to *locate* candidate spans, deterministic Python decides
   present | missing | ambiguous. Fail closed.
2. `telehealth.v1` rules: modality stated; visit duration stated; patient
   location; provider location; consent language where payer-required.
3. Each result carries the found snippet (or none), a plain-language fix hint,
   and a **provider override** control; overrides require a reason and are
   logged → FeedbackAgent.
4. When all rules pass (or are overridden), emit a ready-to-insert
   **attestation header block** for the note top.
5. Runs provider-side *before* the claim reaches billers — this is how it
   reduces, not adds, coder minutes.

**Rule-result contract:**

```json
{"rule_id": "telehealth.duration", "status": "missing",
 "evidence": null, "fix_hint": "State total visit time in minutes.",
 "provider_override": {"overridden": false, "reason": null}}
```

**Acceptance criteria:** on synthetic telehealth notes with seeded gaps, the
checker flags exactly the seeded gaps; **zero false "complete"** on missing
duration or location; override without reason is rejected; attestation block
renders only on pass/override.

### F3. Direct-Contract Guard — *hard gate, small, build now*

**Demand:** Grace (8/6): *"If we could somehow ensure that we know if it's a
direct-contract patient so that we never… charge them a self-pay fee."*

**Behavioral spec:** a standalone deterministic module every charging/dunning
pathway must call before acting:

```
check_direct_contract(patient_ref) -> {"is_direct_contract": bool,
                                       "source": "practice_list.v1",
                                       "checked_at": "…"}
```

- Blocking semantics: `is_direct_contract == True` → hard stop. Unknown/missing
  list → **fail closed** (treat as blocked, surface for human review).
- List source of truth is still open (Aug 13 did not answer); build against a
  versioned local list file + mock. The gate semantics won't change, only the
  source adapter.

**Acceptance:** unit tests for match, non-match, missing-list fail-closed; F5
integration test proving a direct-contract patient can never enter a chase
sequence.

### F4. Note Section Splitter (Athena-shaped) — *zero-gate interim, two requesters*

**Demand:** Christa (8/6) pastes note into HPI then hand-cuts ROS/assessment/plan
into sections. Maura (7/24): copy-paste into chief complaint/subjective,
assessments added separately. Full auto-populate is gated on Athena sandbox (F7);
this interim kills most of the manual chopping today.

**Behavioral spec:** deterministic splitter over the generated note → labeled
blocks matching Athena's paste targets (Chief Complaint, HPI/Subjective, ROS,
Assessment, Plan), each with its own copy button; content moved **unaltered** —
sectioning/formatting only. Unmapped content lands in a visible "unplaced" block,
never dropped.

**Acceptance:** round-trip test — concatenating the blocks reproduces the source
note text exactly (modulo whitespace).

### F5. Self-Pay Chase Agent — *step-0; contract only, mechanics still unanswered*

**Demand:** billing team, unprompted (8/6). Aug 13 did **not** define mechanics —
they remain on hold. F5 and F9b share one follow-up chassis (deterministic
detector → work queue → approval-gated sequence state machine → contact log →
metrics). Every patient contact requires explicit staff approval. F3 gate at
sequence entry.

### F6. Coder Review Surface refinements (BillerWorksheet / CoderReview)

**Demand:** Grace's net-minutes constraint (8/6); the coder-side tuning loop was
promised to Maura on record (7/24); the "brittle hair" precedent (Grace) — she
uses documented provider observations to support coverage-appropriate diagnoses.

**Requirements:** (1) queue pre-sorted clean vs. needs-review so trivial claims
cost near-zero minutes; (2) **modifier work surfaced explicitly** — her stated
review worry; mechanics still deferred as of 8/13; (3) surface
*documented-but-uncoded findings* to the coder as evidence-linked candidates
(brittle-hair pattern — coder judges, engine never auto-adds); (4) keep the timer
and structured reason codes — the timer is the instrument for the
net-coder-minutes metric.

### F7. Athena Note Auto-Populate — *gated: sandbox → Marketplace*

**Gate:** encounter-section write API is unverified. No build beyond F4 until
sandbox access exists. All protocol code in `athena_client.py`.

### F8. Longitudinal Lab Tracker — *step-0, gated (PHI + Athena read)*

**Demand:** Christa (8/6) — replaces shadow spreadsheets; fertility segment
(*"their whole thing is labs"*). **Rails:** every extracted value requires
provider confirmation before entering a persisted series. Real labs blocked on
the applicable BAAs/service approvals + deployment layer + Athena read scope.

### F9. Insurance-Data Chase Agent — *the billers' #1 stated pain*

**Demand:** Grace (8/6): *"I would be ecstatic about the cards… It's my
mantra."* **The bottleneck is patient submission, not OCR performance.**
300–500 claims held monthly. F9a missing-insurance detector; F9b staff-approved
outreach sequence (never contains card data); F9c completion loop closing on
verified write. Aug 13 added: card images must land in the right Athena
*encounter*; team estimates automation removes 80–90% of procurement tasks.
**This workstream lives in the CardCapture / athenaNet repo, not the engine
repo.**

### F10. Procedure / add-on code proposals — *new, from Aug 13*

**Demand:** Grace (8/13) — CPT **99058** on rapid-access visits; missing it is
lost revenue. Prerequisite: the coding agent has never proposed a
`PROCEDURE`-kind code. 99058 carries a CMS MUE of 0 with a clinical
date-of-service edit, so it must surface as a provider-confirmable candidate with
evidence and never auto-capture.

### Out of scope (recorded, not built)

Behavioral-health/psychotherapy coding (time-based psychotherapy CPT — revisit at
first paying therapist org); session-prep bot billing angle (Scott, non-billing,
PHI-gated); payers beyond Medicare NGS J-K remain out of engine scope — note that
MCD's live denials are **commercial** (Optum/UHC), so F2 attacks them via
documentation completeness rather than payer-rule adjudication. Commercial payer
rules = post-validation roadmap.

## 4. Cross-cutting behavioral requirements (apply to every feature)

1. **Evidence spans on everything** a user sees suggested — no span, no render.
2. **Short outputs.** Scott (7/21): ChatGPT's prep output was *"actually a little
   bit too much."* Default to compact; expand on demand.
3. **Override affordances with logged reasons** wherever the system asserts
   something — overrides feed the FeedbackAgent.
4. **Copy-paste-ability** as a first-class affordance (per-item and bulk,
   Athena-friendly formatting).
5. **Verification before persistence** — no auto-select, no auto-write, anywhere.
6. **Language discipline in UI copy and prompts:** "capture documented, medically
   necessary care"; suggested-code microcopy may say criteria-checked *only
   because* the deterministic pipeline checked them.
7. **Determinism boundary:** LLMs extract; Python adjudicates.
8. **Instrument the user metrics from day one:** net coder minutes/claim,
   provider minutes-to-codes, telehealth denial flags caught pre-submission,
   suggestion acceptance rate.

## 5. Build order

*Written Aug 12 for the Aug 13 meeting; superseded. Current engine-repo order as
executed Aug 17: repo foundations → guardrail fuzz suite + offline-mode honesty →
procedure codes + 99058 (F10) → E/M time pathway → completeness checker (F2) →
BillingTabView adapter (F1).*

## 6. HOLD until the open questions are answered

Self-pay chase mechanics/tone/escalation; card-chase cadence, tone, templates,
channel & consent policy, priority weights, and direct-contract chase policy;
modifier workflow specifics (deferred again on 8/13); the direct-contract **list
source** adapter; baseline-metric schemas (the month-end window was missed —
re-ask); anything requiring Athena payload shapes (sandbox first).

## 7. Traceability index (requirement ← who/when)

| Requirement | Source |
|---|---|
| Billing tab, copy-paste ICD/CPT, A/P decomposition, dedupe, coverage-aware ordering, clinical-note-untouched | Sherin 8/10 |
| Suggestions must be criteria-met / never over-billing; coder-side tuning loop; wide family-med surface; minutes↔codes; time-coded televisits | Maura 7/24 |
| Completeness checker w/ override; telehealth attestation (modality/duration/location); direct-contract guard; self-pay chase; net-coder-minutes; brittle-hair pattern; card-submission bottleneck → insurance-data chase agent (F9); Athena card sync | Billers 8/6 |
| Note section splitting; longitudinal labs w/ provider verification; "double-checking your work"; under-billing + 3–4:1 admin ratio | Christa 8/6 |
| Short outputs; org-pays buyer; behavioral-health scope boundary | Scott 7/21 |
| CPT 99058 (F10); top-of-note billing blurb; 24–48h telehealth billability window; Athena derives billing slip from note | Billers 8/13 |

## 8. Open questions

See `2026-08-13-mcd-biller-followup.md` §3 for the status of all eleven after the
follow-up. Summary: Q7 resolved (build the time pathway). Q1, Q2, Q3, Q4, Q5,
Q8, Q9, Q10, Q11 remain open; Q6 partially answered. **The month-end baseline
window (Q2, Q9) was missed and must be re-asked.**

## 9. Changelog

- **v1.0 (Aug 12, 2026):** initial consolidation of five conversations.
- **v1.1 (Aug 12, 2026):** F9 rebuilt as the insurance-data chase agent with
  contracts and acceptance criteria; F5 restated as a second queue type on the
  shared chassis; §8 Q9–Q11 added.
- **Aug 17, 2026 (committed to repo):** preflight reality check added; Aug 13
  follow-up cross-referenced; F10 added; time-pathway decision recorded; §5
  superseded by the executed order. Content of §1–§4 and §7 unchanged except the
  Aug 13 additions to §7.
