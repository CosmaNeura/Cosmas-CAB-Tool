# Athena staff protocol foundation

Tier 2. Base: a21310b8a30bfa046915de4709fa931de438000f.
Preserved implementation: 8d0caca22aaed8001aaeec955c81c3f7ecc11bd4.

## Outcome and scope

Bring forward the complete final staff read protocol, immutable practice/case
scope, and read-lease boundary without selecting an application transport.
The two supporting modules retain exact preserved bytes. Independent review
found an optional summary practice-ID binding gap in the preserved client; this
integration rejects supplied wrong/malformed practice IDs and retains absent-ID
compatibility. The client also corrects stale comments about the lease gate.
The existing client/sync/import behavior remains unchanged. No API, settings,
UI, schema, credentials, source data, or deployment configuration changes.

The protocol retains canonical request identities, numeric response IDs,
bounded JSON, fixed origins/routes, strict continuation semantics, five-second
individual socket limits, and a 25-second logical read deadline. Encounter
department observations remain separate from the chart query context. Source
metadata is checked before and after summary retrieval; completion/signature
remain unverified and no note is automatically admitted for coding.

Production construction requires an exact transaction-lived AthenaReadLease.
This port has no caller of resolve_lease, registry schema, admission record,
service factory, or application route for issuing that lease. The resolver is
preserved as a dormant dependency; it must not be invoked until the separately
reviewed registry, actor authorization, and storage transaction are integrated.
The existing one-shot sync still runs only its mock lane.

## Validation and recovery

Run the complete existing staff client/profile tests and original client/sync
compatibility checks with candidate imports. Carry the original direct
department protocol cases; their store/import cases remain with the later
review workflow. Use authored transports only, with network and child-process
attempts denied. No database, model, vendor, or packaging run is needed here.
Record exact source hashes, commands, results, and deferred test names under
outputs/mcd-training-2026-10-02/athena-protocol-integration.

Independent review follows the final substantive push. Recovery is reverting
this dormant code slice; no schema or stored-data rollback exists. Next comes
the final contact/insurance candidate mapper and human-reviewed case receipt,
then staff API/UI wiring. Case registry/admission and operational source/model
approval remain separate from these protocol metadata objects.

## Review correction AP-01

Before accepting summary text, validate its optional practiceid with the same
canonical response-ID rule as patient and encounter IDs and compare it with the
server-owned vendor practice. Exercise absent/string/integer controls and wrong,
boolean, float, zero-prefixed and null IDs for legacy Preview, explicit Preview
and an authored leased Production scope. This changes no activation or source
completion rule. Final independent review must bind the corrected pushed SHA.
