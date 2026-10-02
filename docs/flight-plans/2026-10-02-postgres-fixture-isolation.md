# PostgreSQL fixture isolation — 2026-10-02

Tier 0: test-only repair; no runtime, migration SQL, Actions workflow or deployment change.

The existing PR68 hosted run used password authentication, while SMS fixtures
created password-less roles and retained the owner's DSN password. Other failures
came from a shared append-only table truncate, an incomplete SMS fixture schema,
and a migration004 down/up test on a schema containing later dependents.

Use generated per-role passwords, fresh practice keys, the complete canonical SMS
fixture dependency set, and a new owned database for the historical004 round-trip.
Preserve all existing behavioral assertions and append-only/RLS guards. The latter
test removes only the unique database it created; no shared schema cleanup occurs.

Validate the original failure subset under local SCRAM, then the affected adjacent
suites and existing anti-inflation guards. Retain original failures and exact
candidate import/source hashes. No vendor calls, real data or hosted execution.
Recovery is reverting this test-only commit; there is no data rollback or activation.
