# CardChase queue and recorded activity integration

Risk: Tier 2 (case-scoped contact metadata). October 2, 2026.

## Outcome and authority

The owner requested a focused command-and-control dashboard, easy case contact
review, and last/number/type of contact attempts. This source-only integration
adds a searchable queue and a selected-case Summary/Activity workspace. It
reuses existing recorded attempts and existing task authorization; it adds no
contact action or transport. Existing manual activity recording is preserved.
Current texting-only intent does not erase previously recorded phone history.

Base: `a21310b8a30bfa046915de4709fa931de438000f`.
Branch: `codex/mcd-card-chase-workspace`.
Preserved reference: `8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`.
Ports: `024dede` activity contract and `01edf57` queue/workspace layout only.
No historical local-demo host, OCR, Athena, communications components, settings,
workflow, schema, auth, clinical/header, or source-pack changes are included.

## Contract and integration

The existing audited POST /api/chase/task adds activity v1: all recorded-attempt
totals, counts by phone/email/SMS/portal, staff minutes, last attempt, and up to
50 recent entries. Each entry exposes only validated time/channel/performer
category/known outcome/minutes. No raw actor ID, contact destination, note or
template appears in this projection; worklist rows do not acquire the history.

Pending tasks and transport events are not attempts. Accepted/sent are not
delivery or patient response. Invalid/missing history shows unavailable, not a
false empty history. Older servers remain readable. All case/practice/role
boundaries and exact manual-action retries retain their existing contracts.

The UI moves next action and source details into a selected-case Summary, with
Activity behind an accessible tab. Search filters only the already-authorized
loaded queue. No patient-record deep link, consent inference, source-system
write, automatic contact, SMS activation or model request is introduced.

## Dependency decomposition

This is an independently deployable presentation foundation. OCR worker/model
packaging and reviewed-card intake are separate optional backup capabilities;
they are not prerequisites for SMS. Dormant SMS protocol/store, transactional
dispatch/runtime, explicit event review and inbox follow in dependency order,
with later safety repairs folded into each relevant port. Do not import the
final CardChase file wholesale before those dependencies exist.

## Verification and recovery

Use cached dependencies only. Run focused deterministic activity and task API
checks, existing role-boundary check, frontend activity contract checks and one
production build. Validate one synthetic local interaction for queue search,
case selection, accessible tabs, activity refresh and case-switch isolation.
No new service calls, model runs, database/schema campaign or broad matrix.

Record exact source hashes, selected commits/hunks, results and remaining limits
under outputs/mcd-training-2026-10-02/card-chase-workspace-integration.
Keep the outcome below 2,000 changed lines / 50 files. Root arranges independent
review of the final commit before publication. No push or PR in this task.

Recovery is a source revert of this additive task projection and UI; existing
attempt data is unchanged. No down migration or deployed activation is involved.

## Observed local evidence

Initial focused Python run:95 passed; one stale static seed-render assertion
was corrected without weakening its server capability guard. Two affected
checks then passed, covering that correction and exact manual-action replay
activity. Six frontend activity checks passed; final isolated build passed.
The frontend now rejects an unknown timezone offset consistently with Python.

One completed synthetic browser flow covered fixed seed, search, keyboard tabs,
manual-record activity refresh, delayed prior-case response isolation and
desktop/tablet containment. Output-only harness setup failures are preserved;
they required no product-source repair. No pipeline, transport or external
request ran. The task-only host and temporary cache symlink were removed.
The existing clinical engine, headers, source pack and workflow files are
unchanged; their earlier validation is retained rather than rerun.
