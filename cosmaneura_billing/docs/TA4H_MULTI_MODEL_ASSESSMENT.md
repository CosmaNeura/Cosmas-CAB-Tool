# Assessment: Adding Azure Text Analytics for Health as a Second Billing-Analysis Model

**Date:** 2026-08-19
**Question:** Can we synthesize Azure AI Language "Text Analytics for health" (TA4H) alongside our
own billing-analysis engine? Is it covered under our Azure subscription, HIPAA-compliant,
non-disruptive to what we've built, and actually valuable?

**Short answer: yes, feasible and recommended — but as a second *extraction engine* feeding our
existing rule engine, not as a competing end-to-end coder.** TA4H does clinical NER, relation
extraction, assertion detection (negation/hypothetical/family-history), and UMLS entity linking
with ICD-10-CM codes. It does **not** do E/M leveling, CPT selection, NCCI edits, or denial
risk — so it slots in exactly where our pipeline is weakest (concept→ICD-10 resolution) and
competes with nothing we consider differentiated IP (the MDM rule grid, guardrails, denial
analytics).

---

## 1. What TA4H is (verified against Microsoft docs, Aug 2026)

- A **prebuilt feature of Azure AI Language** (now branded "Azure Language in Foundry Tools"),
  generally available. It applies ML to unstructured clinical text — doctor's notes, discharge
  summaries, EHR text — and returns:
  - **Named entities**: diagnoses, symptoms/signs, medications (name/dose/route/frequency),
    procedures/examinations, anatomy, demographics, and more.
  - **Relations**: e.g. medication↔dosage, condition↔qualifier.
  - **Assertion detection**: certainty (present/absent/hypothetical/conditional) and association
    (subject vs. family member) per entity — contextual modifiers that preserve meaning.
  - **Entity linking**: preferred names and concept codes from the **UMLS Metathesaurus**
    (which encompasses ICD-10-CM, SNOMED CT, RxNorm, MeSH, etc.), returned with character
    offsets into the source text.
  - Optional **FHIR-structured output** for EHR interoperability.
- **Deployment options**: hosted async REST API (long-running operation) in our Azure region,
  or an **on-premises/VNet Docker container** if we ever need text to never leave our perimeter.
- **Languages**: English plus German, French, Italian, Spanish, Portuguese, Hebrew.
- **2026 status check** (important, given Azure churn): the prebuilt service is alive and moved
  into the "Foundry Tools" umbrella. Two adjacent retirements to be aware of:
  - *Custom* Text Analytics for health (preview) was **retired 2025-01-10** — we cannot
    fine-tune TA4H; we consume it as-is.
  - *Language Studio* (the web playground) retires 2027-03-20 — **API endpoints and SDKs are
    unaffected**.

Sources: [TA4H overview (Microsoft Learn)](https://learn.microsoft.com/en-us/azure/ai-services/language-service/text-analytics-for-health/overview),
[What's new in Azure Language](https://learn.microsoft.com/en-us/azure/ai-services/language-service/whats-new),
[GA announcement](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/text-analytics-for-health-now-generally-available-unlocks-clinical-insights-and-/2531955).

## 2. Subscription coverage and cost

- TA4H is a **standard consumption-billed Azure service**. There is no separate license or
  contract: create an Azure AI Language resource in the subscription, and usage bills to it
  like any other Azure meter. In that sense, yes — it is "covered" by an Azure subscription.
- **Billing unit**: the "text record" = 1,000 characters (a 6,500-character visit bundle = 7
  text records per API call).
- **Free tier (F0)**: 5,000 text records/month — more than enough for the entire validation
  study before we pay anything. The S tier also includes the same initial free quota.
- **Standard (S) tier**: pay-per-text-record with volume discounts; **commitment tiers** and
  disconnected-container pricing exist for scale.
- ⚠️ The exact per-1,000-record dollar rate varies by region/agreement and could not be
  verified from this environment (Microsoft pricing pages are blocked by the sandbox egress
  proxy). **Action item: read the exact rate off the
  [Azure AI Language pricing page](https://azure.microsoft.com/en-us/pricing/details/language/)
  / pricing calculator for our region before budgeting.** Order-of-magnitude: clinical NLP
  APIs in this class price such that a typical multi-page encounter costs cents, not dollars —
  immaterial next to the LLM calls we already make per encounter, but confirm the number.
- **Caveat that matters more than price**: this repo currently has *no* Azure resources at all
  (no hosting, no Postgres, nothing provisioned — Azure is the documented intent in
  `migrations/README.md`, not the current state). "Covered under my subscription" is true the
  moment a Language resource is created in it; TA4H may be the sensible trigger to stand up
  the Azure footprint we already planned.

## 3. HIPAA posture

- **BAA**: Microsoft's HIPAA Business Associate Agreement is included **by default** in the
  Microsoft Product Terms / Data Protection Addendum for every Azure customer that is a
  covered entity or business associate. **No separate signature is required.**
  ([Microsoft HIPAA offering](https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-hipaa-us))
- **In scope**: Azure AI Language / Cognitive Services (Text Analytics, incl. TA4H) is a
  HIPAA-eligible, in-audit-scope service under that BAA.
- **Data handling**: processing is stateless; input text is not stored and is not used to
  train models. **One required configuration**: the Language service logs input text for up to
  48 hours by default for troubleshooting — set the **`loggingOptOut=true`** parameter on
  every call (or disable at the resource) before any PHI flows. Use private
  endpoints/VNet + managed identity when we host in Azure; the container option exists if we
  ever need zero egress.
- **Shared responsibility**: BAA eligibility ≠ compliance. Our obligations (access control,
  encryption, audit logging, minimum necessary) are unchanged and already written down in
  `VALIDATION_PROTOCOL.md` §5.
- **Comparative note**: today the pipeline sends the *entire* visit bundle to Anthropic's
  public API (`llm.py`), and our own docs say "de-identified notes only until a BAA is in
  place." A TA4H call inside our own tenant under the default Microsoft BAA is a *stronger*
  PHI posture than the status quo, not a weaker one — and it reuses the exact "existing cloud
  BAA, no new subprocessor" argument we used to justify replacing Pinecone with pgvector.

## 4. Will it break what we've built? No — if integrated at the right seam

The architecture is unusually well suited to this:

- Every stage after `CodingAgent` is a pure `list[CodeSuggestion] → list[CodeSuggestion]`
  transform (evidence verification, documentation integrity, preferences, compliance
  guardrail, denial risk). A second producer of suggestions flows through all of it unchanged.
- `BillingOrchestrator.__init__` already injects every collaborator **except** the coder.
  One-line seam to add: a `coding=` (and `secondary_coding=`) constructor parameter
  (`orchestrator.py:24-25`; the coder is currently hard-instantiated at `:38`).
- TA4H returns **character offsets** for every entity — evidence quotes built from offsets
  verify at similarity 1.0 against `evidence.py`'s verifier *by construction*. Our
  hallucination firewall becomes a formality for TA4H output instead of a filter.
- `CodeSuggestion.notes: list[str]` and the free-form audit trail already absorb per-engine
  provenance (`engine:ta4h`, `agreement:both`) with zero schema breakage; a defaulted
  `source_engine` field on `CodeSuggestion` is a cleaner additive change.

**The two real risks, and their mitigations:**

1. **The no-upcode invariant.** `ComplianceGuardrailAgent` recomputes the E/M ceiling from the
   `facts` dict. If two engines' facts are merged optimistically, the ceiling can rise and the
   invariant `prove_no_upcode.py` proves in CI is punctured. **Rule: merge facts
   element-wise-minimum (conservative), and any code found by only one engine enters as
   `needs_review`, never `captured`.** Extend `prove_no_upcode.py` to cover the ensemble path.
2. **The zero-dependency core.** `pyproject.toml` deliberately keeps the engine stdlib-only.
   Either call TA4H's REST API via `urllib` exactly as `AnthropicLLM` and the AMA CPT fetcher
   already do, or put `azure-ai-textanalytics` behind a new optional extra (`[azure]`) with a
   lazy import — both patterns have precedent in the repo. Ship a `MockTA4H` mirroring
   `MockLLM` so CI stays offline and hermetic.

Minor: endpoints are sync (`def`), and TA4H is an async long-running operation adding seconds
of latency — run the two engines in a threadpool inside `process()` so latency is max(), not
sum().

## 5. Does it add value? Yes — four concrete ways

1. **It replaces our weakest link.** Concept→ICD-10 resolution today is a naive keyword-overlap
   scorer (`knowledge/icd10_ccsr.py:135-145`). TA4H's UMLS entity linking with ICD-10-CM codes
   is a direct, managed-service upgrade; our existing FY-billable overlay stays as the final
   filter.
2. **Assertion detection hardens compliance.** TA4H's negated / hypothetical / family-member
   modifiers converge with what `DocumentationIntegrityAgent` tries to do from LLM output —
   a second, independent signal against coding denied or family-history conditions.
3. **Cross-vendor redundancy with a human-in-the-loop story.** Engine disagreement routed to
   the existing coder-review queue (`CoderReview.jsx` reason codes + feedback loop) is exactly
   the "two independent reads, human adjudicates" narrative payers and auditors like — and it
   generates labeled disagreement data for our own tuning.
4. **It sidesteps the licensing wall.** `docs/CAC_DATA_SOURCES.md` surveys and rejects the
   open-source equivalents (MedCAT — Elastic License; scispaCy/QuickUMLS — UMLS/SNOMED
   licensing; PLM-ICD — MIMIC data taint). TA4H is the managed version of that entire stack
   with Microsoft holding the licensing burden.

**What it will not do** (so nobody oversells it internally): no E/M leveling, no CPT/HCPCS
selection, no MDM elements, no medical-necessity or denial logic, no guarantee of
billable-specificity ICD-10-CM. It is an extractor/linker, not a coder. Our rule engine
remains the coder — which is precisely why this is complementary and low-risk.

## 6. Recommended shape ("what success looks like")

```
VisitBundle ──┬── CodingAgent (Claude: facts + concepts)      ──┐
              │                                                 ├── ConsensusAgent ── existing pipeline
              └── TA4HCodingAgent (entities + assertions +     ──┘   (evidence → integrity → prefs →
                  ICD-10-CM links, offset-anchored evidence)         guardrail → denial risk → UI)
```

- **Consensus policy**: union by normalized code; both-engines → eligible for `captured`
  (guardrail still gates); single-engine → `needs_review` with an agreement badge in the coder
  UI; facts merged element-wise-minimum for MDM inputs.
- **Config**: `AZURE_LANGUAGE_ENDPOINT` / `AZURE_LANGUAGE_KEY` (managed identity once hosted
  in Azure); absent → engine silently disabled, current behavior byte-identical. `loggingOptOut`
  always on.
- **Rollout**: (1) shadow mode — TA4H runs, results logged, nothing surfaced; measure agreement
  and lift against the frozen gold set per `VALIDATION_PROTOCOL.md` §11/§13 on the F0 free
  tier; (2) coder-review surfacing; (3) consensus gating. Ship nothing to `captured` from TA4H
  until the shadow metrics clear the protocol.

## 7. Open items before implementation

- [ ] Confirm exact S-tier per-record price for our region in the Azure pricing calculator.
- [ ] Confirm our Azure agreement type (BAA flows through Product Terms/DPA automatically for
      MCA/EA/CSP; verify ours).
- [ ] Decide REST-via-urllib vs. `[azure]` optional extra.
- [ ] Compliance sign-off on the conservative facts-merge policy.
- [ ] Decide whether TA4H integration triggers the broader Azure hosting build-out
      (`STATUS_REVIEW_2026-08-06.md` items: hosting, auth, Postgres, secrets).
