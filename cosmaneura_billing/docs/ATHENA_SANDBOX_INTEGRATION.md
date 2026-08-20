# Athena sandbox integration plan

Status: mock adapter and dormant job infrastructure implemented locally; no
Athena credential, network call, imported record, or production write path is
enabled. The exact Preview enrollment boundary is captured in
[ATHENA_SANDBOX_ACCESS_REQUEST.md](ATHENA_SANDBOX_ACCESS_REQUEST.md).

## Deployment decision

Athena sandbox access does **not** require a second public CosmaNeura API.  The
first integration should be an outbound, no-ingress sync job that shares the
existing server-owned practice context and writes through a narrowly scoped
import identity.  The reviewer-facing API remains the existing Entra-protected
Container App.

A separate CosmaNeura deployment is justified only when the sandbox represents
a different practice or Entra tenant, or when the owner deliberately chooses a
separate isolation boundary for experimental credentials and data.  The current
runtime binds one deployment to one `COSMANEURA_PRACTICE_ID` and one Entra
tenant; request data must never select either value.

## Athena application request

Request a dedicated CosmaNeura Athena sandbox application/client rather than
reusing another workstream's credentials.  The initial grant must be:

- sandbox and synthetic-patient only;
- read-only and minimum-scope;
- limited to the records needed to validate encounters, coverage snapshots,
  billing slips/claims, and denial outcomes;
- independently revocable and auditable; and
- free of write scopes until a separate write acceptance is approved.

Do not guess endpoint paths, OAuth behavior, scopes, or payload fields.  Capture
the exact approved sandbox contract as sanitized fixtures after access is
granted.

## Read-first data flow

```text
Athena sandbox synthetic records
  -> outbound HTTPS from a no-ingress Athena sync job
  -> strict athena_client.py parsing and schema validation
  -> server-fixed practice binding and forced-RLS PostgreSQL
  -> existing Entra-authenticated CosmaNeura API and UI
```

The sync job should have its own user-assigned identity.  It may read only its
Athena credential from a secret-scoped Key Vault grant and use a least-privilege
database import role.  The UI API, migration job, and chase worker must not
inherit that credential or role.

The adapter must be mock-first.  All Athena protocol knowledge belongs in
`athena_client.py`; downstream code consumes internal types such as
`VisitBundle` and `chase.models.CoverageSnapshot`.  Required runtime properties
include bounded pagination, a durable cursor/checkpoint, idempotent imports,
strict response limits, sanitized errors, and count-only operational logs.

The VNet currently has no documented static-egress or Athena host-allowlist
contract.  Verify Athena's egress/IP requirements before provisioning the job.

## Safety gates before a sandbox call

1. Protected URLs and every platform-sidecar log stream must be free of
   encounter, patient, claim, chase, and delivery identifiers.
2. The deterministic mock must cover success, pagination, duplicate replay,
   partial response, unknown fields, schema failure, auth failure, timeout, and
   retry exhaustion.
3. Athena practice identifiers are validated as data but never choose the
   server's practice or tenant context.
4. Sandbox credentials live only in Key Vault; no API key, token, payload, or
   raw vendor error reaches application responses or logs.
5. The first live acceptance uses only Athena-provided synthetic records and
   proves exact role, network, cursor, idempotency, RLS, and log boundaries.

## Write path (deferred)

Do not begin with chart, insurance, note-section, or claim writes.  A future
write flow requires a separately approved design:

```text
human-reviewed command
  -> durable outbox + idempotency key
  -> Athena adapter write
  -> vendor read-back verification + retained vendor write ID
  -> local state transition
```

No local state may claim success before read-back verification.  Encounter-note
section writes remain unverified, and the CardCapture/insurance-image flow stays
in its separate athenaNet workstream.

## Parallel demo-data lane

The curated synthetic demo pack and its evaluation harness proceed independently
of Athena access.  Athena is the eventual workflow and outcome source, not a
prerequisite for demonstrating the local synthetic review experience.
