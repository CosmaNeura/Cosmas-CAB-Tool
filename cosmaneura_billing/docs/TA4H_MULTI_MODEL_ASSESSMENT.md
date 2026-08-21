# Assessment: Adding Azure Text Analytics for Health as a Second Billing-Analysis Model

**Date:** 2026-08-19 · **Updated:** 2026-08-19, after merging `main`

> **Two facts in this assessment were superseded while it was being written.** The pipeline no
> longer calls Anthropic's public API — inference is now private Azure OpenAI over managed
> identity, disabled by default — and the Azure footprint (Bicep, Entra auth, PostgreSQL,
> observability) now exists. Both changes make the case for TA4H *stronger*, not weaker: the
> second engine reuses the same tenant, the same managed identity, and the same token scope.
> The paragraphs below are corrected; the conclusions are unchanged.

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
  study before Azure AI Language usage charges. The S tier also includes the same initial
  free quota.
- **Private-network standing cost still applies on F0**: the required private endpoint is
  `$0.01/hour × 730 = $7.30/month` and the additional private DNS zone is `$0.50/month`,
  using the same reviewed rates as the Azure OpenAI cost addendum. The exact fixed increase
  is therefore **$7.80/month**, plus separately metered Private Link data processing. F0 is
  free service capacity, not a zero-cost private deployment.
- **Standard (S) tier**: pay-per-text-record with volume discounts; **commitment tiers** and
  disconnected-container pricing exist for scale.
- ⚠️ The exact per-1,000-record dollar rate varies by region/agreement and could not be
  verified from this environment (Microsoft pricing pages are blocked by the sandbox egress
  proxy). **Action item: read the exact rate off the
  [Azure AI Language pricing page](https://azure.microsoft.com/en-us/pricing/details/language/)
  / pricing calculator for our region before budgeting.** Order-of-magnitude: clinical NLP
  APIs in this class price such that a typical multi-page encounter costs cents, not dollars —
  immaterial next to the LLM calls we already make per encounter, but confirm the number.
- **Caveat that matters more than price**: ~~this repo currently has no Azure resources~~ — as
  of `main` the Azure footprint exists (Bicep templates, Entra auth, PostgreSQL, observability),
  so "covered under my subscription" is true the moment a Language resource is added to the
  existing resource group and the API's managed identity is granted read access on it. No new
  subscription, contract, or secret is involved.

## 3. HIPAA posture

- **BAA**: Microsoft's HIPAA Business Associate Agreement is included **by default** in the
  Microsoft Product Terms / Data Protection Addendum for every Azure customer that is a
  covered entity or business associate. **No separate signature is required.**
  ([Microsoft HIPAA offering](https://learn.microsoft.com/en-us/azure/compliance/offerings/offering-hipaa-us))
- **In scope**: Azure AI Language / Cognitive Services (Text Analytics, incl. TA4H) is a
  HIPAA-eligible, in-audit-scope service under that BAA.
- **Data handling**: with `loggingOptOut=true` fixed on every analysis
  submission, processing is stateless; input text is not retained or used to
  train models. The deployed account must use the reviewed private endpoint,
  VNet DNS, and managed identity. The container option exists if we ever need
  zero egress.
- **Shared responsibility**: BAA eligibility ≠ compliance. Our obligations (access control,
  encryption, audit logging, minimum necessary) are unchanged and already written down in
  `VALIDATION_PROTOCOL.md` §5.
- **Comparative note:** ~~the pipeline calls Anthropic's public model API~~.
  Inference now uses private Azure OpenAI over managed identity and remains
  disabled by default. TA4H therefore adds **no
  new trust boundary at all** — same tenant, same Microsoft BAA, same managed identity, same
  `cognitiveservices.azure.com` token scope, no new subprocessor and no new secret. This is a
  stronger version of the argument used to replace Pinecone with pgvector, not a new exception
  to it.

## 4. Will it break what we've built? No — if integrated at the right seam

The architecture is unusually well suited to this:

- Every stage after `CodingAgent` is a pure `list[CodeSuggestion] → list[CodeSuggestion]`
  transform (evidence verification, documentation integrity, preferences, compliance
  guardrail, denial risk). A second producer of suggestions flows through all of it unchanged.
- `BillingOrchestrator.__init__` injected every collaborator **except** the coder; the
  implementation adds `coding=`, `secondary_coding=` and `ta4h=` parameters, alongside the
  `settings=` parameter `main` introduced.
- TA4H returns **character offsets** for every entity — evidence quotes built from offsets
  verify at similarity 1.0 against `evidence.py`'s verifier *by construction*. Our
  hallucination firewall becomes a formality for TA4H output instead of a filter.
- `CodeSuggestion.notes: list[str]` and the free-form audit trail already absorb per-engine
  provenance (`engine:ta4h`, `agreement:both`) with zero schema breakage; a defaulted
  `source_engine` field on `CodeSuggestion` is a cleaner additive change.

**The two real risks, and how they were resolved in the implementation:**

1. **The no-upcode invariant.** `ComplianceGuardrailAgent` recomputes the E/M ceiling from the
   `facts` dict. If two engines' facts are merged optimistically, the ceiling can rise and the
   invariant `prove_no_upcode.py` proves in CI is punctured. The shipped policy is *stricter*
   than the element-wise minimum originally proposed here: **the secondary engine's facts are
   namespaced (`ta4h_*`) so they never reach a leveling key at all, and `merge_facts`
   re-computes the ceiling both ways and reverts to primary-only facts if they ever differ.**
   Any code only one engine found enters as `needs_review` and is held by the guardrail, never
   `captured`. `prove_no_upcode.py` now proves all three properties.
2. **The zero-dependency core.** `pyproject.toml` deliberately keeps the engine stdlib-only.
   TA4H uses stdlib `urllib`, matching the dependency-free transport stance of
   the AMA CPT fetcher. Azure OpenAI uses the official OpenAI SDK through
   managed identity; no public Anthropic adapter remains. There is **no
   `azure-ai-textanalytics` install**. A `MockTA4H`
   mirrors `MockLLM` so CI stays offline and hermetic.

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
3. **Cross-vendor redundancy with a human-in-the-loop story.** After the surface
   milestone, engine disagreement can be routed to the existing coder-review
   queue (`CoderReview.jsx` reason codes + feedback loop). That is the "two
   independent reads, human adjudicates" narrative payers and auditors like,
   and it generates labeled disagreement data for our own tuning.
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
VisitBundle ──┬── CodingAgent (primary Azure OpenAI when enabled; ──┐
              │   deterministic offline model otherwise)           ├── ConsensusAgent ── existing pipeline
              └── TA4HCodingAgent (entities + assertions +     ──┘   (evidence → integrity → prefs →
                  ICD-10-CM links, offset-anchored evidence)         guardrail → denial risk → UI)
```

- **Consensus policy**: secondary facts remain namespaced and cannot affect the
  MDM ceiling. A single-engine diagnosis is held, either engine may veto,
  secondary evidence never supplies a missing primary span, and agreement never
  independently promotes capture. Shadow results remain internal until the
  reviewer-surface milestone.
- **Config**: `COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED` selects the isolated
  observation lane; `COSMANEURA_AZURE_LANGUAGE_ENABLED` separately selects
  active consensus. They are mutually exclusive and both require
  `AZURE_LANGUAGE_ENDPOINT` + `AZURE_CLIENT_ID`; shadow additionally requires
  primary Azure OpenAI. **Managed identity only** — key-based auth is
  prohibited. `loggingOptOut` is always on and is not configurable.
- **Rollout**: (1) run the fixed synthetic shadow acceptance harness; (2) run
  TA4H out-of-band and surface nothing while measuring aggregate agreement and
  latency against the externally maintained frozen gold set per
  `VALIDATION_PROTOCOL.md` §11/§13 on F0; (3) coder-review surfacing; (4) only
  then consider consensus policy changes. Ship nothing to `captured` from TA4H
  because of agreement.

## 7. Status

**Implemented** on `claude/billing-analysis-multi-model-3k99ke`, off by default. See
[`TA4H_INTEGRATION.md`](TA4H_INTEGRATION.md) for the operator guide, the consensus policy as
built, and the rollout gates.

Resolved during implementation:

- [x] REST-via-`urllib` — no new dependency, core stays stdlib-only.
- [x] Facts-merge policy — namespaced secondary facts + a ceiling clamp, stricter than the
      element-wise minimum originally proposed. Proven in `prove_no_upcode.py`.
- [x] Offline mock so CI stays hermetic; graceful degradation on a TA4H outage.

Still open:

- [ ] Confirm exact S-tier per-record price for our region in the Azure pricing calculator
      (immaterial at pilot volume; F0 covers the study's Language usage, while the documented
      $7.80/month private-network standing cost remains).
- [ ] Compliance sign-off on the consensus policy as built.
- [x] Implement the isolated, non-influencing shadow seam, strict deployment
      latches, closed aggregate health schema, and synthetic acceptance harness.
- [ ] Create/freeze the external 40–60-case coder-labelled gold set and run the
      shadow measurement per `VALIDATION_PROTOCOL.md` §11/§13 before surfacing
      agreement badges. The gold set remains external and pending; runtime
      aggregate counters are not a substitute for coder labels.
- [x] Extend `infra/app.bicep` with dormant F0 Language account provisioning,
      private endpoint/DNS, and the API-identity-only reader assignment. The tracked
      deployment defaults still provision nothing and keep extraction disabled.
