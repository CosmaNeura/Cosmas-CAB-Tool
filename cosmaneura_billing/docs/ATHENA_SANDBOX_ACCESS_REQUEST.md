# Athena Preview synthetic/read-only access request

Status: authenticated portal review completed 2026-08-20. An existing Preview
application has a nominally compatible configuration, but its ownership and
scope separation from the Sovereign lab integration must be confirmed before
it is assigned to this workload. Athenahealth does not offer a read-only
proprietary AthenaOne scope. No application, credential, or scope was created,
opened, rotated, or changed during this review.

This request is only for the billing/claims import lane in this repository.
The Sovereign provider-workspace lab feature must use a different OAuth client
ID, secret, token lifecycle, Key Vault secret, and runtime identity. Never reuse
or copy credentials between the billing importer and Sovereign, even if both
applications target Athena Preview Practice `195900`.

Official onboarding source:

- [athenahealth onboarding overview](https://docs.athenahealth.com/api/guides/onboarding-overview)
- [athenahealth Developer Portal applications](https://developer.api.athena.io/ams-portal/apps)
- [athenahealth sandbox guidance](https://docs.athenahealth.com/api/guides/testing-sandbox)
- [athenahealth Microsoft Technical Request Form](https://forms.office.com/r/Xzb2UpYqmT)

## Proposed application request (confirm in the authenticated portal)

| Field | Proposed value; confirm before submission |
|---|---|
| Application name | `Cosmas Billing Import Preview` |
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
- migration `009` is applied and the dedicated practice/source-bound import
  login has a separate DSN; neither the API nor chase worker can read it;
- the exact portal scope list is captured without credentials;
- the OAuth credential and import DSN are available only to the dedicated sync
  identity through separate, secret-scoped Key Vault role assignments;
- all other workloads have zero Athena settings or secret grants; and
- a bounded acceptance plan uses only sandbox synthetic records and emits
  count/code-only evidence.

## Prepared vendor support request (not submitted)

Athena's official documentation routes technical questions to the Microsoft
Technical Request Form above. Page 1 requires the submitter's name and email,
the Preview client ID, Practice ID, API Solutions Service/legacy Project ID,
an optional manager, and a request category such as API Access, API Consulting,
or Solution Validation. Practice ID is fixed to `195900`. Do not add personal
data, a client ID, or a Service/Project ID to this repository. Those values and
action-time user confirmation are required before submitting the form.

The prepared request text is:

> **Subject: Preview Practice 195900 — vendor-enforced read-only access for
> AthenaOne billing endpoints**
>
> We have a dedicated 2-legged OAuth Preview app, `Cosmaneura CAB Pilot`. Its
> Scopes page exposes AthenaOne only as the Read/Write selection
> `athena/service/Athenanet.MDP.*`. We need system-to-system READ access only,
> solely to athenahealth’s public synthetic Preview Practice `195900`; no
> production/customer practice, writes, uploads, messages, subscriptions,
> webhooks, or administrative operations.
>
> Please confirm whether athenahealth can provide a read-only AthenaOne scope,
> endpoint entitlement, app policy, or server-side method/path restriction
> narrower than `athena/service/Athenanet.MDP.*` for these candidate GET
> operations:
>
> - `/v1/195900/practiceinfo` and `/v1/195900/departments`
> - bounded reads of the documented Preview test patients
> - patient appointments and encounter summaries/diagnoses/services
> - appointment/patient insurance and benefit-detail reads
> - `/v1/195900/claims`, `/v1/195900/claims/{claimid}`, and
>   `/v1/195900/claims/{claimid}/claimtransactions`
>
> If the wildcard is required even for these GETs, can athenahealth bind this
> client server-side to Practice 195900 and the approved methods/paths? Please
> identify the exact supported endpoint paths and required query parameters for
> appointments, encounters, documentation, insurance/coverage, claims,
> statuses/transactions, and denial outcomes, and confirm which of those
> datasets are populated for the documented synthetic patients.
>
> For the 2-legged Preview access token, please also document the guaranteed
> token/introspection contract: expected issuer and audience, whether the
> access token is always a JWT verifiable through `/oauth2/v1/keys` or may be
> opaque, and whether `/oauth2/v1/introspect` always returns `active`, `exp`,
> `scope`, and `client_id`.
>
> Finally, please confirm whether any selectable FHIR R4 SMART V2 read/search
> scopes cover Appointment, Claim, ClaimResponse, or ExplanationOfBenefit in
> Preview. The portal currently exposes Patient, Encounter, Coverage,
> DocumentReference/Binary and other US Core resources, but not those billing
> resources.
