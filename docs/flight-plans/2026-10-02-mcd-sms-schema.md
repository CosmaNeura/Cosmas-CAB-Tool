# SMS canonical schema integration

- Tier: 2 — patient outreach state, authorization evidence and tenant boundaries.
- Base: `efd0037d2859831ce353e6459868f06ec4336ea9` (`codex/mcd-sms-foundation`).
- Outcome: install the complete dormant communications schema before a separate
  PostgreSQL store integration. No API, dispatcher, worker or hosted activation.
- Source: exact final migration bytes from `8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`.

## Expand sequence and closure

The existing baseline is 001–009, 011 and 013. Add canonical up/down pairs
010, 012, 015, 016, 017 and 018 without renumbering. The resulting available
set is 001–013 and 015–018. Missing 014 is Athena-specific and not a dependency.
010 uses the practice/membership/RLS and contact structures in 003/004; 012
uses 010; cancellation/unknown-outcome protection 015/016 use 010/012; review
017 and unassigned SMS 018 close the same storage dependency graph.

The historical SQL vocabulary retains voice records for compatibility.
Application defaults remain SMS-only; this change adds no calling controls.
The existing role provisioner deliberately gives API, worker and importer
roles no communications table/view/function grants. A later consumed store
slice must introduce and verify only its required authority.

## Verification and evidence

Use the checked-in locked migration runner and a cached PostgreSQL 16 +
pgvector image, bound only to loopback with synthetic local identities.
Rehearse baseline → expand, exact checksum ledger and no-op replay. Preserve
all twelve fixed MCD source notes across the expansion. Verify forced RLS,
always-enabled append-only and evidence guards, security-invoker review
views, denied unprovisioned runtime/worker/importer access, and fail-closed
checksum drift. Exercise recovery refusal with synthetic retained evidence.
Run only focused migration/schema checks; no inference or vendor calls.

Record exact canonical and candidate hashes, commands, observed assertions,
and remaining limits under the task's output directory. Retain the owned
container for the same-conversation store agent's separate database; hand
off its label/name/loopback port explicitly, then remove only after that
agent/root releases it. No hosted migration or deployment is performed.

## Recovery and acceptance boundary

Ordinary recovery leaves the additive structures dormant and reverts the
consumer application if needed. Never run down migrations to erase activity.
The canonical down scripts refuse retained evidence and exist only for an
explicitly selected empty disposable database or separately reviewed recovery.

This outcome proves schema closure and provisioning boundaries, not an SMS
workflow, vendor readiness or operational beta activation. Commit as a small
stacked draft PR against the SMS foundation. Independent agent review follows
the final substantive push; record its exact commit and disposition.

## Observed local result

All 13 distinct focused checks passed across the initial run and a four-case
test-only correction delta. Initial 12/13: PostgreSQL rejected a truncate on
its foreign key before the expected append-only trigger. The corrected check
uses CASCADE inside a rollback transaction and confirms the canonical trigger
rejects it; no SQL changed. The delta also verifies header SELECT and INSERT
individually. Both runs and their separate synthetic databases are retained.
All twelve source rows survived expansion and exact seed replay unchanged.
