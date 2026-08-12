# Customer Discovery Synthesis — MCD Billing Team (Grace, Johanna, Edgar)

> **RECONSTRUCTED Aug 12, 2026.** The original synthesis was delivered as a download in the Aug 10 session ("Integrating customer interview insights into codebase"), whose workspace has since reset. This version was rebuilt from recovered excerpts of that session plus project memory: **structure, findings, and all quotes below match recovered original text**; some original sections are summarized rather than reproduced in full. If the original file is retrieved from that chat's download links, **the original supersedes this document.** Recording URL lives in the original / John's Fathom library.

**Date:** August 6, 2026 · ~42 min
**Participants:** Grace Targonski (billing/coding lead), Johanna Adriano (biller/coder), Edgar Morgan (MHA) — with John Clemente (CosmaNeura)
**Organization context:** MCD — hybrid telehealth + in-person, recently migrated to athenaOne, **three billing staff**, significant restorative reproductive medicine (RRM) service lines, **provider payroll dependent on monthly claims closure**.
**Sequence:** Discovery conversation **#3 of 6** (Jul 21 Scott → Jul 24 Maura → **this call** → Aug 6 Christa → Aug 10 Sherin → Aug 13 follow-up). This call partially executes the CardCapture brief §7 discovery plan — conversation 1 of the recommended 5–10, held with the billing team of the most likely design partner.

**How to use this document (Claude Code sessions):** Customer evidence, not spec. It maps what MCD's billing team said onto the three surfaces — CardCapture, the `cosmaneura_billing` five-agent engine, and the shipped SOAP-note + intake chatbot tools. Nothing in discovery overrides the anti-inflation invariant (hold / downgrade / remove only) or PHI boundaries.

---

## 1. Headline findings

1. **Insurance card collection is their self-declared #1 pain — and the bottleneck is upstream of OCR.** Grace: missing cards are "the number one problem for telehealth... at least for our telehealth business." Athena already has native card OCR with QR upload; it's used and "works well enough" — though it "sometimes picks the wrong payer" (which is exactly the package-selection wedge in the CardCapture brief). The failure mode is that **patients don't submit the card at all** — before, during, or even five minutes after the visit. John's paraphrase, confirmed on the call: the system works; patients aren't using it. **Implication:** near-term value for this customer is *collection + chase + direct-contract intelligence*, not read quality. → Requirements doc **F9**.
2. **They explicitly requested an agentic self-pay chase workflow — unprompted.** Johanna (~28:35): "Is there a way to extract continuously through the month self-pay patients that were missing information and... hopefully get their attention to submit what we're missing or maybe confirm that they are self-pay?" The acute pain it lands on: three billing staff, "somewhere between three and 500 claims to process in the next few days" at month end, payroll depending on it, and the held pile dominated by phantom self-pays "where there probably is an insurance card." → **F5 / F9 shared chassis.**
3. **Telehealth documentation gaps cause outright denials today.** Recent Optum/UHC denials because notes didn't clearly state that it was a telehealth visit, how long it was, and where it happened. They want a standardized attestation blurb at the **top** of every telehealth note. → **F2 telehealth.v1.**
4. **The billing engine's premises got field confirmation — with one hard adoption constraint.** Under-coding recovery is real and happening manually today: "Because the provider said brittle hair, I can use that diagnosis to help the patient get coverage for a lab for hypothyroidism" (Grace, ~18:30). And the constraint: Grace's gut that AI-suggested codes could "create more time for us as the biller coder, because we have to review what you're sending in and the modifiers" → **net coder minutes per claim** is the adoption metric; suggestions must arrive review-cheap or not at all. → **F1 constraint, F6.**
5. **Direct-contract patients must never be charged a self-pay fee.** Grace (~22:45): "If we could somehow ensure that we know if it's a direct contract patient so that we never... avoid charging them a self-pay fee." Encoded as the hard, fail-closed gate in `CLAUDE.md`. → **F3.**
6. **Note completeness checking with provider override was requested** — a pre-submission review that "reviews their note and says, hey, you forgot this, you forgot this… and maybe they can override it" (Grace; quote carried from the original). → **F2.**

---

## 2. Feature signals, prioritized (recovered from original)

### P0-A · Self-pay chase agent — new build, explicitly requested

**The ask (Johanna):** continuously through the month, find open slips designated self-pay / missing insurance, contact those patients to submit a card or confirm true self-pay status, so slips close *before* the month-end payroll crunch instead of piling up in it.

Requirements extracted from the call:
- Daily (configurable) sweep producing a working list of open self-pay / missing-info slips. **Source TBD** — Athena report export vs. API pull (§3 systems questions).
- Automated, polite, escalating outreach — email first, SMS later — with a direct card-upload link **and a second path** (photo emailed to the billing collection inbox). Grace: "maybe we need to give them two or three ways."
- Message copy per Grace's draft language: all patients must supply their card, **including members of directly contracted organizations**, or they will be billed self-pay.
- **Direct-contract guard:** before any self-pay designation or fee, check the patient against the direct-contract roster. Hard, fail-closed gate — consistent with engine philosophy.
- **Card intake path, V1 vs. V2:** near-term, uploads route to the billing team's queue/inbox — *pure collection assist, no CosmaNeura PHI persistence*. Later, the CardCapture verification → proxy → Athena write path, **gated on the BAA + Marketplace clocks**. (Reconciliation note for the requirements doc: apply this V1/V2 split to F9c's completion loop.)
- On-demand status reporting: "run me the self-pay list and outreach status today" — matches the Cosmos agent framing John demoed on the call.
- Success metrics: month-end held-slip count; median days from visit to card-on-file; staff hours spent chasing cards; **direct-contract misbills (target: zero).**

### P0-B · Telehealth attestation header — small change to the shipped SOAP-note tool

- Prepend a fill-in template to every telehealth note, **at the very top** (explicit biller preference): synchronous audio-video statement; duration; patient location = home (**POS 10**) or other location (**POS 02**); consent to the limitations of telehealth; **patient's state at time of visit** (licensure check).
- Provider selects home/other; state pre-filled from intake data where available; provider can edit or override.
- Success metric: zero telehealth-documentation denials (baseline: the recent Optum/UHC denials).

### P1-A · Card capture at intake — extend shipped intake chatbot + CardCapture front end

- Add card front/back capture to the intake-link flow (CardCapture's format pipeline already solves the iPhone problems: HEIC, EXIF rotation, oversize photos).
- Ask again immediately post-visit for no-shows on the card step (Grace: "even five minutes after the visit").
- **Decision required:** hand images into Athena's existing OCR/QR path vs. hold for the CosmaNeura verified-write path. V1 ships as pure collection assist with zero PHI persistence; verified write-back waits on the two long-lead clocks.

---

## 3. Explicitly deferred to the Aug 13 follow-up (recovered question list)

- **Quantify denials:** first-pass clean-claim % in Athena (they don't know it yet — ask which Athena report would show it) · rework minutes per soft vs. hard denial · top 3 denial reasons by volume · dollar value of claims held at month end.
- **Workflow (screen share promised by Grace):** the open-slip → claim path in Athena · where biller edits happen · what a clearinghouse rejection with codes looks like · how notes get attached for payer requests · where an outside tool could hand them a work queue **without adding clicks**.
- **Coding/bundling (Grace's deferred topic):** how AI-suggested codes should be surfaced and verified · modifier-handling expectations · **time-based vs. MDM coding mix across their providers** · what evidence makes a suggestion trustworthy in under 30 seconds · whether suggestions belong pre-slip (provider-side) or at biller review.
- **Commercial (the CardCapture brief's open gap):** would MCD pay for the self-pay chase agent + card collection as a standalone · what monthly range feels sane (per provider? per practice?) · who signs (Greg/Kathleen?) · what would make it a "no."
- **Systems:** Acuity replacement decision + timeline · what Athena report/API access the billing team has for producing the self-pay list · sending domain/inbox for patient outreach · **direct-contract roster source of truth.**
- Pricing was **not** discussed on this call.

---

## 4. Verbatim anchors (attribution by context; timestamps approximate)

- **~5:20, Grace** — missing cards are "the number one problem for telehealth... at least for our telehealth industry, our telehealth business."
- **~16:00, Grace** — a provider's "claims from Optum, UHC, get denied because it didn't clearly say that it was a telehealth visit, how long it was, and where it happened."
- **~18:30, Grace** — "Because the provider said brittle hair, I can use that diagnosis to help the patient get coverage for a lab for hypothyroidism."
- **~22:45, Grace** — "If we could somehow ensure that we know if it's a direct contract patient so that we never... avoid charging them a self-pay fee."
- **~24:00–25:20** — Athena "has that [card OCR]... it's not always accurate. It sometimes picks the wrong payer." John's confirmed paraphrase: "the issue... isn't that their system doesn't work well. It works well enough... it's just that the patients aren't using it the way they're supposed to."
- **~27:40, Grace** — "somewhere between three and 500 claims to process in the next few days. And there's three of us." The unprocessed ones are "all the ones that have been designated self-pay where there probably is an insurance card."
- **~28:35, Johanna** — "Is there a way to extract continuously through the month self-pay patients that were missing information and... hopefully get their attention to submit what we're missing or maybe confirm that they are self-pay?"
- **~31:50, Johanna** — "When they upload that information... will it be able to sync into their chart in Athena?"

---

## 5. Repo integration

- **Path:** `docs/discovery/2026-08-06-mcd-billing-team.md` (this reconstruction may be committed as-is; replace with the original from the Aug 10 chat's download links if retrieved — original supersedes). Raw Fathom transcript → `docs/discovery/transcripts/2026-08-06-mcd-billing-team.txt` (export from Fathom).
- **Cross-references:** requirements synthesis F2/F3/F5/F6/F9 (this call is their primary evidence); Christa doc (same day, same practice, provider side); Sherin doc §1.5 (workflow insertion point — providers enter initial slip codes, billers review); CardCapture brief §7 (this call = discovery conversation 1).
- **After Aug 13:** answer §3 questions inline or in a `-2` follow-up doc per the original's instructions; promote F5/F9 configuration from hold to build.
