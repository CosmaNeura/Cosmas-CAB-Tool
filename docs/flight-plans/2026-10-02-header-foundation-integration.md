# Dormant header foundation integration — 2026-10-02

Risk: **Tier 2** (patient-data schema, attributed history and authorization).
Base: `69fa65e844e2c24114dc2bd534d02fb29f6721e1`.
Historical implementation: `a5e323e08447371c3fc2c97f9ff69bc18d7b39ca`.

## Outcome and boundary

Add the source-bound billing-header domain and append-only revision store.
Retain exact historical bytes for both domain/store modules, their focused
checks and canonical migration `011`. Port only its conditional runtime grants,
migration-list expectation and the scoped migration documentation.

No API, UI, settings, workflow, communications or clinical-adjudication changes
are included. This is dormant storage for a later authenticated header workflow;
it does not establish clinical parity or a complete staff launch.

## Expand and verification

The actual checked-in migration set is `001`–`009` plus `011`. Migration `011`
does not require `010`; do not renumber or alter already-reviewed SQL bytes.
Verify this sparse set and the isolated domain/store checks, then apply the
available schema to a disposable local PostgreSQL database. Exercise actual
restricted-login create/save, source and membership checks, concurrent revision
conflicts, exact replay, immutable history, forced RLS and mandatory same-
transaction audit receipts. Record the applied filenames and any unavailable
extension explicitly; do not equate a partial schema check with full deployment.
Use synthetic fixtures only; no hosted, vendor or model calls.

## Recovery and release

Do not activate or deploy this branch. If rolled back after a future approved
expand, leave the new tables dormant and retain their history; do not drop them
as immediate recovery. The canonical down file is preserved for deliberate
schema maintenance, not routine rollback.

The parent integrates the final source hashes and local verification evidence,
then commits and obtains independent review after the final substantive push.
The implementer does not supply that independent approval. Keep the whole
change below 2,000 changed lines and 50 files.
