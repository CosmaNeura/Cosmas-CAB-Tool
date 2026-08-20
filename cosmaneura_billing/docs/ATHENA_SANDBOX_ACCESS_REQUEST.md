# Athena Preview synthetic/read-only access request

Status: authenticated portal review completed 2026-08-20. An existing Preview
application already matches the proposed integration, but athenahealth does not
offer a read-only proprietary AthenaOne scope. No application, credential, or
scope was created, opened, rotated, or changed during this review.

Official onboarding source:

- [athenahealth onboarding overview](https://docs.athenahealth.com/api/guides/onboarding-overview)
- [athenahealth Developer Portal applications](https://developer.api.athena.io/ams-portal/apps)
- [athenahealth sandbox guidance](https://docs.athenahealth.com/api/guides/testing-sandbox)

## Proposed application request (confirm in the authenticated portal)

| Field | Proposed value; confirm before submission |
|---|---|
| Application name | `CosmaNeura CAB Synthetic Read Validation` |
| Environment | Preview / public sandbox |
| Authorization category | 2-Legged OAuth |
| Data authorization | System-level, sandbox synthetic records only |
| Sandbox practice | Public Preview Practice `195900` |
| API access level | Certified APIs plus only the non-Certified read APIs proven necessary below |
| Redirect URL | None; a server-to-server client-credentials flow does not use a browser callback |
| Production access | Not requested |
| Write access | Not requested |
| Webhooks/events | Not requested |

## Authenticated portal finding — read-only scope unavailable

The existing `Cosmaneura CAB Pilot` Preview application is already configured
as a 2-Legged OAuth app with Non-Certified and Certified API access. Its app
description limits the first phase to read-only sandbox billing and coverage
workflows and explicitly defers production and write behavior.

However, the authenticated Scopes page exposes the proprietary AthenaOne
product as one checked `Read/Write` selection. The resulting scope is
`athena/service/Athenanet.MDP.*`; the portal does not expose separate read and
write scopes for that product. The edit screen was cancelled without changing
the grant, and the Credentials page was not opened.

Therefore the desired boundary cannot be enforced by the vendor scope for the
proprietary billing/claim APIs. The current broad Preview grant must remain
unused while the sync lane is disabled. Before any token request, choose and
approve one of these narrower operating strategies:

1. use only Certified/FHIR read scopes for the limited data they can provide,
   accepting that this will not cover the full claims/denials workflow; or
2. retain the Preview AthenaOne wildcard but enforce a reviewed GET-only
   endpoint allowlist, synthetic Practice `195900`, and read-only adapter
   contract in code and acceptance tests, with no generic request method
   exposed to the sync workflow.

Option 2 provides the intended billing effectiveness but is an explicit
least-privilege exception at the vendor OAuth layer. It requires separate user
approval before retrieving or storing the existing client credential.

For any future Certified/FHIR addition, read the exact scope names from the
authenticated Scopes page. Do not guess or broaden scope strings in code or
documentation. Select only read scopes needed for:

1. practice and department reference data required to validate identifiers;
2. appointments and encounters;
3. minimum synthetic patient demographics needed to join those records;
4. encounter documentation needed for the billing extraction evaluation;
5. insurance, coverage, and eligibility snapshots;
6. billing slips, claims, claim status/history, denials, and remittance outcomes.

Exclude every create, update, delete, upload, messaging, subscription, and
administrative scope. If the portal cannot separate a required read operation
from write authority, leave it unselected and record the gap for review.

## Grant boundary

The requested grant is limited to athenahealth's Preview public sandbox and its
synthetic records. It does not authorize access to a customer Preview
tablespace, a production practice, or real/deidentified PHI. Any customer
Preview or Production request remains a separate approval and contracting
milestone.

The public athenahealth onboarding and sandbox guides describe Preview sandbox
access as self-service: creating a Preview app and confirming compatible scopes
automatically grants those credentials access to public sandbox Practice
`195900`. Confirm that behavior and the exact selectable fields in the
authenticated portal before submission. No customer-practice authorization is
intended as part of this request.

After creation, store the client secret directly in the existing private Azure
Key Vault under the reviewed Athena secret name. Never paste it into GitHub,
parameter files, chat, logs, screenshots, or the API container. Record only the
client ID, selected scope names, creation time, owner, and expiration metadata.

## Acceptance before the first token request

- the repository mock suite and dormant infrastructure are merged;
- the sync job remains disabled and manual with no ingress;
- the exact portal scope list is captured without credentials;
- the credential is available only to the dedicated sync identity through one
  secret-scoped Key Vault role assignment;
- all other workloads have zero Athena settings or secret grants; and
- a bounded acceptance plan uses only sandbox synthetic records and emits
  count/code-only evidence.
