# Customer Discovery Synthesis — Maura Buete, MCD Provider (Billing-Focused)

**Date:** July 24, 2026 · 19 min · Recording: https://fathom.video/share/_q-3RTbmMeQkg2uSjfHQxMajfM8fuzzj
**Participants:** Maura Buete (MCD family physician, St. Petersburg FL) — with
John Clemente (CosmaNeura)
**Sequence:** Conversation **#2 of 6**.
**Scope note:** Billing-first synthesis per corpus owner's instruction. Product
updates recorded compactly in §4.
**Quote caveat:** Fathom labels scrambled in places; one Athena line is garbled
in the source (flagged in §3).

**How to use this document (Claude Code sessions):** Customer evidence, not spec.
Nothing in discovery overrides the anti-inflation invariant or PHI boundaries.

## 1. Billing headline findings

1. **Family-medicine breadth is a fourth independent demand signal — and the
   first from outside the RRM niche.** Reaction to the billing-module pitch:
   *"It's so hard being a family physician because I'm doing a million different
   things… somebody come in for a hangnail and then somebody… anything in
   between, fertility awareness."* Reception positive (*"Cool. Very good."*). The
   corpus's provider evidence had skewed RRM/fertility (Sherin, Christa); Maura
   extends it to general family practice, where the coding surface is widest and
   code suggestion helps most.
2. **The prompt-conservatism warranty was made here first — and in its strongest
   form.** John, on the record: *"I've been very conservative to say that it's not
   going to be any kind of up-billing thing… if it suggests the code, you can be
   pretty sure that you've met all the criteria and you're not over billing."*
   This is chronologically the *earliest* recorded instance of the pattern later
   flagged on the Sherin call. It is also a stronger claim: not just "won't
   inflate" but "criteria are met." That sentence is only true if provider-facing
   suggestions route through the deterministic guardrail *and* the
   DocumentationIntegrityAgent's evidence checks — a prompt cannot warrant it.
   The `CLAUDE.md` hard rule (billing surfaces route through the engine) is
   load-bearing for promises already made to at least two customers.
3. **The two-sided product was described to a customer in market language.**
   *"There'll be something for Greg and Kathleen's medical coders where they can
   look at it from their perspective and tune the model."* That is the
   ProviderCard + coder-review + FeedbackAgent loop, stated as a commitment.
   Record it as customer-facing scope.
4. **Time-based coding entered the conversation.** John tied *"the number of
   minutes you did a visit"* to the billing module. 2021+ E/M rules allow leveling
   by **total time or MDM**; the engine documented the MDM 2-of-3 pathway only.
   Family-medicine televisits are frequently time-coded. *(Resolved Aug 17: build
   the time pathway. See the Aug 13 doc §3 Q7.)*
5. **Athena's own AI is now visible inside the design partner.** Maura heard
   during an Athena training that Athena is launching something (transcript
   garbled — plausibly their ambient/AI documentation capability; **verify**).
   The trainer was also auditing her time-per-patient and saw the copy-paste
   workflow. John's stated counter-moat, to the customer: *"no one can do what I
   can do in terms of saying, tell me what new feature you want, and I'll build
   it… Athena's not going to do that."* Note the moat pitched to *users* is
   forward-deployed velocity, while the internal long-term moat thesis is the data
   feed. Both can be true; keep them straight in materials.
   *(Corroborated Aug 12: Greg forwarded Athena's Ambient AI assistant
   announcement.)*

## 2. Commercial state after this call

- **Free-runway-end speech delivered** (*"I've been giving this away for free as
  long as I can… I'm going to need to start charging"*). Maura agreed to a
  **trial-then-decide**: use the Zoom feature ~2 weeks–1 month, then circle back
  on the $59/month subscription. No payment yet.
- **Value bar stated:** *"If I'm asking for $59 a month, I want to be providing
  $600 a month worth of value."* Useful internal framing (10× value-to-price).
- **Language discipline:** John's pitch included *"help you increase your
  revenue."* Approved positioning is *revenue capture of documented, medically
  necessary care* — never revenue generation/increase. Small drift, worth
  correcting in the outreach kit before it propagates; the *"you're not over
  billing"* half of the pitch is the right half.

## 3. Athena friction detail (feeds existing workstreams — no new build)

- Nothing auto-populates; patients added manually; note copy-pasted into chief
  complaint/subjective; assessments added separately; labs uploaded separately.
  Corroborates Christa's note-chopping (Aug 6) → **F4 section splitter / F7 auto-
  populate**, and adds the assessments/labs steps to its requirements.
- Athena intake is weaker than Tebra was (*"not a whole lot of intake coming in
  from the patients"*), so she does intake in-visit → strengthens the intake-bot
  relevance for MCD-on-Athena.
- Acuity calendar sync broken for her account — operational item.

## 4. Non-billing items on this call (compact record)

Zoom meeting-bot connected live on the call (she'll parallel-record as backup for
the first visits). Personality-profile feature demoed (*"self-directed"*); she
didn't initially grasp the implications — onboarding copy needed if it ships
broadly. "AI forward-deployed engineer" GTM framing stated to a second customer.
Magisterium described as a final-say scrubber for ERD/Catholic-teaching
conformance — an architectural promise now on customer record. Praise for
transcription's chat-filtering (*"it weeds out… catching up with friends"*) —
retention signal.

## 5. Quote anchors (timestamps approximate)

- ~8:03 — copy-paste into chief complaint/subjective; *"add all of the assessments
  and then separately upload the labs."*
- ~8:50 — Athena trainer audited her time-per-patient; heard Athena is launching
  [garbled — verify] during training.
- ~9:28 — *"no one can do what I can do… Athena's not going to do that."*
- ~9:45 — free-runway-end + subscription notice.
- ~12:17 — *"$59 bucks for a 24/7 virtual assistant seems pretty reasonable"*;
  minutes-per-visit tied to billing module.
- ~12:48 — *"It's so hard being a family physician… I'm doing a million different
  things."*
- ~13:17 — *"very conservative… not going to be any kind of up-billing thing… if
  it suggests the code, you can be pretty sure that you've met all the
  criteria."*
- ~13:17 — *"something for Greg and Kathleen's medical coders… tune the model that
  way."*
- ~17:58 — *"$59 a month… $600 a month worth of value."*

## 6. Repo integration

- **Path:** `docs/discovery/2026-07-24-mcd-provider-maura-buete.md`. Transcript
  not committed.
- **Corpus chronology:** see `README.md`.
- **Cross-references:** Sherin doc §3/§4 (the conservatism flag — this call is
  instance #1 chronologically); Christa doc §3.2 (note auto-populate — this call
  adds assessments/labs steps); requirements synthesis §1 (pipeline routing rule),
  §3 F1.7 (E/M leveling scope), §4.6 (positioning language).
