# MCD synthetic training quickstart

Use the CAB address supplied by your trainer and your assigned sign-in.
This session uses fictional cases only. Do not upload a real patient note or
send a message to a patient. CAB keeps your edits separate from the clinical
note; recording a review does not send a claim or write to an EHR.

## Before the session

The administrator loads the twelve-case pack
`cosmaneura-mcd-training-20260930-v1` with
`python -m cosmaneura_billing.seed_mcd_training`.
The coding cases are `SYN-CAB-20260930-01` through
`SYN-CAB-20260930-12`; cases 10–12 are fertility/preconception visits.
These are different from the older `SYN-RC-*` examples and the four CardChase tasks.
[Seed setup and expected result](MCD_TRAINING_SEED.md).

For combined coding and follow-up work, the administrator assigns **both
`Coder` and `BillingStaff`**, in both Entra app roles and active CAB practice
membership. `Coder` alone can view Card chase but cannot change cases, use
Athena review or authorize texts. `BillingStaff` alone has the follow-up
workspace; `Admin` is for the designated administrator.

## Review a coding case

1. In **Provider**, select one of the twelve case IDs. Read the clinical note,
   visit details and any documentation warnings.
2. Choose **Generate billing header**. If a saved header already appears, review
   it. Read each suggestion with its source quotation; a held or unresolved
   result is not an accepted code.
3. Choose **Edit header** to make a justified correction or code proposal. Select
   supporting source evidence for code proposals, confirm any proposed primary
   and diagnosis order, enter **Reason for edit (required)**, then choose
   **Save header edits**. Saving edits does not approve their codes.
4. Open **Coder review** for the same case. Explicitly review each suggestion,
   any human proposals and earlier suggestions shown. Supply the requested
   decisions/reasons, check **I reviewed this header’s facts, code proposals,
   evidence, and documentation warnings**, then choose
   **Record coder review and header**.
5. Check **Coder review recorded** and the reviewed revision number. Use
   **Copy header with evidence** or **Download header text** on the saved header.
   Reopen the case to confirm that the same edits and review receipt remain.

Unsaved edits cannot be exported. Editing a reviewed header requires a fresh
coder review; its old receipt still describes the earlier revision. If saving
is interrupted, use **Retry same request** or **Retry exact saved review**, as
shown. A timed-out save or generation has an unknown outcome; stay in the tab
and retry the same request rather than starting a replacement. Other cases and
tabs remain blocked while that outcome is unresolved. A failed header load offers
**Reload header** and can be left. Before leaving unsaved edits, either save them
or explicitly confirm their discard. For a confirmed conflict, use the offered
reload action. If access or a feature is unavailable, contact the trainer instead
of creating a replacement.

If **New coding generation awaits approval** appears for a registered provider
or Athena note, the source and any saved header remain available for review and
editing. New generation and a fresh coder signoff await that source's model
permission. The note's presence alone does not grant it.

## Practice CardChase

Before any texting trial, an **Admin** opens **Card chase → Demo setup →
Load synthetic demo tasks** while outreach is off and transport is
`record_only`. This loads a separate fixed **four-case** follow-up pack;
loading the twelve coding notes does not populate this queue.

Select a case and use **Summary** for contact details and the available next
action. **Activity** shows attempt totals by type, the last attempt and contact
history. Counts include recorded contacts and each platform dispatch start once,
including failed or unknown attempts; unsent drafts are excluded. The latest 50
entries are shown while totals cover the full history. Manual actions record
work; they do not send a text. For the first exercise, record only the trainer's
assigned fictional action and confirm it in Activity.

For an authorized texting test, **Communications** shows current delivery status
and any **Delivery problem** with an explanation. Accepted or sent does not prove
delivery. Follow the displayed action; STOP and unknown outcomes require review,
not repeated sending. Report a problem to the trainer with its code and the
synthetic case ID, without including message content or phone numbers.

| Feature | What to expect during training |
|---|---|
| Texting | **Communications** is shown when configured for authorized staff. Prepare and authorize SMS only in a trainer-arranged test using consenting testers. An authorization is not delivery; check the displayed status and reply. STOP remains suppressed. Calls are outside this rollout. |
| Athena | **Athena review** requires a configured connection and an explicitly matched case. Review patient/contact/insurance details before selecting fields to apply. These selections do not grant messaging consent. Note discovery/import is implemented, but the available source must contain a suitable completed training note; an open note must not be presented as signed or finished. |
| Cosmas MD button | The Core provider-page connection is implemented separately and must be configured before use. It is not required for this standalone CAB training session. Use only a trainer-approved synthetic note when it becomes available. |

A hidden or unavailable vendor feature is not a missing patient record. The
application supports these workflows; the trainer will identify which are
connected for this session. Neither the twelve-case pack nor local test results
prove a live vendor connection.

## Finish the exercise

Confirm that the original note is unchanged, the saved header matches its
export, the receipt names the reviewed revision, and the assigned CardChase
action appears in Activity. Report confusing wording, missing evidence or an
unexpected result with the synthetic case ID and step. There is no target
billing level or expected revenue result.

Administrators: use the [current deployment guide](../../infra/BETA_DEPLOYMENT.md)
for setup and the [operations guide](PILOT_OPERATIONS_RUNBOOK.md) for access and
recovery. This quickstart describes the implemented workflow, not a claim that
the hosted environment has already been updated.
