# NPI and Claim-Submission Scope

Status: approved product boundary, amended 2026-08-28

## Current scope

CosmaNeura CAB is a human-reviewed billing-intelligence and assistance product. It produces evidence-linked recommendations, completeness findings, review dispositions, copyable output, and approved exports. Its coding/recommendation flow does not submit claims, emit 837P transactions, enroll with clearinghouses or payers, write to an EHR, or initiate patient contact. This does not retire the separately governed, default-off Card Chase outreach workflow, which remains subject to its existing consent, direct-contract, authorization, and operational-activation gates and does not transmit claims.

Public provider signup, practice onboarding, Checkout, trial activation, entitlement, and CAB use must work without a National Provider Identifier (NPI). An NPI is public registry data, not authentication, licensure evidence, credential approval, or tenant authority. CAB must not request it as a general onboarding field or use it to grant access.

## Workforce authorization boundary

Provider credential or licensure status is not a condition of signup, trial,
billing, continued platform use, entitlement, CAB capability, or real-PHI
access. The subscribing practice authorizes its workforce membership and roles;
Core enforces that authority together with verified identity, legal/BAA state,
subscription/entitlement, tenant isolation, consent, and rollout controls.
CosmaNeura must not claim that it verified a provider's license or credentials.

If a legacy credential/licensure field remains for compatibility, it is
customer-supplied, unverified informational metadata. It must not be labeled
`approved` or `rejected`, used in authorization, security, billing, or trial
logic, or retained beyond its documented purpose. CAB does not require or
consume it.

CAB accepts the approved minimum-necessary finalized-note packet defined by its
receiver contract. It does not ingest or retain raw audio, video, media chunks,
or transient transcription-provider payloads. John Clemente, CEO, is the
initial primary operational point of contact; secondary coverage and tested
escalation routes remain activation requirements.

## Allowed public-data use

Public NPI-bearing datasets may be used for de-identified/provider-level benchmarks, rate analysis, or public directory lookup. Those data-source uses do not authorize collecting an NPI from a customer, matching an account by NPI, or transmitting a claim.

## Future boundary change

Collect an NPI only just-in-time if an approved and enabled payer-facing HIPAA-standard transaction or integration demonstrably requires the appropriate Type 1 and/or Type 2 identifier. Before implementation, a new architecture decision must define:

- exact transaction or integration and customer benefit;
- legal, payer, and contractual basis;
- individual and organization NPI requirements;
- identity, tenant, and human-approval controls independent of NPI;
- minimum stored data, source, validation, retention, and deletion;
- clearinghouse/payer enrollment, EDI testing, liability, audit, and rollback; and
- explicit owner, counsel, security, and production-activation approvals.

Until that decision is approved, 837P generation, claim submission, clearinghouse enrollment, and NPI-gated onboarding remain out of scope.

EHR writeback is an independent boundary and is also not on the roadmap. It requires its own approved architecture decision even if no NPI or payer transaction is involved.

## Public legal-copy gate

The current counsel-approved public BAA v2.2 includes “claims preparation and submission support” in Exhibit A. This product-scope document does not amend or interpret that agreement. Before public promotion, counsel must either confirm in writing that the phrase is consistent with advisory-only CAB or approve a newly versioned agreement and acceptance record.
