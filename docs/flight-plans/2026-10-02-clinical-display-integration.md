# Clinical evidence in billing review and saved headers

Risk: Tier 2 — billing evidence and attributed human review.
Base: `2ee9aa30774388828d46ac9b194bb44606ca5d6b` (qualified E/M engine and corrected risk/time).

## Outcome

Present the engine's source-qualified data, addressed problems and management
risk as counted, duplicate or held events. Preserve exact evidence locations,
original rule versions and explanatory quotes even when no E/M code is captured.
Require retained event-specific anchors for counted work; incomplete evidence
suppresses the aggregate display without inventing a replacement classification.

## Contract and boundaries

Keep optional original data/clinical ledgers immutable across human header edits,
reload, coder review and export. Validate both ledgers against original source
bytes on reload and export. Admit reviewed data rule v3 while retaining old v1/v2
records unchanged; admit risk v4 while retaining v1–v3. Carry the complete 320-reference envelope through both readers.
Human mappings, primary/order confirmation and exact unresolved request retries
remain intact. No provider host, model-input authority, source-admission callback,
CRLF conversion, schema or external-service settings are imported in this slice.
Original training notes remain unchanged; no PHI, live inference or outreach.

## Verification and recovery

Reuse exact synthetic presentation fixtures and selected source tests, add v3 and
data-only reload/export regression coverage, and run focused backend/frontend
checks plus one isolated build. C6 supplies the clinical pipeline and invariants;
final integrated acceptance reuses the original 12 notes/24 cached extractions.
Obtain separate non-implementer review after final substantive push. Remain below
2,000 changed lines and 50 files. Keep existing release controls unchanged.
Disable the header feature and restore the prior reviewed image for recovery;
retain all saved headers, receipts, revisions and audit history. No state is
converted or removed. This display is advisory and does not authorize claims.

Independent finding C7-R1: the data summary card now uses the validated ledger,
with complete/incomplete/invalid/absent actual-component regression coverage.
An unavailable ledger cannot fall back to a raw MDM classification.


Header time-fact correction: the old presentation regex missed the exact eligible
time retained by the pipeline. Populate this fact only from the completed E/M
view's explicit total and retained exact source span, requalified against the
stored note/transcript. Remove the unqualified regex path. Do not infer from
code/rationale, change visit duration, release held codes, or modify original
notes/engine results. Verify exact source, attribution, missing/stripped evidence
and the actual note-09 syntax with scoped adapter/API checks before review.
