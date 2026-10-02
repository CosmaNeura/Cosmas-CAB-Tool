# Athena staff review and local billing-import core

Tier 2. Base: 7fbd8607994f946a43bb94c00775301c83ca19fb.
Stack: codex/mcd-athena-review-core above codex/mcd-athena-schema.
Preserved source: 8d0caca22aaed8001aaeec955c81c3f7ecc11bd4.

## Outcome and boundaries

Port the exact final staff review service, memory/PostgreSQL store, unmounted
route definitions, configured-workflow core, binding reader and private owner
registration CLI. Include the required source_visit_context helper used during
billing import; source metadata remains outside note and header hashes.

Staff must explicitly confirm the existing case's patient, select contact and
policy observations, and recheck source before application. Contact consent
remains unknown; neither eligibility nor outreach is authorized. Billing import
requires explicit finished-note and clinical-context confirmation, preserves
exact source hashes, and commits source/receipt/audit atomically. No EHR write.

The c67c742 mapper repair and canonical schema parent remain unchanged.
API mounting, settings, deployment grants, configured/API acceptance and UI
integration remain a subsequent slice. Preserve their complete historical
test suites and fixture unchanged for that integration; no committed skips
or partial substitute tests. No public source registration or runtime activation.

## Verification and recovery

Run the complete original review, binding and PostgreSQL suites against candidate
imports. Adapt only the original PostgreSQL fixture's Unix-socket assertion to
the explicit dedicated loopback test database. Before tests, use the actual
checksum-locked runner to apply all 17 available sparse migrations; verify exact
ledger checksums and replay. Keep all source/fixtures and original assertions.

The two-replica proof uses actual restricted logins and fake Athena responses:
selected evidence, unknown consent, unchanged case state, note/audit atomicity,
source tamper rejection, exact retry, practice isolation and membership revocation.
Run required guardrails once. Deny external calls; use only a new database on the
verified owned container, and retain it without affecting other consumers.

Publish a draft below 2000 lines/50 files and obtain independent final-commit review.
Recovery leaves existing schema and evidence intact and keeps these modules
unmounted. No hosted configuration, vendor/model call or source corpus change.
