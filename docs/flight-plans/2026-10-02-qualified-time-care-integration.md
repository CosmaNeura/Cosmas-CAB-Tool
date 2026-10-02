# Qualified total-time and encounter-work foundation

Risk: Tier 2 (billing evidence). Base: `d2dc9a729a0de3a7ab3fa666f05fcb5a6d8ec25c`.

## Outcome

Add exact final `em_time.py` and `encounter_care.py` from
`8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`, following C5.
These qualifiers remain dormant until C6 consumes them through the existing
full pipeline. No code level, API, UI, schema or activation setting changes.

## Boundaries

Time requires exact clinician service-date total evidence, excluding ineligible
components and conflicting totals. The qualifier does not sum component times,
round a range or use appointment/staff time. Current encounter work requires a
complete source-bound assessment and qualified order group with matching speaker,
raw/source fingerprints and retained evidence. It cannot supply a diagnosis,
problem/data/risk category or E/M level. Individual order ownership remains held
when ambiguous, including requests and competing current assessments.
Original training notes and saved header/receipt state remain unchanged.

## Tests and deferred integration

Keep direct encounter-care and order-ownership definitions unchanged. Historical
time tests mostly invoke the future pipeline: retain all those original names,
AST fingerprints and inputs for C6, while adding direct qualifier counterparts
using the exact existing parameter vectors. No full-pipeline assertion is replaced
or committed as skipped. Corpus replay remains for C6/final acceptance.
Assert candidate imports; run focused leaf checks and required guardrail/invariant
proof once with existing dependencies. No database, browser, model or vendor calls.
Freeze source, selected/deferred test and verification manifests; obtain independent
review after the final substantive push. Stay below 2,000 lines and 50 files.

## Recovery and limits

Revert this dormant module/test outcome before activation; no state needs rollback.
Bounded source qualification and synthetic checks do not certify clinical accuracy,
claim validity or deployment readiness. C6 must enforce retained current-care
necessity and the final evidence-qualified time/MDM ceiling together.

## Independent-review repair

C5-01 retains patient/report/template source ownership across blank paragraphs
and neutral headings. Only an explicit current clinician label resets that owner;
ordinary multiline notes and exact source/time fingerprints remain supported.
The time proof has no public rule-version field, so its shape stays unchanged.
Encounter-care and order ownership bytes are untouched. Keep the initial evidence,
add direct source-heading/reset controls, and freeze the focused repair separately
for independent review; pipeline replay remains assigned to C6.
