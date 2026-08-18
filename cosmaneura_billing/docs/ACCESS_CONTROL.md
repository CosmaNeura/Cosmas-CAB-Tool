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
conflicting identity headers and claims. In staging and production it then
requires an active, explicitly provisioned PostgreSQL membership for that same
tenant, object ID, and practice. Effective roles are the intersection of the
Entra token roles and the database membership roles; neither source can expand
the other. The Entra object ID becomes the reviewer key; display names and
email addresses are never authoritative.

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

There is no just-in-time user or membership creation. Login traffic never adds
a row, changes a role, or reactivates a user. The migration job bootstraps only
the explicitly configured initial administrator; subsequent changes use the
owner-only `python -m cosmaneura_billing.memberships` command after human
review. A missing/inactive membership is denied, while an unavailable
membership store fails closed with 503. Chase assignees must likewise be active
`Admin` or `BillingStaff` members of the same practice.

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
and ambiguous retry.

Staging and production store attempts and completions in practice-scoped
PostgreSQL under forced row-level security. The runtime login has insert-only
audit privileges—no update or delete. Coder-feedback submission commits the
normalized review and its allowed completion in the same transaction. Chase
mutations do the same when the chase and state stores use the required shared
`DATABASE_URL`; a rollback therefore cannot leave a successful mutation with a
missing completion. JSONL audit is a local development/test seam only.

Real PHI remains blocked until P5 logging/redaction, the BAA, and deployment
acceptance are all complete. These controls are not a claim of HIPAA
completeness.

## PostgreSQL boundary

Staging and production refuse to start unless encounters, feedback,
memberships, audit, and chase state use PostgreSQL. The API connects only as the
least-privilege runtime role; the migration owner DSN is confined to the
one-shot migration job and controlled membership administration. Both state
and chase use the same database so mutation/audit transactions cannot split
across stores. The migration runner binds the runtime login to exactly one
practice. Every connection also sets that configured practice context, and the
schema's forced row-level-security policies require both values to agree; the
runtime login cannot select another practice by changing the session setting.

The external EHR/demo encounter identifier is not copied into access audit.
Audit resource references use the encounter's internal UUID (or a validated
opaque chase ID), reducing accidental PHI leakage through identifiers.

## Local development

Synthetic/de-identified local work may use:

```text
COSMANEURA_ENV=development
COSMANEURA_AUTH_MODE=disabled
COSMANEURA_DEV_USER_ID=local-developer
COSMANEURA_DEV_ROLES=Admin
```

The bypass is rejected in staging and production. Never use it for a pilot or
with real patient data. Local JSONL feedback/audit files and the in-memory
encounter/chase stores are also rejected for staging and production.
