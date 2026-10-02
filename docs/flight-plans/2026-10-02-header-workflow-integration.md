# Authenticated editable billing header integration

Tier 2. Integration base: `7e19a4f6b6aeb29440b8f2bd133c9cceed3fb624`.
The parent advanced the initial MCD-pack base onto the committed foundation.
Port source: `fe5fd3f64548f44b9578313e96089c1138b66c9f`.

## Outcome and boundary

Add Generate billing header, editable facts/code proposals, attributed save and
reload to the existing CAB provider workspace. The user requested a modifiable
first-review header. This slice uses authorized stored encounters, including
the fixed MCD synthetic pack; it does not choose or integrate another provider
project, enable inference/outreach, or change clinical adjudication. Preserve
the server-only v3 signer, held-code review protections and immutable sources.

The source-bound domain, store and canonical migration 011 are an explicit
prerequisite in the separately reviewed foundation slice. Do not copy them or
add fallback implementations here. The parent advanced this worktree
onto the committed foundation before execution. Header control stays false by
default; its absence adds no table dependency to ordinary API operation.

## Implementation contract

- Port only header-related API/settings/audit/rate-limit/workspace hunks plus
  routes, presentation runtime, UI/helper and focused tests. No historical
  local-demo auth, Playwright harness, CardChase, Athena or workflow changes.
- Read and generation bind the exact stored encounter/source snapshot. The
  full existing billing pipeline generates suggestions; held/removed codes
  remain withheld. Exact source quotes use Unicode code-point coordinates.
- Derive actor/practice from existing authenticated membership. Validate source,
  version and expected revision again inside the store transaction, together
  with append-only revision history and atomic access audit.
- Retry an ambiguous mutation with its original operation UUID/body. An existing
  generated header returns the current revision without replacing edits or
  rerunning inference. Definitive conflicts preserve visible unsaved inputs
  and require source/revision recovery before another mutation.
- Human edits remain unreviewed proposals with evidence references and a
  required reason. They cannot mutate the clinical note, clear documentation
  gaps, accept codes, or become a signed claim. Combined coder signoff and the
  later clinical qualification/parity repairs are separate integrations.
- Preserve existing signer initialization/validation. Durable test fixtures
  provide an explicit test signing key; missing-key checks stay fail closed.

## Verification and recovery

After foundation is committed and present, run the focused header API and
frontend contract checks using this candidate package, plus necessary signer
or held-code regression checks. Use the existing isolated build/fixture route
for a bounded UI proof if authorized; do not create a generic demo service.
No tests, PostgreSQL startup or model/vendor requests precede dependency
readiness. Record exact source/evidence hashes and independent final review.

Use the existing header release control to disable the UI/API if needed.
Retain canonical schema, source notes, operation receipts and revision history;
do not drop the expanded structure or delete edits as rollback. No hosted
configuration, migration execution or deployment is authorized by this source
integration. Keep this one outcome under 2,000 changed lines and 50 files.
