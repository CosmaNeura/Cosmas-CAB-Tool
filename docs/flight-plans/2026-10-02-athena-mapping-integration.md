# Athena contact and insurance review candidates

Tier 2. Base: 89a31183e36735b608c9391ca896cc0bd1ac7c52.
Stack: codex/mcd-athena-mapping above codex/mcd-athena-protocol.
Preserved source: 8d0caca22aaed8001aaeec955c81c3f7ecc11bd4.

## Outcome and boundaries

Port the final athena_card_chase.py and its complete existing test module,
then repair explicit scope/context validation (ATHENA-MAPPING-R1). The pure
mapper produces immutable contact/insurance
review candidates from supplied, bounded Athena response bytes. Each observed
field retains its exact source pointer, type and response digest.

Absent, null, empty, invalid, partial and unavailable observations remain
distinct. Numeric IDs preserve original values alongside bounded normalization;
member/group leading zeros are retained. National phone syntax requires country
confirmation. Conflicting policy/member/subscriber observations stay unresolved.
A source eligibility snapshot is never fresh CAB verification.

Candidates never select a patient, contact route or policy, grant consent,
authorize outreach, establish current coverage, mutate a case, import a note,
or write an EHR. Preview and operational source classifications stay separate;
a scope is metadata, not read/storage/model authority. Every supplied scope
must be valid and match the context's server practice, vendor practice and
source patient before optional reads, including unavailable responses. Matching
Preview and Production retain their existing classification rules; legacy
unscoped Preview stays compatible. This localized repair intentionally differs
from the preserved mapper bytes and changes no valid candidate shape.

The reviewed protocol parent supplies the decoder and immutable scope contract.
This slice adds no route, API/settings change, schema, credential reader, network
request, source fixture mutation or application activation. Human-reviewed
receipts, staff UI and later billing imports remain subsequent integrations.

## Verification and recovery

Run every original mapper test, the new contradictory/valid scope regressions,
and the existing immutable-profile contract tests
against this candidate with network/process operations denied. Assert import
origins, unchanged parent dependencies and preservation of every original test;
retain commands, logs, XML, result, source
hashes and any failure under the local athena-mapping-integration output folder.
Preserve the initial independent review and evidence; store repair results in
repair-01. No dependency install, model, vendor, database, browser or deployment run.

Keep this outcome below 2000 changed lines/50 files. Create a draft stacked PR;
independent review must bind the final substantive push before merge. Recovery
is omitting or reverting the dormant mapper; no schema/data rollback is needed.
