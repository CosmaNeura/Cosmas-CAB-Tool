# Customer Discovery Synthesis — Sherin George, MCD Provider (Billing-Focused)

> **RECONSTRUCTED Aug 12, 2026.** The original synthesis was delivered as a download in the Aug 10 session ("Integrating customer interview insights into codebase"), whose workspace has since reset. Rebuilt from recovered excerpts of that session plus project memory: **§1–§3 below match recovered original text nearly verbatim, including all quotes**; §4–§5 are faithfully summarized from the original's recovered opening and the constraints it fed into `CLAUDE.md` and the requirements synthesis. If the original file is retrieved from that chat's download links, **the original supersedes this document.** Recording URL lives in the original / John's Fathom library.

**Date:** August 10, 2026
**Participants:** Sherin George (MCD provider — RRM/fertility practice) — with John Clemente (CosmaNeura)
**Sequence:** Discovery conversation **#5 of 6** (Jul 21 Scott → Jul 24 Maura → Aug 6 billers → Aug 6 Christa → **this call** → Aug 13 follow-up).

**How to use this document (Claude Code sessions):** Customer evidence, not spec. This interview is the primary evidence for the Provider Billing Tab (requirements synthesis **F1**) and the origin of its committed timeline. Nothing in discovery overrides the anti-inflation invariant (hold / downgrade / remove only) or PHI boundaries.

---

## 1. Headline findings

1. **Bilateral demand, four days apart.** She independently requested a billing surface with AI-generated code candidates — demand for the CAB pipeline is now validated on **both sides of the workflow**: billers (Aug 6, with the coder-time constraint) and providers (Aug 10, unprompted).
2. **Her coding behavior is exactly what the invariant exists to police — and her legitimate need is fully servable within it.** She splits lumped assessment items ("overweight, obesity, metabolic health") into separate documented ICD codes so labs correlate to the right diagnosis and coverage follows (semaglutide covered when obesity + insulin resistance + family history are all coded). That is **specificity of documented, addressed conditions — legitimate and squarely in scope.** But her framing — "it's kind of hacking the insurance companies," "the more fertility ICD codes we have, the better," wanting "three or four major ICD codes to get... the higher coding" — describes code-count-driven E/M inflation if implemented literally. §4 draws the line precisely.
3. **Coverage-aware code selection is a real provider need — and it's commercial-payer territory.** "Insurance will not cover infertility. Most insurances won't. So we will use different health conditions" — RRM codes the documented underlying conditions (endocrine disorder, ovarian dysfunction, progesterone deficiency) rather than infertility. This is payer-coverage knowledge (DenialRiskAgent territory), but for **commercial payers**, which are explicitly out of the engine's Medicare-first scope. Flagged as a scope tension, not a commitment.
4. **A pricing anchor is now on record.** John stated the standing arrangement with Greg & Kathleen: **$59/user/month**, currently given away free, with John seeking Sherin's help driving provider adoption in exchange for deferring charges. This updates the project's "pricing entirely undetermined" status to: anchor set with leadership in principle; no one has paid; value validation still pending. (The Jul 21 Scott doc later showed this anchor already existed by that date.)
5. **The workflow insertion point got its provider-side half.** Sherin confirmed the biller account from Aug 6: **providers enter the initial codes that go onto the slip.** AI-assisted codes will therefore be born in the provider's billing tab → slip → biller review. John explicitly deferred the "how aggressive, and how it reaches billers" question to Grace and Johanna — confirming it as an **Aug 13 decision**. The net-coder-minutes metric now has a provider-side counterpart: **provider minutes saved locating and splitting codes.**

---

## 2. What she asked for, precisely

1. **ICD codes generated with the plan, copy-pasteable.** She picks which ones to use; the AI identifies candidates from the visit. ("If I can just copy and paste because it generated the ICD code, that would have been awesome.")
2. **Assessment/plan decomposition to code granularity.** Lumped items split so each documented condition carries its own code, with the plan's tasks/orders grouped under the code that justifies them — semaglutide instructions under obesity (+ family history), lab orders under insulin resistance. (This is also exactly the DenialRiskAgent's primary-dx-linkage check, seen from the provider side.)
3. **Multi-code mapping with dedupe.** Some findings legitimately sit under more than one code (progesterone deficiency under an endocrine-disorder umbrella and its own code); she asked whether repeats can be avoided.
4. **Coverage-aware suggestions** ("is it possible to teach the system to know important ICD codes"): surface the documented conditions payers actually cover — never lead with a non-covered label like infertility when the documented underlying conditions are the billable reality.
5. **CPT/E/M alongside ICD.** John's on-call framing and the recorded action item both include ICD **and** CPT in the billing tab scope.

## 3. Design agreed on the call

- **Separate billing tab** (dollar-sign icon in the Cosmos left nav): same visit, same note content, reorganized by a second billing AI into a billing reference view. The clinical note remains clinical-first and goes into the EMR unchanged — Sherin confirmed the priority explicitly ("clinical tool first... that's correct").
- **John's stated compliance posture, on the record with the customer:** "I have trained my model to be very conservative... it is not going to identify things for up-billing" — with workflow aggressiveness deferred to the billers ("that's their expertise").
- **Commitment + timeline:** recorded action item — *add billing tab w/ ICD/CPT for Sherin, then schedule demo/feedback*. John told her **~1–2 weeks to first visible results** (→ ~Aug 17–24). What can honestly ship in that window is bounded by the standing gates: the demo runs on de-identified/synthetic encounters until the BAA + deployment layer exist.

---

## 4. The invariant boundary — how to build this without crossing it (summarized from original)

Sherin's underlying clinical practice is legitimate: RRM treats real, documented underlying conditions, and coding to the highest documented specificity is proper coding, not inflation. Her "hacking insurance" phrasing is casual provider-speak for coverage navigation. The line the build must hold:

- **Servable:** decomposing documented, addressed conditions to their most specific codes; grouping plan items under the justifying diagnosis; ordering/labeling legitimate evidence-backed codes so covered documented conditions lead; showing the E/M level that the documented MDM facts produce — including above the provider's selection, with evidence.
- **Not servable:** treating code *count* as a lever on E/M level; adding or suggesting any code without documentation evidence; relabeling toward covered diagnoses that aren't documented and addressed; any prompt-only "conservative" implementation. **E/M is an output of documented facts, never a target.** Every rendered candidate carries evidence spans; the tab routes through the deterministic pipeline (CodingAgent → DocumentationIntegrityAgent → ComplianceGuardrailAgent → DenialRiskAgent); acceptance framing: the practice could hand a payer this view and defend every code.
- **The standing architectural flag:** John's "trained to be conservative" language describes prompts; the project's own architecture requires the invariant in deterministic code paths. The billing tab must be the engine's ProviderCard rendered in Cosmos — not a parallel prompt product.

## 5. Scope tensions recorded (not commitments)

Commercial-payer coverage intelligence (her infertility example is Optum/UHC-world, not Medicare NGS J-K) — direction acknowledged, out of current engine scope; coverage *notes* in the tab are best-effort metadata, not adjudication, until commercial rules are in scope. Aggressiveness/routing of provider-born codes into biller review → Aug 13 (Grace/Johanna own the answer).

---

## 6. Quote anchors (recovered; attribution by context)

- "If I can just copy and paste because it generated the ICD code, that would have been awesome."
- "It's kind of hacking the insurance companies." / "The more fertility ICD codes we have, the better." / "three or four major ICD codes to get... the higher coding."
- "Insurance will not cover infertility. Most insurances won't. So we will use different health conditions."
- "Clinical tool first... that's correct."
- John: "I have trained my model to be very conservative... it is not going to identify things for up-billing." / aggressiveness deferred to billers: "that's their expertise."

---

## 7. Repo integration

- **Path:** `docs/discovery/2026-08-10-provider-sherin-george-billing.md` (this reconstruction may be committed as-is; replace with the original from the Aug 10 chat's download links if retrieved — original supersedes). Raw Fathom transcript → `docs/discovery/transcripts/2026-08-10-sherin-george.txt` (export from Fathom).
- **Cross-references:** requirements synthesis **F1** (primary evidence + acceptance criteria), §1 hard constraints (the pipeline-routing rule this call made load-bearing); biller doc §1.4/§3 (coder-minutes constraint and the Aug 13 aggressiveness decision); Maura doc §1.2 (the conservatism warranty's chronologically first instance); Christa doc §3.4 ("double-checking your work" corroboration).
- **After Aug 13:** record the aggressiveness/routing decision and the time-vs-MDM answer here or in the requirements doc §8; confirm the demo date with Sherin against the gate-bounded scope.
