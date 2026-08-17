# Customer Discovery Synthesis — MCD biller follow-up (Grace Targonski, Johanna Adriano)

**Date:** August 13, 2026 · **Participants:** Grace Targonski (Director of
Billing, CPC), Johanna Adriano — with John Clemente (CosmaNeura)
**Sequence:** Conversation **#6 of 6**. This is the follow-up the Aug 12
requirements synthesis (v1.1 §8) was written to await.
**Source:** Zoom meeting summary supplied by Grace by email, Aug 13 21:44 UTC.
No recording or full transcript was available — Grace confirmed Aug 13 22:05.
**Caveat:** the Zoom summary's speaker attribution is unreliable (it labels
CosmaNeura's demo and roadmap statements as Grace's, and renders Johanna as
"Joanna"/"Joe"). Statements below are attributed by content, not by the
summary's labels. Treat exact wording as paraphrase, not quotable.

**How to use this document (Claude Code sessions):** Customer evidence, not spec.
Nothing here overrides the anti-inflation invariant or the PHI boundary.

## 1. Headline findings — three new requirements the Aug 12 synthesis predates

1. **CPT 99058 (rapid-access / disrupted-schedule add-on) is unclaimed revenue.**
   The team wants a tool that helps providers apply 99058 correctly on
   rapid-access visits; missing it is straight lost revenue. This is the corpus's
   first explicit request for a **procedure/add-on code**, not an E/M level or a
   diagnosis. Engine impact: the pipeline has never proposed a `PROCEDURE`-kind
   code — the schema field exists and no agent populates it. 99058 also carries a
   CMS MUE of 0 with a clinical date-of-service edit, so it must be proposed as a
   provider-confirmable candidate with evidence, never auto-captured.
2. **The "standardized blurb" is now confirmed twice, and its placement is
   specified.** The team wants key billing metadata **at the top of the note** —
   length of visit, place of service, visit-type confirmation, patient consent.
   This is the second independent statement of the completeness/attestation ask
   (first: Grace 8/6, *"run something that reviews their note and says, hey, you
   forgot this, you forgot this… and maybe they can override it"*). It confirms
   F2's attestation-header design rather than changing it.
3. **Telehealth notes must be documented within 24–48 hours to be billable.**
   A hard timing constraint not previously in the corpus. Engine impact: the
   completeness checker's output is time-sensitive — a note failing telehealth
   rules is not merely denial-prone, it can fall out of billability entirely.
   Worth surfacing as urgency in the provider-facing view.

## 2. Workflow mechanics learned (feeds F1, F2, F4)

- **Athena generates the billing slip *from* the provider note.** Providers enter
  notes into Athena; Athena produces a billing slip that appears on the Athena
  dashboard; billing staff review and clean up slips before submitting claims.
  **This makes note completeness upstream of everything else** — fixing the note
  fixes the slip, which is precisely why F2 reduces rather than adds coder
  minutes.
- **"Missing slips"** are unsigned or unreviewed appointments. Providers sign off
  via an appointment-checkout flow. Billing staff can access unedited claim
  information and provider notes to finalize.
- **Note formatting is inconsistent across ~300 providers**, especially for
  telehealth: visit length and patient location details are the recurring
  omissions. The team wants standardized templates but acknowledges the
  difficulty across specialties.
- **Athena is worse than Tebra for claim modification** — limited ability to
  modify claims after the fact, which raises the value of getting the note right
  pre-submission.
- **Insurance card entry is multi-path and manual.** Patients may upload or hand-
  enter insurance during Acuity scheduling; named staff (Cindy, Maggie) enter
  missing cards into Athena after appointments. The card **image** must land in
  the right patient encounter in Athena, not merely on the patient record.
- **Payer routing complexity:** distinct trusts and EDI numbers route claims;
  thorough information gathering is required for correct routing.

## 3. Answers to the Aug 12 synthesis §8 open questions

| Q | Question | Status after this call |
|---|----------|------------------------|
| 1 | Will the practice pay ($59/user/mo, org vs provider buyer)? | **Unanswered.** Deferred to the Greg & Kathleen conversation. |
| 2 | Baseline metrics from month-end close | **Not captured.** The summary records no numbers. The one-time month-end window referenced in the Aug 12 plan appears to have passed uninstrumented — recapture explicitly. |
| 3 | Self-pay chase mechanics (sequencing, tone, escalation, approver) | **Unanswered.** Discussion centred on card chase, not self-pay balances. F5 mechanics remain on hold. |
| 4 | Modifier workflow + coding/bundling mechanics | **Still deferred.** Not covered. F6's modifier surfacing stays unspecified. |
| 5 | Direct-contract list: where it lives, who maintains it | **Unanswered.** F3 continues to build against a versioned local list with fail-closed semantics. |
| 6 | Athena screen-share: real section names / paste targets | **Partially answered** — billing-slip and missing-slip flows were demonstrated, but paste-target section names were not enumerated. F4 labels remain provisional. |
| 7 | Time-coded vs MDM-coded; does the team re-level on time? | **Not answered directly.** Owner decision (Aug 17): **build the total-time pathway** alongside MDM. Rationale: family-medicine televisits are frequently time-coded (Maura 7/24) and Sherin referenced 99215 (8/10). |
| 8 | What has the billing team heard about Athena's own AI? | **Not answered** in the summary. Separately, Greg forwarded Athena's Ambient AI assistant announcement on Aug 12 — Athena is moving on ambient documentation. |
| 9 | Card chase today: who, channels, touches, minutes, outstanding count | **Qualitatively answered, quantitatively not.** Named staff and the Acuity→Athena path are known; no touch counts or minutes-per-card were recorded. The Aug 6 call remains the only source of a number: **300–500 claims held monthly**. |
| 10 | Patient-contact channels & consent (F9b config) | **Not answered by the call; decided by the owner (Aug 17).** Channels: **email yes** (no per-patient opt-in needed for an established patient), **SMS yes** but per-patient TCPA consent required and fail-closed, **automated outbound voice no** — phone stays a staff task. What remains open is narrower than the original question: **where the SMS opt-in flag comes from** (Acuity, Athena, or a practice-run form). With no such feed, `consented(SMS)` is False for everyone and the cadence runs on email alone. |
| 11 | Should direct-contract patients ever be card-chased? | **Unanswered.** F9a continues routing direct-contract matches to human review. |

**Net:** the call was rich on *workflow mechanics* and produced three new
requirements, but it did not deliver the **numbers** (Q2, Q9) or the
**configuration inputs** (Q3, Q4, Q10, Q11) the Aug 12 plan expected. Those
remain open and should be asked for explicitly rather than assumed answered.
Q10 has since been settled by owner decision rather than by the call — the row
above records it, and the residual question is a data-source question, not a
policy one.

## 4. Requests recorded but out of engine scope

- Ticket system for patient email — future, noted.
- Acuity-replacement scheduling program — belongs to `cosmaneura-scheduling`
  (see the expansion plan), not this repo.
- Card-chase automation with Athena OCR — belongs to the CardCapture /
  athenaNet workstream, not this repo. The team's confidence estimate on record:
  automation could eliminate **80–90%** of current card-procurement tasks.

## 5. Repo integration

- **Path:** `docs/discovery/2026-08-13-mcd-biller-followup.md`. No transcript
  available; the email summary is the primary source.
- **Requirements-synthesis deltas (for the next revision, v1.2):**
  1. Add **F10 — procedure/add-on code proposals**, with 99058 as the first rule.
     Prerequisite: the coding agent gains `PROCEDURE` capability at all.
  2. F2: promote the attestation block from "emit on pass" to a **specified
     note-header block** (visit length, place of service, visit-type
     confirmation, consent) and add the **24–48 hour** billability window as
     provider-facing urgency.
  3. F2 rationale strengthened: Athena derives the billing slip from the note,
     so note-side fixes are structurally upstream of coder minutes.
  4. §8 Q2 and Q9 must be re-asked — the month-end baseline window was missed.
  5. Record the time-pathway decision (Q7) as **resolved: build it**.
- **Cross-references:** billers 8/6 (card bottleneck, POS 10/02 denials, the
  completeness ask in Grace's own words); Maura 7/24 (time-coded televisits,
  copy-paste friction); Sherin 8/10 (billing tab, multi-code granularity).
