# Shared SMS preparation store integration

Tier 2: patient-outreach storage, source binding, consent and audit boundaries.

## Outcome

Stack the dormant shared PostgreSQL store on the reviewed SMS schema outcome
at `8b152965777871703ea771a899c729ceec3fc362`. Port these three complete files byte-for-byte from
preserved `8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`: `postgres.py`,
`event_review.py`, and `test_chase_communications_postgres.py`.

The store references a current verified contact snapshot, reserves an immutable
staff-attributed preparation, persists inbound evidence and STOP suppression,
and supports exact retries across replicas. Preparation never authorizes,
queues, sends, creates a contact Attempt, or changes a clinical source. Its
eligibility objects are internal server inputs, not browser authority.

`postgres._event` lazily imports the final `event_review.review_ref`; preserve
that dependency and its circular-import order. Actual inbound/history checks
exercise it. The rest of the final event-review service remains dormant until
the dispatcher outcome supplies its host and complete acceptance tests. This
PR makes no staff-review, callback, worker or end-to-end texting claim.

## Dependency and rollout boundary

The preceding expand outcome supplies the unchanged 010/012/015/016/017/018
migrations and their existing foundation dependencies. The final store's
purpose-consent branch queries the later consent/provider-event/unknown-send
tables, so it must not be mounted against a 010-only database. Existing deployed
schema remains untouched; no migration runs against staging in this task.

No API, settings, dispatcher, worker, callback server, frontend, environment or
transport entry point is mounted or changed. SMS remains the default permitted
channel. Historical voice data compatibility is retained by the source port;
this does not enable calls. The separate runtime must later derive eligibility
from current consent, practice/case/direct-contract policy, cadence, verified
contact and approved test-recipient gates before any dispatch authorization.

Do not copy `config.py` early: it imports DispatchPolicy, recipient validation
and Settings parsers belonging to that later outcome. The complete event-review,
cancellation, unknown-resolution and paused-draft integration tests import the
dispatcher/runtime at collection time. Preserve them in the original checkout
and port them with their real dependencies; do not replace those checks with
fake dispatch success or remove safeguards to satisfy this slice.

## Local verification and evidence

Use a separate disposable database on the schema outcome's owned local PG
instance. Apply the candidate's actual discovered migration chain through the
locked runner before testing; record its checksums. Preserve all other databases
and previews. Retain the complete original PostgreSQL store test module.

Add only focused real-PG checks for purpose-consent-enabled preparation:
strict SMS attestation, unknown/not-required contact consent with explicit
purpose evidence, latest revocation, denied-route refusal, STOP across account
and sender rotation, unknown-start refusal with exact prior receipt recovery,
and atomic real append-only access-audit/preparation rollback on denied INSERT.
The owner only arranges synthetic historical ledger fixtures through normal
constraints. Store operations use an actual non-superuser, non-BYPASSRLS login
with the existing scoped privileges; no trigger/RLS bypass or vendor request.

Run the two scoped PG modules, repository guardrail fuzz tests and the 4,000-case
no-upcode proof. Preserve failure output and corrections. Compare all three
ported file hashes with preserved source, inspect the final base diff, freeze
source/verification manifests, then push one stacked draft PR for independent
post-push review. Keep the complete outcome below 2,000 changed lines/50 files.

## Recovery and remaining work

Omit/revert this dormant application-code outcome in a later reviewed build.
Leave the preceding additive schema in place; it is not a destructive rollback
target. Future dispatcher/runtime integration must still prove signed callbacks,
consent/policy authorization, recipient scoping, cancellation, unknown-send
resolution, unassigned review and worker isolation before any texting activation.

## Observed result

The candidate's 17 migrations (001–013, 015–018) applied successfully through
the locked runner to a fresh separate loopback database on PostgreSQL 16.15 (Debian 16.15-1.pgdg12+2)
with pgvector 0.8.6. The ledger exactly matches the candidate file hashes.
All 79 focused real-PG checks passed on the first run, including complete
original store coverage plus the new purpose/audit supplement. Ten guardrail
checks and 4,000 invariant cases passed. No external request or model call ran.

Test grants belong only to fresh disposable restricted logins. The actual API,
worker and importer provisioners remain unchanged and have no new communications
authority. No image/platform/promotion or complete dispatcher acceptance is
claimed by this local storage proof. The local PG instance is retained for the
same-conversation independent reviewer; preserve the schema agent's database.
