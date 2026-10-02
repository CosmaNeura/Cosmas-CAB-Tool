# Current-revision billing header review

Tier 2, local synthetic beta. This additive slice joins coder feedback and the
editable billing header. It does not change engine adjudication, the clinical
note, claim transmission, EHR writes, or the existing PHI admission gates.
`billing_header_enabled` remains the release control. The PostgreSQL expand
structure remains dormant when that control is off.

## Review contract

`POST /api/encounters/header/signoff` accepts `encounterId`, `headerId`, the exact
`expectedSource`, `expectedRevision`, `feedback` (the existing complete signed
recommendation manifest), `acknowledged: true`, and `humanProposalDecisions`.
Every changed, added, or removed header code needs exactly one `{id, decision,
reason}` with decision `accept` or `reject`. A nonblank reason is required.
Accepted code proposals must agree with final coder feedback; rejected proposed
additions cannot remain in it, and rejected removals must retain the original.
This records a human decision, not pipeline validation of a human-added code.

The API validates the existing secret-signed v3 review context before calling
the header store. The store independently enforces current source/version,
practice membership, Coder/Admin role, exact header revision, and full manifest
digest. Provider users may edit headers but cannot record a coder receipt.
Server actor and time are authoritative. Client actor fields are not accepted.

The original generated header and attributed edit history stay immutable and
`unreviewed`; the separate receipt identifies the reviewed revision and source,
recommendation digest, coder decisions, proposal dispositions, reviewer, and
time. One receipt may win for a given header revision. An exact repeated review
ID returns its same receipt only while the source and revision are still current.
A changed body, a second review ID for that revision, or stale source/revision
returns a conflict. Existing feedback-only records cannot be promoted silently.

Editing a reviewed header preserves its historical receipt but removes current
signoff. A fresh review requires new explicit coder decisions and confirmation.
Changing the source prevents the old header from being used at all. The browser
loads detail and coder view at matching encounter versions; receipt/source
validation fails closed before displaying a current review.

## Atomic persistence and migration rung

Migration `013_billing_header_signoffs.up.sql` expands after 003 and 011;
communications migrations010/012 are independent prerequisites of their own
workflows and are included in the current full001–026 deployment chain. The
header signoff table grants the runtime role only
SELECT and INSERT. Forced practice RLS, active role checks, immutable rows,
current encounter/header locks, and unique review/revision constraints apply.

The store writes coder feedback, header receipt, and successful access completion
in one PostgreSQL transaction. Deferred constraints require the referenced
feedback and audit completion to be inserted in that same top-level transaction.
Pre-existing success evidence cannot authorize a new signoff. Savepoint-only
subtransaction inserts fail closed; no such write path is used. Rollback leaves
none of the three records. Concurrent review/edit transactions serialize on the
encounter; a receipt that loses currency is never returned as the current review.

Focused local tests inject one `MemoryHeaderFeedbackAdapter` shared by
header/feedback/preferences. Its lock and copied state commit atomically within
that process. It is transient and makes no crash-durability claim. The ordinary
JSONL feedback adapter is unchanged and is rejected for composed signoff;
there is no claimed cross-file transaction.

Recovery: turn off the header release control and leave the additive table
dormant. The down migration refuses to drop a table containing review evidence;
it is only for an empty disposable rehearsal. No production migration or
activation is implied by local rehearsal.

## Operator workflow and verification

Providers and coders can generate the full-pipeline header and save attributed
edits. The coder workspace shows that same saved revision, original evidence,
human proposal decisions, and a separate confirmation. A first save freezes the
complete feedback/header request. An ambiguous response leaves the controls
locked for an exact retry. Confirmed conflicts require a refreshed workspace;
they never display an old receipt as current. When the header feature is absent,
the existing feedback-only action remains available.

Copy/download exports saved header text with original source quotations,
coordinates, manual attribution, warning flags, and current review outcomes.
Unsaved edits cannot be exported. Nothing is copied into the clinical note.

Focused tests cover source/revision/actor/manifest mismatches, proposal semantics,
exact replay, stale receipts, detached memory views, transaction rollback,
concurrent reviews and edits, real PostgreSQL restricted roles/RLS, and SQL
bypass attempts. Historical browser coverage is retained on the feature branch;
its local demo/Playwright harness is not included in this integration. New
verification and independent review must bind the final integrated source.
