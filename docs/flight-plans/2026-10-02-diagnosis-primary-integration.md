# Source-qualified diagnosis and primary-intent integration

Risk: Tier 2 (billing evidence and adjudication). Base: `b4570b4074cb72853e8dd8415bfd76b1abd0e92e` (includes both parent header-retry repairs).

## Outcome

Port the repaired diagnosis meaning/current-billability gate and explicit primary
intent together from reviewed source `8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`.
An unresolved or held explicit primary must not silently grant a secondary
primary status or procedure-coverage authority. Valid secondaries remain visible.
This changes deterministic diagnosis handling through the existing full pipeline;
it does not activate MCD coding or claim clinical parity for the older E/M rules.

## Scope and preservation

Use final diagnosis terminology v2 and the shared primary resolver. Merge only
selected API/header/frontend hunks onto the authenticated mechanical workflow.
Preserve server-only v3 review signing, held/rejected selection restrictions,
header source hashes, edit/CAS/signoff receipts and all workflow files.
Current manual-code validity/order changes and full MDM rules remain separate
planned outcomes; no manual human result is silently changed by this slice.
Keep the packaged 12 source notes and three fertility visits byte-for-byte.
No source bridge, SMS, OCR, Athena, model-service policy or external call changes.

## Verification

Run focused meaning, negation/uncertainty/attribute, current-code capture, primary
intent and coverage tests plus impacted API/header/provider/coder contracts.
Verify candidate Python import origin. Run the affected Node contracts and one
isolated frontend build using existing dependencies. No database rerun or new
model call. Only duplicate fixture-backed 12-note replay functions are deferred;
all standalone checks remain. The final clinical stack will use the original
24 cached extraction objects in one deterministic replay without a baseline rerun.
Record final source/evidence hashes and independent agent review before promotion.
Recalculate the actual diff against the final parent: <=2,000 changed lines and
<=50 files, including this plan and review. Split rather than dropping checks.

## Recovery and limits

Revert this code outcome before activation if integration fails. No schema or
persisted source changes require rollback. Previously saved headers/receipts stay
readable; fresh inference/signoff uses a fresh source-bound review context.
Catalog membership does not establish clinical applicability. Unresolved concepts
and primary intent require human review; captured codes do not certify a claim.
Existing deployment/inference controls remain unchanged.
