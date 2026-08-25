# Flight plan: name the outcome

Use this template only for Tier 2, schema/data sequences, cross-repo rollouts,
or changes released over multiple deploys/cohorts. Delete instructional text
and mark inapplicable sections `N/A` with a reason.

## Outcome and boundaries

- Problem and measurable outcome:
- In scope:
- Explicit non-goals:
- Affected roles, practices, services, environments, and data classes:

## Risk and dependencies

- Tier and reason:
- Billing, evidence, outreach, PHI, or other high-blast-radius decisions:
- Dependency-ordered PR stack; explain how each PR can be held safely:
- Cross-repo contract/version and receiver-before-sender order:

## Current deployed state

- Live image digest, commit mapping, configuration, and schema evidence:
- Difference from `main` and deployed-only behavior:
- Reconciliation decision: assimilate / retain temporarily / replace
- Previous known-good artifact and data-recovery boundary:

## Release control (reversible user-visible behavior only)

- Control/latch, owner, expiry, cohorts, and activation authority:
- Safe/dark default and removal PR/condition:
- If no reversible user-visible behavior, reason this is N/A:

## Schema/data sequence (schema/data work only)

| Change | Rung | PR | Invariant | Recovery |
|---|---|---|---|---|
| | | | | |

- Rehearsal runbook/evidence and PHI-boundary status:
- If no schema/data change, reason this is N/A:

## Rollout and promotion

- Ordered synthetic environments/cohorts and hold points:
- Health signals and measurable abort thresholds:
- Canary journey, including anti-inflation/evidence/role-isolation checks:
- Founder-only manual production/PHI promotion record, or current control gap:

## Security and recovery

- Open security acceptances, scope, expiry, and promotion impact:
- Required BAA/service/data-handling approvals:
- Recovery move for behavior, image, infrastructure, schema/data, messaging,
  and data damage as applicable:
- Observation window before irreversible cleanup:

## Completion

- Evidence required to call the outcome complete:
- Follow-up cleanup PRs and owners:
- Billing, clinical, deployment, and contract records to update:
