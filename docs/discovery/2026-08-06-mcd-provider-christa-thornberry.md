# Customer Discovery Synthesis — Christa Thornberry, MCD Provider (Pilot User)

**Date:** August 6, 2026 · 40 min · Recording: https://fathom.video/share/C6yBtMr_AChw5e-VpSyQo_5TnagyDpFn
**Participants:** Christa Thornberry (MCD provider; active pilot user and volunteer evangelist) — with John Clemente (CosmaNeura)
**Organization:** MCD (MyCatholicDoctor) — the design-partner org (hybrid telehealth + in-person, recently migrated to athenaOne)
**Sequence:** Chronologically discovery conversation **#2** — held the same day as, and immediately after, the biller-team call (John on the recording: "I just got off a conversation with Grace and Johanna"). Slots between `2026-08-06-mcd-billing-team.md` (#1) and `2026-08-10-provider-sherin-george-billing.md` (#3). The Aug 13 biller follow-up will be #4.
**Interview status:** A product-update call (Zoom-bot rollout, settings, in-platform AI) that became discovery. No follow-up interview scheduled; five commitments were recorded as action items (§6).
**Quote caveat:** Fathom's speaker labels are scrambled in several passages (e.g., the HIPAA exchange ~20:08–20:35); quotes below are attributed by context. The transcript alternates "Cosmas"/"Cosmos"; standardized to Cosmos here.

**How to use this document (Claude Code sessions):** Customer evidence, not a spec. It maps Christa's statements onto the three surfaces — the shipped **Cosmos platform** (SOAP notes, intake tools, new Zoom meeting bot), the **`cosmaneura_billing` engine**, and the **CardCapture / Athena integration** workstream. Every commitment in §6 is constrained by the standing gates (BAA/ZDR, deployment layer, Athena sandbox → Marketplace). Nothing in discovery ever overrides the anti-inflation invariant (hold / downgrade / remove only) or the PHI boundaries.

---

## 1. Headline findings

1. **Third independent under-billing confirmation in four days.** Unprompted: "My guess is — and I'm sure this is probably true for a lot of us — we're **continually under-billing**." Billers (Aug 6) → Christa (Aug 6) → Sherin (Aug 10). The engine's core thesis is now voiced on all three sides of the workflow, including by the provider with the *least* financial motive.
2. **Shadow lab spreadsheets are a segment-wide practice — and a new validated feature lane.** Providers (Christa, Sandy, "a lot of people") hand-enter lab values into personal Google Sheets outside the chart because longitudinal lab review is effectively impossible in Athena. It's the fertility/functional-medicine segment's core workflow — and a live HIPAA exposure CosmaNeura can be the compliant replacement for.
3. **Athena integration pressure is now platform-wide, not CardCapture-scoped.** This call adds two more customer-voiced asks that block on the same sandbox → Marketplace clock: lab *read* and note auto-populate *write*. Combined with the insurance write (CardCapture) and Johanna's card-to-chart sync question (Aug 6), four asks now sit behind a clock the CardCapture brief flagged as "not confirmed started." John committed on-call to entering the sandbox.
4. **Adoption, not product depth, is the commercial bottleneck.** ~6–12 regular users against a provider base of ~180 (engine handoff brief) at $59/month. Even the best evangelist can't get colleagues — including Kathleen in leadership — to *try* it. Her diagnosis: email is dead; provider-to-provider demonstration works. She volunteered to run demos.
5. **Strongest retention evidence in the corpus:** "If I had to go back to doing it without your tool, I would probably quit." Sandy is retention story #2 ("She told me she was going to quit").

---

## 2. User profile — why her evidence matters

Christa is the low-volume, mission-driven end of the MCD spectrum: day job as a contract pediatric hospitalist (two 72-hour shifts/month); MCD panel of ~10–15 patients/month, kept "because I believe in the mission"; her husband wants her to stop because the economics don't work. She reports **3–4 hours of admin per billable hour** — lab entry, research, note assembly. Two implications:

- **Her value evidence is conservative.** She has no revenue motive and still volunteered the under-billing observation and the 3–4:1 admin ratio. When the least-motivated user confirms the thesis, it's the strongest form of the evidence.
- **She is the retention floor and the GTM channel.** The tool is the stated difference between her staying and quitting MCD; she has already made how-to videos, repeatedly offered Zoom demos, and articulated the adoption mechanism better than anyone: "doctors are much more likely to listen to someone else who's in the same position."

---

## 3. What she asked for, precisely

1. **Longitudinal lab tracking with AI analysis — replacing her manual spreadsheet.** Extract lab values (source today: Athena, where "the labs are so hard to look at" and "there's no way to look over time"), populate a per-patient longitudinal view, auto-update as new results arrive, and layer analysis/highlighting ("good, bad, or other") in her interpretive frame. Agreed on the call: **the provider verifies every extracted value** before it counts. Segment insight: "for the fertility people, that would be super helpful too, because they use so many labs. Their whole thing is labs."
2. **Athena note auto-populate.** Today she pastes the generated note into HPI, then manually cuts ROS / assessment / plan into their Athena sections. Ask: one action, sections land where they belong. John: "there is a way to do that… I need to get into Athena's sandbox."
3. **In-platform AI chat over patient context** — validates the in-flight feature. She already round-trips to ChatGPT and OpenEvidence for labs and supplements (Sandy taught her the pattern) and wants it "directly tied to my note."
4. **Billing assistance, reframed as a double-check.** Reaction to the billing-tab pitch: MCD providers code their own visits, Athena has made it harder ("asking us for more information and linking of things"), and her instinct is chronic under-billing. For her the value is "double-checking your work" and easing the Athena interface — not code volume. That's the ProviderCard posture, stated by a customer.

---

## 4. The shadow-spreadsheet finding (biggest new thing on this call)

**What she showed:** a personal Google Sheet, outside the chart, rows keyed by patient initials, hand-entered lab values over time — used to manage a months-long thyroid case and her fertility patients. Whoever "has time first" — her or the patient — enters values.

**Why it exists:** longitudinal lab review is effectively impossible in the source systems. "It's impossible to process all the information just looking at what the lab sends you… now we're using Athena and the labs are so hard to look at. And there's no way to look over time." This is workflow-driven shadow IT, not laziness.

**How widespread:** "a lot of people are using the same thing where they're hand-entering things." She names the fertility/NFP and functional-medicine segments specifically — i.e., MCD's RRM core (see the Sherin doc). Sandy runs the same pattern and was near quitting over the workload.

**The compliance kicker, raised on the call itself:** the spreadsheet is patient lab data living in a personal Gmail account ("probably from a HIPAA perspective, not no bueno" — label-scrambled exchange; both parties acknowledged the problem; her mitigation is initials-only, which she concedes forces her to memorize who everyone is).

**Positioning implication:** the lab tool is simultaneously the time-saver (provider pitch) and the **compliant replacement for existing shadow IT** (leadership pitch — risk removal). For a company whose premise is trust, "we take patient lab data out of personal Google Sheets" is a strong line — but it can only be said once CosmaNeura's own PHI gates are closed (§6). Do not market it before then.

---

## 5. Billing engine validation, provider side #2

- **The thesis, in her words:** "My guess is — and I'm sure this is probably true for a lot of us — we're continually under-billing." Combined with the 3–4:1 admin-to-billable ratio, this is the quantified pain the engine exists to fix: legitimately earned, documented care going unrecovered.
- **MCD does its own billing — Athena's revenue-cycle service is not in play.** Per the call: Athena's standard model is tech given away in exchange for ~8% of collections; Greg & Kathleen negotiated tech-only and kept billing internal. The billing team *is* the revenue cycle, CAB augments them, and there is no Athena billing service to displace. Add to the groundwork brief's competitive section — it also explains why Athena's own billing intelligence is absent from MCD's workflow.
- **Her framing protects the invariant.** "Double-checking your work" and easing the Athena interface is exactly the ProviderCard review surface (evidence spans, held codes, rationale) — not a code-count maximizer. Nothing in this call pressures the invariant boundary the way Sherin's "more codes" phrasing did.
- **Safety anecdote — record and reuse:** she has dyslexia; the transcription AI caught a spoken mifepristone/misoprostol swap, flagged that she misspoke, and corrected the note. "That's a thing that you could really get into a medical-legal issue over." Documentation-integrity value, stated by the customer, in exactly the risk register a Catholic medical group cares about. This belongs in the outreach kit.

---

## 6. Commitments John made on the call — ledger with gates

| # | Commitment (recorded action items) | Gates before real data | Zero-gate interim |
|---|---|---|---|
| 1 | Enable Zoom meeting-bot auto-join for Christa; verify it records/transcribes into Cosmos | Shipped feature — none | Done on call. Watch: she hit connect-flow friction live (kicked out mid-login; unsure whether to "go through Cosmos"). Onboarding-friction data point for §7. |
| 2 | **Lab extraction + longitudinal analysis, integrated with Athena labs** | Real lab values + patient identity = PHI → BAA/ZDR + deployment layer (auth, encryption at rest, access controls). Athena **read** scope → sandbox → Marketplace. | Design the data contract; build against **synthetic lab fixtures** (same pattern as CardCapture's synthetic cards); add a mock Athena lab-read endpoint to the existing mock harness. |
| 3 | **Athena note auto-populate into sections** (HPI / ROS / A&P) | Sandbox → Marketplace. Encounter-section **write** API is *unverified* — repo knowledge covers the insurance write from public docs, not clinical-note section writes. Validate in sandbox before promising behavior. | A section-splitter that outputs Athena-shaped blocks (one paste per section) kills most of the manual chopping today with zero integration. |
| 4 | Provider outreach kit (email + links + videos) for Christa to distribute; propose a Christa-led Zoom demo to Greg & Kathleen | None | **Cheapest, highest-leverage item from this call — do first.** |
| 5 | Follow up with Maureen McCarthy; onboard her end-to-end | None | — |

**Posture note:** the standing recommendation (CardCapture brief §2) is *pause new engineering; go validate*. These calls are the validation — but build commitments are stacking across customers: Sherin's billing tab (~1–2 weeks stated), Christa's lab tool, note auto-populate, Sandy's custom lab resources. The groundwork brief's tiering should absorb these as **step-0 entries** (data contract + mock + synthetic fixtures), and customer-facing timelines should track the gates, not enthusiasm. The lab tool in particular must not touch a real lab value before the BAA and deployment layer exist.

---

## 7. Adoption & commercial intelligence

- **Penetration baseline:** "half a dozen to a dozen" regular users against ~180 MCD physicians (engine handoff brief) ≈ **3–7%**. At $59/month that is roughly $350–700 MRR from the design partner. The revenue constraint is adoption, not features.
- **Pricing anchor hardened.** $59/month is now quoted to an end user on the record ("for 59 bucks a month, which is what I'm charging"), and the end of the free runway was stated ("I'm kind of at the end of this runway thing where I can't give it away for free anymore"). Combined with the Sherin-call anchor (agreed in principle with Greg & Kathleen), pricing has moved from *undetermined* → *anchored but unpaid*. The payment question rides with Aug 13 and the Greg & Kathleen conversation. Constraint on record: the agreement with Kathleen prohibits forcing usage or payment — adoption must be pull, not push.
- **The adoption diagnosis, from the best evangelist available:** even she can't get colleagues to *try* it — including Kathleen (leadership). Maureen McCarthy: repeated demos, videos, and emails; "three months later, she'll be like, 'what was the name of that guy?'" Email is a dead channel. Her prescription: provider-to-provider live demonstration — "this is how I approach a patient, this is how it makes my life easier" — and she has offered to run them. John's read matches: "once they use it, they'll love it… until you get to that point, they're averse."
- **GTM promise on record:** "for every provider that's part of MyCatholicDoctor, build them a feature… specifically for them the way they want it." Powerful adoption lever; flag the scalability trade-off explicitly. Sandy → Christa is the healthy version of the pattern: a bespoke entry point (Sandy's lab resources) that generalizes into a segment feature (longitudinal lab tracking).
- **Values differentiation is now customer-visible:** the model routing — Magisterium (Catholic LLM layer) with GPT-class models and connectors into OpenEvidence — was described to a user. Part of the trust story; keep it accurate in outreach materials.
- **Onboarding friction observed live:** the one-click Zoom connect flow confused and briefly dropped the platform's most enthusiastic user. Small sample, real signal — the adoption problem starts inside the first click, not just in the inbox.

---

## 8. Deltas to apply (groundwork brief + CLAUDE.md)

1. **Add step-0 feature #5: longitudinal lab tracker.** Data contract (patient-scoped lab series: analyte, value, unit, date, source, provider-verified flag), synthetic lab fixtures, mock Athena lab-read endpoint. Segment tag: RRM / fertility / functional medicine. PHI and Athena-read gates explicit in the entry.
2. **Elevate the Athena clock to a platform-wide blocker.** Now blocking four customer-voiced asks: insurance write (CardCapture), card-to-chart sync (Johanna, Aug 6), lab read (Christa), note auto-populate (Christa). Action unchanged, urgency higher: developer registration → sandbox → Marketplace application. John committed on-call to entering the sandbox.
3. **Add an adoption/GTM workstream** with the two ungated actions (outreach kit; Christa-led demo proposal to Greg & Kathleen) and the penetration baseline (6–12 of ~180) as its tracking metric.
4. **Record the pricing state change:** anchored at $59/user/month, now quoted to leadership *and* an end user; zero payments; free-period end announced.
5. **CLAUDE.md additions (proposed):**
   - *Lab features are provider-verified decision support: every extracted value requires provider confirmation before it enters any persisted series; analyses are review surfaces and are never auto-written to the chart.* (Mirrors CardCapture's never-auto-select rule.)
   - *Note auto-populate moves clinical content unaltered — sectioning/formatting only. The clinical note remains clinical-first (Sherin-call principle).*

---

## 9. Quote anchors (timestamps approximate; labels scrambled in places)

- ~11:30 — "for the fertility people, that would be super helpful too, because they use so many labs… their whole thing is labs."
- ~12:20 — "I manually enter them into a spreadsheet because it's so hard to follow… over time."
- ~14:40 — "now we're using Athena and the labs are so hard to look at. And there's no way to look over time."
- ~20:08–20:35 — the HIPAA exchange on the spreadsheet ("probably from a HIPAA perspective, not no bueno" / "It doesn't have the patient's name on it").
- ~21:10 — "for 59 bucks a month, which is what I'm charging."
- ~22:40 — "If I had to go back to doing it without your tool, I would probably quit."
- ~24:06 — Maureen: "three months later, she'll be like, 'what was the name of that guy?'"
- ~26:36–27:49 — dyslexia / mifepristone–misoprostol: "it actually said that I misspoke… and fixes the correct word."
- ~29:46 — Athena's ~8% model; "Greg and Kathleen negotiated… we're going to handle our own billing."
- ~30:35 — "we're continually under-billing." / "I end up spending three or four hours for every one hour that I bill for."
- ~34:03 — "She [Sandy] told me she was going to quit."
- ~36:11–37:17 — manual note chopping into Athena sections; John: "I need to get into Athena's sandbox."

---

## 10. Repo integration

- **Path:** `docs/discovery/2026-08-06-mcd-provider-christa-thornberry.md`, with the raw Fathom transcript at `docs/discovery/transcripts/2026-08-06-christa-thornberry.txt`.
- **Corpus ordering:** #1 Aug 6 billers → **#2 Aug 6 Christa (this doc)** → #3 Aug 10 Sherin → #4 Aug 13 biller follow-up (upcoming).
- **Cross-references:** biller synthesis (same practice, workflow side; Johanna's Athena-sync question joins the Athena-clock cluster); Sherin doc §2–§4 (billing tab — Christa corroborates the under-billing thesis and the review-surface framing; she is *not* a second requester of the same UI); engine brief §2 (under-coding demo) and §9 gaps (EHR intake — the Zoom bot + Cosmos note are becoming the de facto VisitBundle intake path); CardCapture brief §7 (the Athena clock, now escalated).
- **After Aug 13:** update the biller doc's verdicts per its own instructions; touch this doc only if the billing team volunteers lab-adjacent revenue angles (e.g., lab coverage coding — cf. Grace's "brittle hair" precedent).
