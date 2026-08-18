# Pilot access-control boundary

The pilot uses Microsoft Entra ID through Azure Container Apps built-in
authentication. Container Apps validates the session before forwarding the
request and injects `X-MS-CLIENT-PRINCIPAL`; the application accepts that
header only when `COSMANEURA_AUTH_MODE=container_apps`. Production and staging
refuse to start with authentication disabled.

This is a strict network trust boundary. The application does not validate a
signature on the injected header itself, so the container must have no route
that bypasses Container Apps built-in authentication. Before public ingress is
left enabled, the deployment acceptance check must prove that an anonymous
caller cannot gain access by supplying any `X-MS-CLIENT-PRINCIPAL*` header.

## Principal and roles

The application requires one valid Entra tenant ID, one immutable object ID,
and at least one configured app role. It rejects malformed, duplicate, or
conflicting identity headers and claims. The Entra object ID becomes the
reviewer key; display names and email addresses are never authoritative.

| Surface | Roles |
|---|---|
| View encounters and run billing/completeness | `Admin`, `BillingStaff`, `Coder`, `Provider` |
| Submit coder feedback | `Admin`, `Coder` |
| View aggregate feedback | `Admin` |
| View or act on the chase worklist | `Admin`, `BillingStaff` |

Configure the Entra enterprise application with **Assignment required = Yes**,
define exactly those app roles, and assign only pilot staff or controlled staff
groups. A role in the client payload is not accepted. Legacy identity inputs
(`userId`, `coder_id`, `actor`, `?user_id`, and `?coder_id`) return a validation
error instead of being ignored.

Public surfaces are limited to the same-origin frontend assets, Container Apps
authentication endpoints, and shallow `/api/live` and `/api/ready` probes.
Every other `/api/*` route requires the trusted principal.

## Request controls

- Production is same-origin. Development CORS accepts only explicitly listed
  origins; wildcard origins and credentials are disabled. Simple requests with
  a foreign `Origin` are rejected server-side.
- A pure ASGI limiter checks both declared and actually streamed body bytes
  before JSON parsing. The default is 1 MiB, and compressed request bodies are
  rejected.
- Authenticated requests have separate inference and general fixed-window
  limits. They are keyed by practice, immutable object ID, and route class.
  The limiter is process-local, so with two replicas the effective deployment
  allowance can be twice the per-process value. It is an abuse brake, not a
  billing quota; a shared limiter is required before treating it as global.

## Access audit

Access events are separate from the claim-decision audit. Each event contains
only a generated event ID, UTC time, practice, immutable actor ID and roles,
semantic action, bounded resource ID, declared route template, method, status,
outcome, and the immediate peer address. Raw paths, query values, request or
response bodies, notes, transcripts, evidence, authorization headers, Entra
principal blobs, and forwarding headers are never recorded.

Every protected action first appends a durable `attempted` reservation with the
authenticated actor, before a route can read or mutate data. A completion with
the same event ID then records status and outcome. If the reservation cannot be
written, the route never runs and returns 503; this prevents an unlogged write
and ambiguous retry. P2's production adapter is an append-only JSONL seam, so
P3 still must make each mutation and completion transactional in tenant-scoped
PostgreSQL. The temporary Azure path is ephemeral by design, so real PHI is
still blocked until P3, P5 logging/redaction, the BAA, and deployment acceptance
are all complete. This P2 control is not a claim of HIPAA completeness.

## Local development

Synthetic/de-identified local work may use:

```text
COSMANEURA_ENV=development
COSMANEURA_AUTH_MODE=disabled
COSMANEURA_DEV_USER_ID=local-developer
COSMANEURA_DEV_ROLES=Admin
```

The bypass is rejected in staging and production. Never use it for a pilot or
with real patient data.
