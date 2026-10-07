# Communications delivery error visibility

Risk: Tier 2 — patient outreach evidence and an additive data migration.

Outcome: an undelivered or rejected message can show a bounded provider code
and a controlled staff explanation, including the carrier-registration hold.
Signed callbacks already parse the code and include it in their identity hash,
but discarded it on insert; submission receipts retained it without projecting
it to staff. The new diagnostics contain no vendor messages, URLs, contact
fields, credentials or arbitrary diagnostic text.

## Expand and release

1. Independently review the final pushed source and rehearse the canonical
   `001`–`026` to `027` migration, checksum ledger and no-op replay in a disposable
   synthetic database. No hosted migration or configuration is authorized here.
2. Apply `027_communications_delivery_errors` through the locked owner runner
   before updating API, callback and worker applications. The nullable column
   leaves old callback bytes untouched. API/callback readiness checks require it.
3. Release the reviewed application image through the existing deployment
   authorization process. Mixed older applications can omit the new information;
   the UI accepts an absent code and does not invent a historical cause.
4. Verify one authorized synthetic callback's persisted code, staff display,
   and transition to a higher-precedence successful status before relying on
   the feature operationally. Local fixtures are not live delivery proof.

## Boundaries and recovery

- Provider status precedence and equal-precedence arrival ordering stay intact.
  The latest known non-null code for the effective status survives a truncated
  visible history; a higher-precedence success clears the operation's error.
  Historical callback rows keep their original codes.
- Existing exact callback hashes/replay identities do not change. Replaying an
  old event never backfills or mutates it. Forced practice RLS and append-only
  guards remain; callback insertion and its audit commit atomically.
- Pending work does not become failed, and unknown sends stay unknown. No
  automatic resend, permission, cancellation, consent or sending gate changes.
- Recover by reverting application code and redeploying the prior reviewed
  image. Retain `027` and all evidence; its down migration refuses deletion.

## Verification

Focused adapter, restricted PostgreSQL callback/receipt, status-reconciliation,
legacy replay, audit failure, RLS, immutability, code constraint and migration
checks; frontend contract checks and the actual built-app SMS browser flow.
Guardrail fuzz and the 4,000 supplied-fact invariants remain required. Use cached
dependencies and synthetic transports only; no vendor, credential or cloud work.
Preserve initial failures and their repairs in the local evidence. Independent
agent review must bind the final substantive pushed commit; it is not GitHub
approval or deployment authorization.
