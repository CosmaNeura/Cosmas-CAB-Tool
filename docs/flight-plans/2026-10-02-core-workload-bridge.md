# Core completed-note workload bridge

Risk: Tier 2 (authentication, external contract, source authority, schema).
Base: `ec3038b0ce3bbee41b7becd4cc501bd9f8cfdadb` (reviewed provider model lane).

The provider workflow must import the current Core-approved completed note and
read, generate, or edit its existing CAB header without inventing an Entra user.
The bridge is a separately composed application, disabled until every workload
identity, source connection and runtime mapping is explicitly configured.

## Contract and boundaries

- Reuse `provider_completed_note` and existing source/header CAS and receipts.
  Add `core_workload_verified` source mode, never provider-attested provenance
  for workload verification. Completion remains the provider's confirmation,
  not an independently verified signature.
- Authenticate dedicated Entra app-only tokens with a maintained verifier.
  Reject browser/delegated tokens, unconfigured audiences/callers/roles, and
  caller-controlled token/key URLs. No synthetic X-MS identity headers.
- Project Core issuer/subject to an explicitly `core` internal user UUID under
  the existing CAB practice tenant partition. Ordinary Entra auth admits only
  `entra` identities. Immutable provenance identifies every Core actor.
- Reauthorize the exact current Core operation, action and request digest over
  fixed authenticated HTTPS. Core's source approval is authoritative only
  within the configured source connection. No manual fresh-operation grants
  and no body-only synthetic/deidentified assertion.
- Remote authorization is point-in-time, not a distributed lock. Check expiry
  and local mapping/source revocation in each actual CAB write transaction.
- Full pipeline and reviewed provider model authority remain mandatory for
  external inference. Read/storage authority alone never enables the model.
- Preserve original ordered-content hash, exact projected text/hash/version,
  and excludedRecordingTime metadata. Send/store no raw transcript.

## Delivery and local proof

First implement schema/auth compatibility, then consume it in the receiver and
header read/generate/save continuation. Split independently reviewable outcomes
before exceeding 2,000 changed lines or 50 files; never omit required guards.
Migration026 is additive; down preserves expanded structures and metadata.
No existing runtime role, database, preview or hosted configuration is changed.

Use local signed-token fixtures and fake Core/Azure transports with a new owned
PostgreSQL database. Prove wrong authority/actor/source/class is rejected,
ordinary Entra cannot use a Core projection, exact source intake and header
revision/replay remain atomic, and expired/revoked authority cannot authorize
new disclosure/persistence. Preserve existing Entra actor bytes/receipt hashes.
Run relevant guardrail proof once after the consumed workflow is complete.
The owner's September 25 review decision applies: local implementer review and
retained checks bind the published bytes; this is not independent agent review
or GitHub approval. The first shared browser flow passed. The revised-context
browser continuation remains a separate pending integration check.

## Recovery and remaining activation

Keep bridge configuration empty to leave all existing routes unchanged. Do not
roll back additive schema by dropping tables. Separate operator decisions must
ratify the Core/CAB authorization contract and configure workload audiences,
callers, roles, managed identities, source scope and model disclosure policy.
This work performs no real-PHI, vendor, messaging or hosted action.

## Receiver and operator configuration

The shared image contains the `core_bridge` extra but does not start this app.
An explicitly composed workload runs `uvicorn cosmaneura_billing.core_bridge:app`
with the same approved PostgreSQL/header settings and its distinct runtime login.
`COSMANEURA_CORE_BRIDGE_POLICY_JSON` defaults empty. Its closed schema is
`cab-core-bridge-policy.v1` with `inbound` (`tenant_id`, `audience`, `caller_id`,
`role`), `coreOrigin`, `coreTokenScope`, and `managedIdentityClientId`. New,
reviewed Core/CAB audiences and roles are required; browser tokens never suffice.
The owner-only provider-admission CLI supports `register-core-connection` and
`register-core-actor`; neither is mounted over HTTP. Connection registration
creates a fresh dedicated bound login, not a grant upgrade to an existing API.
Revocation uses `revoke-core-connection` / `revoke-core-actor`. Ordinary Entra
membership cannot authenticate an explicitly Core-projected UUID.

Core intake always sends `expectedSource:null`. After exact current Core
reauthorization, CAB resolves the prior source pin for that server-derived record
and requires a newer source revision. Existing transactional CAS rejects races.
An exact retry verifies the persisted Core operation digest and recovers its
original grant pin; a superseded receipt returns `current:false, source:null`.
Header read/generate/save still require the returned exact source snapshot.
Core authorizations last at most30seconds; up to2seconds future issuance skew is
accepted, but expiry has no grace. Source intake uses actual database time.
The callback is rechecked at use and before persistence. Core revocation is
point-in-time across databases; CAB does not claim a distributed transaction.

The returned source DTO carries exact projected text, explicit service date and
provider-confirmed completion, recording-time omission metadata, and model
availability. Read-time availability uses the current read/accept authorization;
actual Azure egress additionally requires the active `header.generate` purpose.
Saved header review remains available when model permission is removed.
