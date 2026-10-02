# Editable billing header

The authenticated CAB API can generate a review header over an authorized stored
encounter and save Provider/Coder edits separately from its clinical note.
Migration `011_billing_headers` is an additive prerequisite. The deployment
control `COSMANEURA_BILLING_HEADER_ENABLED` defaults to false. Local tests explicitly enable the feature using memory storage; deployed
applications use PostgreSQL with practice isolation, current membership checks
and atomic access-audit completion. Disabling it leaves the immutable history in place.

The normal provider workspace shows the button and header only when encounter
detail includes the server-owned `headerSource` capability/snapshot. A Coder can
use that same workspace. No final provider-platform URL or separate dashboard
hosting is assumed by these contracts.

- `POST /api/encounters/header`: encounter ID and expected source snapshot;
  returns the latest header/revision or `view: null`.
- `POST /api/encounters/header/generate`: adds an operation UUID. Generation
  traverses the full billing pipeline. A retry or existing header returns the
  current head without overwriting edits or rerunning inference.
- `POST /api/encounters/header/save`: adds header ID, expected revision, operation
  UUID, draft facts/code proposals/reviewer note and a required reason. Actor and
  timestamp come from the server. Saving never accepts a code.

The source snapshot includes encounter version, note/supporting-source hashes,
bundle hash and ingestion provenance. Stale source/revision conflicts return HTTP 409;
reused operation IDs with changed intent are rejected. Evidence uses Unicode
code points. Human facts do not establish documentation or clear completeness
flags. Engine suggestions remain unchanged; human code edits and removals are
separate unreviewed proposals with before/after history.

The header remains unreviewed after existing coder feedback. A future signoff
slice must compose the current recommendation digest with the reviewed header
ID/revision and validate both atomically. This change does not claim that
existing coder signoff approves header edits. Native provider-platform launch,
verified terminology manifests, and that combined signoff are follow-up work.

Focused checks cover provider/coder editing, source/revision conflicts, exact
request retries, source evidence and unchanged notes. The PostgreSQL API
rehearsal requires an explicitly configured disposable database and signing
key. Its result is recorded separately from in-memory checks. Live Athena,
messaging, hosted rollout, and parity with later clinical qualification repairs
are separate from this workspace integration.
