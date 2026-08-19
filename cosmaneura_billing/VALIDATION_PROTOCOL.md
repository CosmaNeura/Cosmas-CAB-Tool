# Validation protocol — under-coding recovery rate (certified-coder gold standard)

This protocol measures how much legitimately-earned revenue the CosmaNeura billing
engine recovers from under-coded encounters, and — just as important — proves it
does not introduce over-coding. It is written so a certified coder, a compliance
officer, and an engineer can each follow their part. Parameters you should confirm
before fieldwork are collected at the end under "Open parameters."

## 0. Phasing (given your current constraints)

You don't have two certified coders yet, and the encounter count and source are
still TBD. That doesn't stall the work — it changes the order. Run it in two phases.

**Phase 0 — now, no certified coders, minimal data.** The goal is a directional
read and a head start on the gold set, explicitly labeled provisional:

- *Shadow mode.* Run the engine on whatever de-identified encounters you can get
  and log, per encounter, the submitted code, the engine's proposed code, and the
  engine's evidence + audit trail. This both builds the corpus you'll later
  adjudicate and immediately shows the distribution of suggested changes (how often
  the engine proposes a higher level, and the dollar spread). It is an opportunity
  signal, not a validated rate.
- *Provider face-validity review.* Have one or two pilot physicians eyeball a small
  batch (25–40) of engine outputs for clinical sensibility. Cheap, and it catches
  obvious failure modes early.
- *One contract coder for an interim gold.* Standing up one credentialed coder is
  far easier than two; use the single-coder fallback in Section 6 to produce an
  interim gold on a subsample and report a provisional recovery rate with wider
  error bars.
- Do not make deployment or external claims off Phase 0 numbers — label them
  internal/provisional.

**Phase 1 — the defensible study (Sections 4 onward), once you have coder capacity
and a sample source.** Two credentialed coders, blind dual-coding plus adjudication,
full metrics and acceptance thresholds. This is the number you put in front of a
CFO or an investor.

**Sourcing, since both are open.** Coders: the AAPC and AHIMA directories list
credentialed coders (CPC/CCS) available for contract/per-chart work, RCM and
coding-service vendors will do chart review, and your pilot group may have a coder
or biller you can engage for the over-read — you need only one to begin Phase 0.
Encounters: the cleanest source is the pilot group itself, de-identified under your
existing BAA (with the data-use provision in Section 5); pull ~50 first to estimate
variance, then scale toward the Section 4 target. Synthetic claims (DE-SynPUF) carry
no clinical notes, so they exercise the pipeline but cannot measure under-coding —
the recovery study needs real documentation.

## 1. Objective

Estimate, on real (de-identified) pilot encounters, three things:

1. the **under-coding recovery rate** — the share of encounters where the
   documentation supports a higher-value, defensible code set than what was
   originally submitted, and the dollars attached to that gap;
2. the engine's **coding accuracy** against an independent certified-coder gold
   standard (E/M level and diagnosis capture); and
3. the engine's **safety** — that it essentially never proposes a code above what
   the gold standard supports (the guardrail's core promise).

The deliverable is a number you can defend to a pilot CFO and to investors: "on N
encounters, the engine recovered $X per encounter of documented, medically-necessary
revenue, agreed with certified coders at Y%, and over-coded in Z% of cases."

## 2. Definitions

A code set is **supported** when the documentation (intake + transcript + signed
note) substantiates it under the 2021+ E/M rules and ICD-10 guidelines. The
**gold standard** for an encounter is the code set assigned by independent
certified coders working from the documentation alone and reconciled by
adjudication (Section 6). **Under-coding** is a submitted level below the gold
level; **over-coding** is above it. **Recovery** is the defensible difference
between the gold-supported code set and what was originally submitted — never a
code the documentation doesn't support.

## 3. Metrics

Primary:

- **Under-coding recovery rate** = (encounters where gold E/M level > submitted
  level) ÷ (total encounters). Report with a 95% confidence interval.
- **Recovered revenue per encounter** = mean of (allowed amount at gold code set −
  allowed amount at submitted code set), using the fee schedule chosen in Section 10.
  Report mean, median, and annualized (× encounter volume).

Secondary (accuracy):

- **E/M exact-match rate** = engine level == gold level.
- **E/M within-one-level rate** = |engine level − gold level| ≤ 1.
- **E/M leveling agreement** vs gold via **quadratic-weighted Cohen's kappa**
  (levels are ordinal).
- **Diagnosis capture precision / recall / F1** = engine ICD-10 set vs gold ICD-10
  set (exact code; also report at 3-character category for partial credit).
- **Denial-flag precision** = of the engine's denial-risk flags, the share a coder
  agrees are real risks.

Throughput & upskilling (the goal is faster, better coders — not fewer):

- **Time-to-code per encounter**, engine-assisted vs the coder's baseline without the
  tool, and its **trend over time** (a falling median as coders learn the tool is the
  upskilling/throughput signal). Captured automatically by the review UI.
- **Suggestion acceptance rate** = share of engine codes accepted unchanged; high and
  rising indicates the engine is trustworthy and the review is fast.

Safety (the one that gates deployment):

- **Over-coding rate (engine vs gold)** = encounters where the engine's E/M level >
  gold level. By design (the compliance guardrail can only hold/downgrade/remove)
  this should be at or near zero; any non-zero value is investigated before launch.
- **Unsupported-capture rate** = captured codes with no valid documentation span or
  contradicted by the note (should be zero).

## 4. Sample design

Target a sample large enough to estimate the recovery-rate proportion to ±5% at 95%
confidence. With the conservative assumption that the true rate is unknown (p = 0.5),
n = 1.96² · p(1−p) / 0.05² ≈ **384 encounters**. If a quick pilot of ~50 charts
suggests the rate is lower (say ~0.3), the requirement falls to ~323; 350–400 is the
practical planning range. Apply a finite-population correction if the pilot's total
eligible encounters are small.

Stratify so the estimate isn't dominated by one mix, and so you can report by segment:

- by **visit type** (new vs established; office vs telehealth),
- by **specialty / provider** (sample multiple providers so you measure the engine,
  not one clinician's habits),
- by **payer** (Medicare, Medicaid, commercial), since fee schedules and edits differ.

Allocate roughly 50–80 encounters per major stratum. Draw a **random** sample within
each stratum from a defined period (e.g., the last two completed quarters) to avoid
cherry-picking. For the dollar-impact estimate specifically, run the ~50-chart pilot
first to estimate the per-encounter dollar variance, then confirm n gives the precision
you want on the mean difference (a paired comparison on the same encounters needs fewer
charts than an unpaired one).

## 5. Data and de-identification

Use **de-identified** pilot encounters. You already hold a BAA with the pilot group;
a validation/QA study of a product you provide them generally fits the BAA's permitted
"health care operations / quality" uses, but confirm the specific language with counsel
and add a short data-use provision scoping this study, retention, and destruction.
That practice agreement governs receipt and study use; it does not by itself
authorize disclosure to a cloud or model service. Run model validation only on
properly de-identified encounters until the Microsoft agreement/BAA is verified
to cover the selected Azure OpenAI service and US data zone and the private
deployment, retention, identity, and logging acceptance gates are complete.

- De-identify by **Safe Harbor** (remove the 18 HIPAA identifiers) unless your counsel
  prefers **expert determination**; the documentation text must be scrubbed of names,
  dates shifted consistently, geography truncated, and any embedded identifiers removed.
- Apply **minimum necessary**: coders and engineers see only the documentation, the
  submitted code, and (engineers only) the engine output — never more PHI than the task
  needs.
- Store the working set encrypted, access-controlled, and logged; destroy or return per
  the data-use provision at study end.

## 6. Gold-standard construction (dual coding + adjudication)

The gold standard is the spine of the study, so build it independently of both the
submitted code and the engine.

1. Engage **two credentialed coders** (e.g., AAPC CPC or AHIMA CCS) with relevant
   outpatient/specialty experience.
2. Each coder independently codes each encounter **from the documentation only**,
   **blinded** to (a) the level originally submitted and (b) the engine's output, and
   in **randomized order**. Each assigns the E/M level and the diagnosis set, and notes
   the supporting text.
3. Measure **inter-rater reliability** (quadratic-weighted kappa for E/M; F1 for
   diagnoses). Low agreement is itself a finding about documentation clarity.
4. **Adjudicate** every disagreement: a third senior coder (or a consensus conference)
   resolves it; the adjudicated result is the gold standard for that encounter. Record
   the rationale.

**Single-coder fallback (Phase 0, what you can do now).** With only one credentialed
coder, the interim gold is that coder's assignment, blinded to the submitted level and
the engine output. To keep it honest, have a senior coder (or the same coder on a later
pass with the chart re-blinded) over-read a 15–20% subsample and report the two-reader
agreement (weighted kappa) on that overlap. Treat the resulting recovery rate as
provisional, with wider confidence intervals, and don't use it for external claims — it
exists to steer development until Phase 1.

## 7. Blinding and bias controls

- The engine's output is withheld from the gold coders until after adjudication.
- Coders do not see the submitted level (so they can't anchor to it).
- The analysis plan (metrics, thresholds, strata) is fixed in writing **before** the
  engine is run against the gold set — no post-hoc metric shopping.
- Whoever computes the metrics is different from whoever tunes the engine.

## 8. Procedure

1. Define the eligible population and draw the stratified random sample (Section 4).
2. De-identify the documentation and freeze the working set (Section 5).
3. Capture each encounter's **submitted** code set from the source system.
4. Run **dual coding → adjudication** to build the gold standard (Section 6).
5. Run the **engine** on the same de-identified documentation; capture its output and
   its audit trail. Pin the versions in play: CPT edition, ICD-10 fiscal year, NCCI
   quarter, LCD/article versions, and the model deployment/version, API version,
   prompt build, and structured-output mode. Record whether the strict-schema or
   JSON-object fallback path ran; never treat fallback as equivalent without
   separate acceptance results.
6. Compute all metrics in Section 3 with confidence intervals; break out by stratum.
7. Review every case where the engine and gold disagree, and every (expected-zero)
   over-coding case, with a coder; categorize the cause (extraction miss, rule gap,
   ambiguous documentation).
8. Write up against the pre-registered thresholds (Section 9).

## 9. Acceptance thresholds (ratify with your compliance team)

These are sensible starting bars; set the final numbers with your coders and compliance
officer before any deployment.

- E/M within-one-level agreement with gold **≥ 90%**, exact-match **≥ 70%**.
- E/M weighted kappa **≥ 0.6** (substantial agreement).
- Diagnosis capture F1 **≥ 0.85** for primary diagnosis.
- **Over-coding rate ≤ 1%** and **zero** codes above gold attributable to the engine
  rather than ambiguous documentation; **zero** unsupported captures. This is the hard
  gate — a billing tool that recovers revenue but occasionally over-codes is not
  shippable.
- Denial-flag precision **≥ 80%** (flags coders agree are real).

Deployment posture: until these are met, the engine runs **assistive only** — every
output is provider/biller-reviewed (the worksheet already requires sign-off). Autonomous
or auto-submit behavior is out of scope for this study.

## 10. Dollar impact

For each encounter compute the per-code allowed amount under the chosen fee schedule and
sum the difference between the gold set and the submitted set:

- Use the practice's **contracted commercial rates** for the realistic number, and/or
  the real **Medicare allowed amounts** from CMS's Physician & Other Practitioners data
  as a neutral, reproducible benchmark (the `benchmarks/` module loads these per code
  and specialty — e.g., 2024 Internal Medicine office visits: 99213 ≈ $89, 99214 ≈ $123,
  99215 ≈ $173).
- Report recovered revenue (gold > submitted) **and** avoided over-billing (any case
  where submitted > gold), so the figure is net and defensible.
- Annualize by the practice's encounter volume and by the stratum mix.
- For top-down market-sizing (before the chart study), compare the practice's E/M level
  mix to its specialty's national mix via `EMBenchmark.compare(...)`: a practice sitting
  well below the specialty mean on higher levels is a candidate for under-coding (an
  upper-bound opportunity that prioritizes chart review, not a coding target), and one
  well above is screened as a potential over-coding outlier. This is a population
  benchmark only — never an input to coding an individual encounter.

Frame the headline as captured-but-uncoded revenue from documented, medically-necessary
care — never as an increase in services or acuity.

## 11. Governance, audit, reproducibility

- Log every engine decision and keep the full audit trail for each encounter.
- Version everything (CPT edition, ICD-10 FY, NCCI quarter, LCD/article version,
  model + prompt build) so a result is reproducible and a regression is detectable.
- Re-run the engine against the frozen gold set on every model or ruleset update; treat
  a drop below threshold as a release blocker.
- Retain the gold set as your regression suite.

## 12. Limitations and ethics

The study measures **defensible** recovery only; the guardrail guarantees no code
exceeds documented support, so "recovery" never means inflating acuity. MIMIC/MDACE
validate the diagnosis and evidence-grounding side but are inpatient, so they
complement — not replace — this outpatient gold set. The engine remains a decision
support tool; certified-coder and provider judgment govern the final claim.

## 13. Coder feedback loop (augment, don't replace)

The contract coders supporting the pilot are the engine of this study, not its
subject. The review tool (`frontend/CoderReview.jsx`) pre-codes each encounter with
its evidence and the *why* behind every code, and the coder accepts, edits, or
rejects each one with a reason in a click or two. That does three things at once:

- it makes the coder **faster** (they review a draft instead of coding from scratch),
- it **upskills** them (every code shows the rule rationale and the supporting text), and
- it **captures their corrections** as structured feedback (`coder_feedback.py`).

Those captured corrections are exactly the Phase 0 shadow-mode data and the seed of
the gold standard: each submission records the engine code, the coder's final code,
the reason, the engine version, and the time spent. `scripts/analyze_feedback.py`
turns the accumulated feedback into (1) the agreement/accuracy and throughput metrics
above, (2) a **ranked rule/prompt tuning backlog** (e.g. "engine under-levels E/M when
data element X" or "coders keep adding code Y"), and (3) an exported **gold set** that
becomes the regression suite. For a rule + LLM engine that is how feedback "re-enters
the model": rule calibration, prompt/few-shot updates, and a versioned regression set —
each change auditable and re-validated against the frozen gold set (Section 11). The
engine never auto-applies a coder's correction to other claims; corrections inform
versioned updates that are reviewed and regression-tested before release.

## Open parameters — please confirm

1. **Coders** — *current answer: no two coders yet.* So we start in Phase 0 with one
   contract coder (Section 6 fallback) or, failing that, shadow mode + provider
   face-validity review. Confirm whether you can engage even one credentialed coder
   per-chart now, or want help drafting a scope of work to source one.
2. **Sample** — *current answer: TBD on count and source.* Default plan: pull from the
   pilot group, de-identified under the existing BAA, starting at ~50 to estimate
   variance. Tell me the realistic first-pull size and which specialties/payers are in
   the mix once you know, and I'll finalize the stratification and target N.
3. **Fee schedule**: report dollars on the pilot's contracted commercial rates, the
   Medicare PFS benchmark, or both?
4. **De-identification & governance**: Safe Harbor or expert determination, and does
   counsel want a standalone data-use addendum to the existing BAA for this study? Any
   IRB/ethics review required by the pilot organization?
5. **Thresholds**: are the Section 9 bars acceptable as a starting point, or does your
   compliance team want different ones ratified first?
