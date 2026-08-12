# Customer Discovery Synthesis — Scott Weeman, Fiat Health (Billing-Focused)

**Date:** July 21, 2026 · 17 min · Recording: https://fathom.video/share/KrLvvyUoSfw6z64gJwYKMaw5_oxwsyWf
**Participants:** Scott Weeman (therapist, Fiat Health / fiathealth.org — behavioral health) — with John Clemente (CosmaNeura)
**Sequence:** Chronologically discovery conversation **#1 of 6** — the earliest call in the corpus (see chronology in the Maura doc, §6).
**Scope note:** Billing-first synthesis per corpus owner's instruction; product asks recorded compactly in §3.
**Why this doc matters despite thin billing content:** it holds the corpus's earliest pricing evidence, its first willingness-to-pay "yes," and the first customer outside the design partner.

**How to use this document (Claude Code sessions):** Customer evidence, not spec. Nothing in discovery overrides the anti-inflation invariant or PHI boundaries.

---

## 1. Billing & commercial headline findings

1. **First verbal "yes" to $59/month — from a second organization — then immediately waived.** John asked directly: "Are you okay with trying to start paying, say, 59 bucks a month… that's the rate I have with Greg and Kathleen." Scott: "Yeah, I'm sure. I'll probably have Fiat cover the cost… Carolina would be happy to do that." John then offered to waive the fee in exchange for evangelism to other therapists/providers; Scott accepted ("Happy to provide whatever kind of endorsement or support I can"). **Net commercial state: willingness-to-pay verbally validated, payment count still zero, the yes converted into a GTM barter.** Record the waiver as a deliberate trade (endorsement channel), not a pricing failure — but the corpus's open question ("will anyone pay?") remains open, and the first person who said yes was let off.
2. **The $59 anchor is older than the corpus previously showed.** "The rate I have with Greg and Kathleen" was stated as an existing fact on **July 21** — so the leadership anchor predates the Aug 6/Aug 10 references. Pricing-state summary across the corpus: anchored with MCD leadership since ≤ July 21; quoted to three end users (Scott 7/21, Maura 7/24, Christa 8/6); one verbal yes (waived), one trial-then-decide, zero payments. The economic-buyer question surfaced here for the first time: **the org pays, not the provider** (Fiat covering Scott). Carry that into the Aug 13 / Greg & Kathleen commercial conversations — per-provider pricing with an org buyer is a different sale than provider-pays.
3. **Second organization = first expansion signal beyond MCD.** Fiat Health is a distinct buyer in the Catholic-health orbit. One logo doesn't make a segment, but it converts "MCD design partner" into "MCD + adjacent network," which is the shape the company thesis needs.
4. **Behavioral-health billing is a different coding domain — flag the scope boundary now.** The engine's current scope is Medicare NGS J-K, E/M leveled by MDM 2-of-3. Therapy billing runs on time-based psychotherapy CPT codes and different documentation rules. Decision to record (not build): if therapists become a paying segment, the billing engine needs a behavioral-health coding scope decision; until then, billing features marketed to Fiat-type users are out of scope, and nothing on this call committed otherwise. (No billing tool was pitched to Scott on this call.)

---

## 2. Compliance-adjacent finding (brief, but it completes a pattern)

Scott's current workflow: full **therapy session transcripts pasted into a personal ChatGPT account**, one standing per-client conversation, used to prep the next session ("here's my recent transcript, help me organize my next session… using internal family systems therapy"). This is the second instance of the corpus's shadow-AI pattern (Christa: labs in personal Google Sheets) — and the more sensitive one, since psychotherapy content carries heightened protection. The native session-prep bot John committed to build is the compliant replacement, and that's the honest way to position it — **but only after CosmaNeura's own gates close** (BAA/ZDR, deployment layer). Do not market the compliance angle before then; do not let the session-prep feature ship against real transcripts before then either. Same rule as the lab tool.

---

## 3. Non-billing asks on this call (compact record — these are the actual build requests)

1. **Native session-prep bot** inside the dashboard: reads recent session(s), produces a short recap + suggested outline in the therapist's modality (IFS named); output **shorter** than his current ChatGPT result ("this is actually a little bit too much"). John committed, with a Catholic overlay via the model stack.
2. **Patient-journey memory:** sessions currently stand alone; he wants cross-session continuity informing both prep and (lightly) the notes ("we continued on this thread…"). John confirmed per-session is the current architecture and agreed to string sessions into a journey.
3. **Client/therapist terminology toggle:** replace doctor/patient labels for behavioral-health users. John: likely an easy fix.
4. Zoom meeting-bot positively received (lets him keep an earbud in — his phone-recorder workflow fails with AirPods).
5. Networking: BAIF Rome intro (John to email contact); Fiat endorsement/evangelism per §1.

---

## 4. Quote anchors (timestamps approximate)

- ~6:30 — "I'll take the most recent session's transcript… I've got, for each of my clients… a working conversation [in ChatGPT]."
- ~11:01 — prep workflow: "Here's our most recent transcript. Build me some talking points and an outline…"
- ~13:39 — "This is actually a little bit too much" (output-length preference).
- ~14:18 — client/therapist toggle request.
- ~15:20 — "Are you okay with trying to start paying, say, 59 bucks a month… that's the rate I have with Greg and Kathleen." / "Yeah, I'm sure. I'll probably have Fiat cover the cost."
- ~15:52 — the waiver-for-evangelism trade; "Happy to provide whatever kind of endorsement or support I can."

---

## 5. Repo integration

- **Path:** `docs/discovery/2026-07-21-fiat-provider-scott-weeman.md`, transcript at `docs/discovery/transcripts/2026-07-21-scott-weeman.txt`.
- **Corpus position:** #1 of 6 chronologically; see the Maura doc §6 for the full corrected chronology.
- **Cross-references:** Christa doc §4 (shadow-IT pattern — this is instance #2, higher sensitivity); Maura doc §1.2/§2 (pricing timeline and the $59 anchor's true age); groundwork brief pricing state (update: anchored ≤ 7/21; org-pays evidence); engine brief §6 (payer/coding scope — behavioral-health boundary recorded here).
- **Groundwork-brief deltas from this doc:** (a) pricing state rewrite per §1.2; (b) add "economic buyer: org vs provider" as an explicit open commercial question; (c) add behavioral-health coding scope to the out-of-scope ledger with a revisit trigger ("first paying therapist org"); (d) session-prep bot joins the gated-features list (PHI gates identical to the lab tool).
