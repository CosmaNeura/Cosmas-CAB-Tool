# Customer Discovery Synthesis — Maura Buete, MCD Provider (Billing-Focused)

**Date:** July 24, 2026 · 19 min · Recording: https://fathom.video/share/_q-3RTbmMeQkg2uSjfHQxMajfM8fuzzj
**Participants:** Maura Buete (MCD family physician, St. Petersburg FL) — with John Clemente (CosmaNeura)
**Sequence:** Chronologically discovery conversation **#2 of 6** (see corpus chronology in §6). Predates the Aug 6–10 interviews already in the corpus.
**Scope note:** Per corpus owner's instruction this synthesis is billing-first. Product-update content (Zoom bot rollout, personality profiles, intake bot, acuity sync bug) is recorded compactly in §4.
**Quote caveat:** Fathom labels scrambled in places; one Athena line is garbled in the source (flagged in §2.5).

**How to use this document (Claude Code sessions):** Customer evidence, not spec. Billing statements here bear directly on the `cosmaneura_billing` engine's provider-facing surface (ProviderCard / billing tab) and on customer-facing compliance language. Nothing in discovery overrides the anti-inflation invariant (hold / downgrade / remove only) or PHI boundaries.

---

## 1. Billing headline findings

1. **Family-medicine breadth is a fourth independent demand signal — and the first from outside the RRM niche.** Reaction to the billing-module pitch: "It's so hard being a family physician because I'm doing a million different things… somebody come in for a hangnail and then somebody… anything in between, fertility awareness." Reception positive ("Cool. Very good."). The corpus's provider evidence to date skewed RRM/fertility (Sherin, Christa); Maura extends it to general family practice, where the coding surface is widest and code suggestion helps most.
2. **The prompt-conservatism warranty was made here first — and in its strongest form.** John, on the record: "I've been very conservative to say that it's not going to be any kind of up-billing thing… **if it suggests the code, you can be pretty sure that you've met all the criteria and you're not over billing.**" This is chronologically the *earliest* recorded instance of the pattern later flagged on the Sherin call (Aug 10). It is also a stronger claim: not just "won't inflate" but "criteria are met." That sentence is only true if provider-facing suggestions route through the deterministic guardrail *and* the DocumentationIntegrityAgent's evidence checks — a prompt cannot warrant it. The proposed `CLAUDE.md` hard rule (billing tab routes through the engine) is now load-bearing for promises made to at least two customers.
3. **The two-sided product was described to a customer in market language.** "There'll be something for Greg and Kathleen's medical coders where they can look at it from their perspective and tune the model." That is the ProviderCard + coder-review + FeedbackAgent loop, stated as a commitment. Alignment is good — the architecture already exists on paper; record it as customer-facing scope.
4. **Time-based coding entered the conversation.** John tied "the number of minutes you did a visit" to the billing module. Engine scope question to resolve: 2021+ E/M rules allow leveling by **total time or MDM**; the engine brief documents the MDM 2-of-3 pathway only. Family-medicine televisits are frequently time-coded. Decide explicitly whether time-pathway leveling is in scope, and record the decision — silence here becomes an accidental gap.
5. **Athena's own AI is now visible inside the design partner.** Maura heard during an Athena training that Athena is launching something (transcript garbled — plausibly their ambient/AI documentation capability; verify). The trainer was also auditing her time-per-patient and saw the copy-paste workflow. John's stated counter-moat, to the customer: "no one can do what I can do in terms of saying, tell me what new feature you want, and I'll build it… Athena's not going to do that." Consistent with the CardCapture brief's read (Athena improves the easy cases; the wedge is messy cases + bespoke service) — but note the moat pitched to *users* is forward-deployed velocity, while the internal long-term moat thesis is the data feed. Both can be true; keep them straight in materials.

---

## 2. Commercial state after this call

- **Free-runway-end speech delivered** ("I've been giving this away for free as long as I can… I'm going to need to start charging"). Maura agreed to a **trial-then-decide**: use the Zoom feature ~2 weeks–1 month, then circle back on the $59/month subscription. No payment yet.
- **Value bar stated:** "If I'm asking for $59 a month, I want to be providing $600 a month worth of value." Useful internal metric framing (10× value-to-price); fine for materials.
- **Language discipline (working-agreements item):** John's pitch included "help you increase your revenue." Approved positioning is *revenue capture of documented, medically necessary care* — never revenue generation/increase. Small drift, worth correcting in the outreach kit before it propagates; the "you're not over billing" half of the pitch is the right half.

---

## 3. Athena friction detail (feeds existing workstreams — no new build)

- Nothing auto-populates; patients added manually; note copy-pasted into chief complaint/subjective; assessments added separately; labs uploaded separately. Corroborates Christa's note-chopping (Aug 6) → **note auto-populate** workstream, and adds the assessments/labs steps to its requirements sketch.
- Athena intake is weaker than Tebra was ("not a whole lot of intake coming in from the patients"), so she does intake in-visit → strengthens the **intake bot** relevance for MCD-on-Athena.
- Acuity calendar sync broken for her account — operational item, John to raise with Greg.

---

## 4. Non-billing items on this call (compact record)

Zoom meeting-bot connected live on the call (she'll parallel-record as backup for the first visits — sensible rollout advice from John). Personality-profile feature demoed ("self-directed"); she didn't initially grasp the implications — onboarding copy needed if it ships broadly. "AI forward-deployed engineer" GTM framing stated to a second customer. Magisterium described as a final-say scrubber for ERD/Catholic-teaching conformance — an architectural promise now on customer record. Praise for transcription's chat-filtering ("it weeds out… catching up with friends") — retention signal.

---

## 5. Quote anchors (timestamps approximate)

- ~8:03 — copy-paste into chief complaint/subjective; "add all of the assessments and then separately upload the labs."
- ~8:50 — Athena trainer audited her time-per-patient; heard Athena is launching [garbled — verify] during training.
- ~9:28 — "no one can do what I can do… Athena's not going to do that."
- ~9:45 — free-runway-end + subscription notice.
- ~12:17 — "$59 bucks for a 24/7 virtual assistant seems pretty reasonable"; minutes-per-visit tied to billing module.
- ~12:48 — "It's so hard being a family physician… I'm doing a million different things."
- ~13:17 — "very conservative… not going to be any kind of up-billing thing… if it suggests the code, you can be pretty sure that you've met all the criteria."
- ~13:17 — "something for Greg and Kathleen's medical coders… tune the model that way."
- ~17:58 — "$59 a month… $600 a month worth of value."

---

## 6. Repo integration & corrected corpus chronology

- **Path:** `docs/discovery/2026-07-24-mcd-provider-maura-buete.md`, transcript at `docs/discovery/transcripts/2026-07-24-maura-buete.txt`.
- **Corrected chronology (filenames sort correctly; treat # labels in earlier docs as superseded):**
  1. 2026-07-21 — Scott Weeman, Fiat Health (behavioral health)
  2. 2026-07-24 — Maura Buete, MCD family medicine (this doc)
  3. 2026-08-06 — MCD billing team (Grace, Johanna, Edgar)
  4. 2026-08-06 — Christa Thornberry, MCD provider
  5. 2026-08-10 — Sherin George, MCD provider
  6. 2026-08-13 — biller follow-up (upcoming)
- **Aug 13 agenda additions (biller follow-up):** (a) Do providers' claims come in time-coded or MDM-coded, and does the team ever re-level on time? (b) What has the billing team heard about Athena's upcoming AI/automation features from their side of the rollout?
- **Cross-references:** Sherin doc §3/§4 (the conservatism flag — this call is instance #1 chronologically); Christa doc §3.2 (note auto-populate — this call adds assessments/labs steps); engine brief §5–§6 (ProviderCard/coder loop; E/M leveling scope), §10 (positioning language); CardCapture brief §5 (Athena competitive read).
