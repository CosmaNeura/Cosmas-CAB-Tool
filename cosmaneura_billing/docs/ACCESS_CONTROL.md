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
| View encounters and run provider billing/completeness | `Admin`, `Coder`, `Provider` |
| View coder review and submit coder feedback | `Admin`, `Coder` |
| View aggregate feedback | `Admin` |
| View the chase worklist/contact-task projection | `Admin`, `BillingStaff`, `Coder` |
| Detect, assign, or record chase state transitions | `Admin`, `BillingStaff` |
| Register, approve, activate, or revoke a chase policy | `Admin` |
| Release an exact direct-contract review or per-message touch | `Admin` |

The same rules drive navigation, but navigation is not the security boundary.
`GET /api/session` returns server-derived view capabilities, and every data
route independently enforces the corresponding role set:

| App role | Visible tabs |
|---|---|
| `Provider` | Provider only |
| `BillingStaff` | Insurance follow-up (Card Chase) only |
| `Coder` | Provider, Coder review, and Insurance follow-up |
| `Admin` | Provider, Coder review, and Insurance follow-up |

The frontend never interprets raw role names. A provider or billing-staff user
cannot reveal a hidden surface by modifying browser state because the denied
backend route returns 403. Actual user-to-role assignment remains an Entra and
practice-membership administration operation; login never creates or expands a
membership.
If a Provider token is mistakenly also assigned `BillingStaff`, Provider remains
the restrictive effective persona and still receives only the Provider surface.
Only `Coder` or `Admin` intentionally expands a login to all three views.

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
authentication endpoints, and the shallow `/api/live` probe. The more expensive
`/api/ready` dependency check requires an authenticated session at the external
Easy Auth edge. Container Apps startup and readiness probes still reach that
route directly inside the app environment, before the external auth edge. Every
other externally reachable `/api/*` route requires the trusted principal.

## Request controls

- Production is same-origin. Development CORS accepts only explicitly listed
  origins; wildcard origins and credentials are disabled. Simple requests with
  a foreign `Origin` are rejected server-side.
- Every response produced by the application, including its auth denials and
  generic errors, carries a same-origin CSP with framing and object embedding
  disabled, legacy `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, a
  bounded referrer policy, and a permissions policy disabling camera,
  geolocation, and microphone. Deployed HTTPS responses additionally carry
  one-year HSTS; local HTTP development deliberately does not.
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

Real PHI remains blocked until P5 logging/redaction, deployment acceptance, and
every applicable service-specific agreement/BAA and approved data-handling
configuration are complete. A practice BAA does not by itself authorize a
cloud or model disclosure. These controls are not a claim of HIPAA completeness.

## Azure OpenAI inference boundary

The pilot's only permitted live extraction target is a dedicated Azure OpenAI
account in North Central US. The account has local authentication and public
network access disabled and is reached through its private endpoint. Only the
API user-assigned managed identity receives the account-scoped inference role;
the migration and chase-worker identities receive neither model configuration
nor authorization. The API receives a non-secret endpoint, deployment name,
model label, and API version. It receives no model API key, static bearer token,
or Key Vault model credential.

Application inference is separately fail-closed behind
`COSMANEURA_AZURE_OPENAI_ENABLED`, which defaults false. Enabling it requires the
complete reviewed local Azure configuration; invalid or credential-based
configuration fails startup instead of falling back to another live provider.
Startup does not prove token issuance, private DNS, RBAC, the model deployment,
or a data-plane response—those require the controlled synthetic acceptance run.
Responses report `inferenceMode=azure_openai` and an allowlisted model label
without exposing the endpoint, provider request IDs, or chart content.

Catalog and ARM validation prove only that the proposed `gpt-5.6-sol`
2026-07-09 `DataZoneStandard` deployment shape is accepted by Azure. Before
application enablement, a synthetic/de-identified data-plane test must exercise
both strict JSON Schema output and the JSON-object fallback, refusals,
truncation, token accounting, and PHI-safe telemetry. Real chart text remains
blocked until the executed Microsoft agreement/BAA is verified to cover Azure
OpenAI and the US data zone and the service retention/data-processing posture is
documented.

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

### Chase scheduler/worker split

The API and scheduled chase worker are separate least-privilege database
principals bound to the same practice. The API role owns authenticated human
inputs: detection/contact snapshots, immutable policy revisions, Admin
approval/revocation events, and exact review or per-message releases. It cannot
claim or finalize delivery-outbox rows. The worker role can read only the chase
authorization evidence needed to schedule and can write only cadence, outbox,
delivery-event, pending-task, and narrowly validated accepted-attempt effects.
It cannot read identity, encounter, feedback, or access-audit data and cannot
mint a policy, approval, release, or consent decision.

The worker reads only `COSMANEURA_CHASE_WORKER_DATABASE_URL`; it never falls
back to the API's `DATABASE_URL`. Its DSN/password and
`COSMANEURA_ACS_EMAIL_CONNECTION_STRING` belong only to the scheduled job and
must never be exposed to the API container. The API and worker receive the same
`COSMANEURA_ACS_EMAIL_ENDPOINT`, sender, and reply-to policy-fingerprint
metadata, plus the same chase-gate HMAC key. A deployed scheduler requires that
key to contain at least 32 bytes even when `record_only` is selected.

An Admin approval is bound to the complete immutable policy fingerprint.
Direct-contract review releases are bound to the current gate evidence, and
per-message releases are bound to the exact policy revision, cadence step, and
contact snapshot. Drift invalidates the authorization. Revocation removes the
active policy and cancels only queued work that provably has not called the
provider; submitting, accepted, or ambiguous operations are not retried.

The policy status/create/approve/activate/revoke and exact per-message release
API surfaces are Admin-only. Policy and template bodies are derived from server
configuration rather than accepted from request bodies. Durable detection
commits the entire batch and its audit completion atomically, returns counts
only, and leaves row retrieval to the authenticated worklist. Logging a
scheduler-created phone attempt completes the exact durable staff task and
advances its cadence in the same audited transaction.

The user-facing worklist is a minimum-necessary projection: it omits patient
references, gate payloads, assignee identifiers, attempt notes, and contact
destinations. `POST /api/chase/task` accepts an opaque chase ID only in its JSON
body and returns either a fixed digest-pinned synthetic manual-call destination
or an athenaNet system-of-record placeholder. A direct-contract review withholds
even the synthetic destination until the human gate is resolved. No identifier
is accepted in a chase URL.

`POST /api/chase/demo-seed` is an authenticated Admin operator action, not an
anonymous/public seed path. It requires the exact bundled pack digest, writes
all four cases and contact snapshots atomically in the durable topology, and
records the Admin access completion in that same transaction. Exact replay is
a no-op; partial or lookalike state conflicts. It refuses to run unless the
scheduler, patient-messaging, and operational-activation latches are all off and
the configured transport is `record_only`. Synthetic contact disclosure also
requires server-owned persisted seed provenance containing the exact pack and
case digests; matching a patient reference or chase ID is insufficient.

The one-shot worker is invoked with
`python -m cosmaneura_billing.chase.worker --once`. `record_only` is the default:
it creates no patient message but may create a durable manual phone task for
staff. There is no outbound voice implementation and no SMS transport in this
slice. ACS email additionally requires the scheduler, patient-messaging enable,
and operational-activation latches. Any unresolved ambiguous provider
submission halts new outreach. Keep real messaging disabled until the Azure
deployment layer, the applicable Microsoft/ACS agreement and operational
approvals, P5 logging/redaction, and deployment acceptance are all complete.

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
