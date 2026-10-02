# Header signoff and baseline reconciliation — 2026-10-02

Risk: **Tier 2** (coder authorization, patient-data history and source binding).
Base: `f263e74881682dec52ecef3c7fc3660a7ab55fba`.
Historical outcome: final `7c2bd6e` state of `b573acc` signoff plus its baseline
reconciliation correction, relative to `fe5fd3f`.

## One outcome

Record explicit coder decisions against the exact current header revision and
source, with manual copy/download of that saved review. Original suggestions
absent from a later pipeline run require separate retain/reject decisions;
retention adds no code by itself. Human edits remain distinct from engine output.
A saved edit invalidates current signoff but preserves historical evidence.

Port only the listed domain/store/routes/UI/tests and canonical `013` SQL.
Extract authenticated feedback validation without changing the current v3
signer/key behavior. Add only signoff audit classification and conditional
runtime SELECT/INSERT grants. Preserve the current header readiness/held-E/M
repairs, CR-02 suppression, training pack and all unrelated routes/settings.
No local-demo/Playwright harness or clinical-adjudication changes are included.

## Expand and validation

`013` requires `003` and `011`; the actual sparse schema is `001`–`009` plus
`011` and `013`. Preserve canonical SQL bytes and rehearse these exact files.
Use scoped domain/API/helper checks, current signer regressions, and one owned
local PostgreSQL instance for source/revision conflicts, exact replay, explicit
baseline decisions and atomic feedback/receipt/audit persistence. Exercise an
actual restricted runtime login; stop and remove the owned instance afterward.
All cases are synthetic and external/model calls remain absent.

This slice supplies review mechanics, not clinical parity. Clinical repairs
remain required before activating MCD coding. No hosted migration, activation,
claim submission, clinical-note modification or EHR write is performed.

## Recovery and review

Keep `billing_header_enabled` off until an approved rollout. Recover behavior by
turning it off while retaining the expand tables and immutable history. The down
migration rejects populated review history; it is not immediate rollback.

Freeze exact hashes and local evidence before parent commit/push. A separate
agent reviews the final substantive push. Remain below 2,000 changed lines and
50 files; split the outcome if its actual size exceeds those limits.
