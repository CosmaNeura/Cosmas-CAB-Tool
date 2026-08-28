# Flight plan: credential-neutral CAB workforce policy

## Outcome and boundaries

- Problem and measurable outcome: every current CAB governance surface states that provider
  credential/licensure status is informational only and never controls signup, trial, billing,
  continued use, entitlement, CAB capability, or real-PHI access.
- In scope: repository guidance, CAB product-scope documentation, and cross-repository authority
  wording.
- Explicit non-goals: no billing adjudication, API, schema, authentication, patient outreach,
  legal-text, infrastructure, deployment, or production change.
- Affected roles, practices, services, environments, and data classes: maintainers and operators;
  documentation only, with no customer or patient data.

## Risk and dependencies

- Tier and reason: Tier 0 documentation, coordinated with a cross-repository architecture decision.
- Billing, evidence, outreach, PHI, or other high-blast-radius decisions: none; CAB remains
  advisory-only and real-PHI activation remains separately blocked.
- Dependency-ordered PR stack: architecture PR #11 supplies ADR 0025/0026; this repository records
  the consumer boundary and may remain safely unmerged or undeployed.
- Receiver-before-sender order: Core remains the runtime identity/tenant/entitlement authority.
  CAB consumes only the future reviewed minimum-necessary packet.

## Current deployed state

- Live image, configuration, schema, and drift reconciliation: N/A for this docs-only change and
  must be performed separately before any future promotion.
- Reconciliation decision: retain current deployment; do not promote from this PR.
- Previous known-good artifact/data boundary: unchanged.

## Release control

N/A — no user-visible runtime behavior changes. Existing CAB, inference, Athena, and outreach
controls remain dark unless separately approved.

## Schema/data sequence

N/A — no schema or data change.

## Rollout and promotion

- Ordered environments/cohorts: merge documentation only after exact-head review; no deployment.
- Health signals/abort thresholds: documentation consistency checks and existing required CI must
  pass; abort on any claim that credential status grants or withholds access.
- Canary journey: N/A; no executable path changes.
- Founder production record: not authorized by this plan.

## Security and recovery

- BAA v2.2 remains byte-pinned. Counsel disposition of its claims-support phrase remains open.
- CAB receives no raw audio/video/media payloads; its future Core packet remains
  minimum-necessary and independently authorized.
- Recovery: revert the documentation commit; no runtime, data, or infrastructure rollback exists.

## Completion

- Evidence: exact-head independent review, green required CI, clean diff, and terminology scan.
- Follow-up: Core/Stripe contracts must implement the same credential-neutral policy before CAB is
  activated for public real-PHI use.
- Records: ADR 0025/0026 and the public launch change record remain authoritative.
