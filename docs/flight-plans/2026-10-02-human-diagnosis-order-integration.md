# Current-code human diagnosis decisions and confirmed order

Risk: Tier 2 (billing decisions and durable human review).
Base: `cdfdd9bf34468fda810421604e66ef2223511fba` (C1/C2 repairs and dormant C3).
Source: selected final `8d0caca` behavior from `106d160` and `00b74ca`.

## One outcome

Require affirmative current catalog billability for every new non-reject human
ICD decision and every ICD code retained in a new draft. Save explicitly confirmed
human primary/order through revisions, coder acceptance, reload and export.
Array position never confirms a sequence. An accepted sequence requires eligible
accepted code decisions; rejecting its primary cannot promote a secondary.

Preserve original generated codes, primary intent, holds, evidence and source
hashes as immutable inference history. Confirmation resets in the editor when
ICD membership, order, code or evidence changes. A new sequence is attributed to
the authenticated actor and revision; signer/source/CAS checks remain intact.
Shape validation precedes exact receipt lookup, while current-code validation
precedes a new write. A historical exact replay remains readable after catalog
changes and cannot become authority for a new invalid decision.

## Bounded integration

Reuse header v1, existing JSON revisions and the reserved order decision; no new
migration, catalog mapping UI, MDM wiring, model service or external source path.
Preserve both parent header refresh/retry fixes. Do not change the source-only
12-note pack or its three fertility visits. Keep all workflow bytes unchanged.

## Verification and recovery

Run candidate-origin-focused human validity/replay/order/API tests, related
frontend contracts, and one isolated build with cached dependencies. Exercise
actual save/reload/signoff/replay/audit behavior on one disposable local sparse
001–009+011+013 PostgreSQL schema; do not repeat the foundation matrix. No live
model/vendor calls, credentials, hosted changes or data activation.
Freeze exact files, observed checks and limitations for independent review.
Keep <=2,000 changed lines /50 files including review; split before growing.

Disable this workflow before rollback; retain append-only revisions/receipts.
An older reader must not be used to confirm the new diagnosis-order contract.
Catalog presence does not establish clinical applicability. The coder remains
responsible for an evidence-supported human decision; no EHR/claim write occurs.

## Observed local checks

190 focused Python checks, 51 Node contracts and one isolated cached frontend
build passed. One owned PG16/pgvector rehearsal of sparse 001–009+011+013
passed save/reload/signoff, actor/audit, exact catalog-change replay, new-invalid
write rollback and source-bound export across restricted runtime replicas. The
container was removed. These checks used parent `409c843`; the parent advance
retained all 19 C8 file hashes. No full MDM parity or new live inference is claimed.
