# Dormant Athena schema and required source guards

Tier 2. Base: c67c74243608c242ed4d547d42cca3b761a5de99.
Stack: codex/mcd-athena-schema above codex/mcd-athena-mapping.
Canonical source: 8d0caca22aaed8001aaeec955c81c3f7ecc11bd4.

## Outcome and dependencies

Port exact up/down pairs 014, 019, 020, 021, 022 and 023. Athena review,
mapping and read/storage admission structures remain dormant. The 019 canonical
hash function is required by 021/023. The 019 provider source guard and 020
admission wrapper protect the provider provenance retained in 023's allowlist.
No runtime grant, route, configuration, model, vendor or operator registration
is activated; Session Prep and provider-host wiring are outside this slice.

The actual set is 001–009, 011, 013, 014, 019–023. Existing migration bytes,
ledger records and application data must remain unchanged. New structures
expand the schema; permission to run local proof is not hosted promotion.

## Local verification

Use a new dedicated test database on the verified existing owned local
pgvector/PostgreSQL container, never another consumer's database. Apply the
baseline through the real checksum-locked runner, retain synthetic baseline
rows and ledger, then apply the six new canonical rungs and verify exact
checksums, no-op replay, drift rejection and unchanged baseline records.

Use actual restricted logins to prove practice/role isolation, owner-only
mapping registration even after accidental grants, immutable audit-coupled
review evidence, current mapping and admission bindings, expiry/revocation
and operational receipt integrity. Verify the existing API writer cannot
manufacture provider provenance and receives no new default grants.
Run focused migration/schema tests and required guardrail checks once.
Retain failures and final source/import/container/command evidence locally.

## Release and recovery

Keep the PR below 2000 changed lines/50 files. Publish draft stacked source,
then independent review of the final substantive push. Hosted migration
requires separate current-state reconciliation. Roll back application use,
retain expand structures, registrations, revocations and review history.
Do not stop the shared owned container while other consumers are active.
