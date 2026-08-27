# Flight plan: bind provider guidance to exact evidence and encounter version

## Outcome and boundaries

- Problem and measurable outcome: the provider surface must never display or
  copy a code unless its cited text is an exact, addressable slice of the
  loaded documentation, and a billing result must match the exact server-owned
  encounter version shown to the reviewer.
- In scope: server response version fields, exact evidence-location filtering,
  exact decision evidence for visibly held or removed candidates, fail-closed
  browser validation, and deterministic backend/frontend tests.
- Explicit non-goals: changing coding levels or diagnosis selection, editing
  clinical notes, persisting provider decisions, preparing or transmitting
  claims, enabling Athena or EHR writes, enabling patient outreach, changing
  authentication, or deploying.
- Affected roles, practices, services, environments, and data classes: the
  provider view available to provider, coder, and administrator personas;
  synthetic read-only demo data only. No operational practice or PHI cohort is
  activated by this change.

## Risk and dependencies

- Tier and reason: **Tier 2** because this tightens billing-evidence rendering
  behavior.
- Billing, evidence, outreach, PHI, or other high-blast-radius decisions: every
  visible candidate requires a nonempty exact source slice with valid integer
  bounds; for a held or removed code that slice supports the decision rather
  than the code, and the UI labels it accordingly. A result from a different
  encounter version is rejected before rendering or copy. Outreach, PHI, and
  external writes remain unchanged and disabled.
- Dependency-ordered PR stack; explain how each PR can be held safely: this is
  one follow-up slice based directly on PR #25's exact head. It remains local
  until the parent is merged or the maintainer separately authorizes updating
  that PR. Holding it leaves the open provider slice unchanged.
- Cross-repo contract/version and receiver-before-sender order: N/A. Both API
  fields and browser consumer change atomically in this repository. The added
  response field is backward compatible.

## Current deployed state

- Live image digest, commit mapping, configuration, and schema evidence: not
  asserted by this plan. Deployment requires a separate live-state
  reconciliation and promotion authorization.
- Difference from `main` and deployed-only behavior: `main` does not contain the
  open provider slice. This follow-up is based on PR #25 and must be replayed or
  merged only after that dependency is resolved.
- Reconciliation decision: retain the live deployment unchanged; assimilate
  this safety behavior only through reviewed `main`.
- Previous known-good artifact and data-recovery boundary: the current live
  digest remains the recovery artifact. This slice changes no persistent data.

## Release control (reversible user-visible behavior only)

- Control/latch, owner, expiry, cohorts, and activation authority: N/A. This is
  a fail-closed safety invariant, not optional user-visible behavior.
- Safe/dark default and removal PR/condition: N/A; removing the evidence or
  version check would weaken a hard rule and requires a new Tier 2 decision.
- If no reversible user-visible behavior, reason this is N/A: the safe response
  to malformed, unlocated, or stale evidence is always to withhold it.

## Schema/data sequence (schema/data work only)

| Change | Rung | PR | Invariant | Recovery |
|---|---|---|---|---|
| N/A | N/A | N/A | No schema or stored data changes | Revert application commit |

- Rehearsal runbook/evidence and PHI-boundary status: synthetic unit and API
  fixtures only; no real patient data is introduced.

## Rollout and promotion

- Ordered synthetic environments/cohorts and hold points: local deterministic
  suite, independent review, repository CI, then a separately authorized dark
  synthetic deployment. Hold before merge and again before deployment.
- Health signals and measurable abort thresholds: zero unlocated visible code
  cards; zero stale-version renders; focused and full Python/frontend tests and
  the anti-inflation proof green. Any failure aborts promotion.
- Canary journey, including anti-inflation/evidence/role-isolation checks: load
  one approved synthetic encounter, confirm its version, generate a review,
  inspect exact character offsets, confirm the contradicted family-history code
  is visibly removed with its verbatim denial labeled as decision evidence,
  confirm evidence-free exclusions are withheld, verify copied codes contain
  suggestions only, tamper the response version and confirm no result or copy
  control renders, then repeat persona denial checks.
- Founder-only manual production/PHI promotion record, or current control gap:
  no production or PHI promotion is authorized by this plan. Existing manual
  founder promotion and live reconciliation requirements remain blocking.

## Security and recovery

- Open security acceptances, scope, expiry, and promotion impact: existing
  synthetic-only acceptance remains the ceiling; this plan grants none.
- Required BAA/service/data-handling approvals: unchanged and still required
  before any real patient data or external model/service path.
- Recovery move for behavior, image, infrastructure, schema/data, messaging,
  and data damage as applicable: revert this application commit and restore the
  previous known-good image if separately deployed. No schema, data, messaging,
  or infrastructure repair is required.
- Observation window before irreversible cleanup: N/A; no irreversible cleanup.

## Completion

- Evidence required to call the outcome complete: focused API and rendering
  tests, frontend unit tests, production build, full Python suite,
  anti-inflation and PHI-log checks, clean diff, and independent agent review of
  the final commit.
- Follow-up cleanup PRs and owners: release owner restacks this one slice after
  PR #25 lands and records the reviewed commit in the PR description.
- Billing, clinical, deployment, and contract records to update: this flight
  plan and future PR evidence only. No clinical record, claim, or deployment
  record is created by local validation.
