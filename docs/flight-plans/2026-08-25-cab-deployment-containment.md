# Flight plan: contain CAB registry and pilot deployment mutations

## Outcome and boundaries

- Problem and measurable outcome: adding Azure identity configuration must not
  cause a merge to `main` to publish or deploy an image. Every registry or
  pilot mutation must instead come from a founder-started manual run tied to
  the exact current `main` commit and its independent-agent review record.
- In scope: source workflow gates, rerun/stale-commit defenses, policy tests,
  and documentation of the actual control boundary.
- Explicit non-goals: creating GitHub environments or Azure credentials,
  changing human approval settings, querying or changing live Azure state,
  enabling PHI, running a migration, publishing an image, or deploying.
- Affected services and data: the CAB container registry, migration job, API,
  and chase worker. This source-only rung handles no patient data.

## Risk and dependencies

- Tier: **Tier 2**, because this changes production identity and promotion
  machinery and the workflow can start an ordered-SQL migration job.
- Billing, evidence, outreach, and PHI decisions: unchanged. Both patient
  messaging latches remain outside the workflow and must remain off.
- Dependency order: merge the source controls; perform independent review of
  the exact merged commit; separately reconcile live state; only then consider
  credential wiring and a manual run.
- Cross-repo contract: N/A; this is repository-local containment.

## Current deployed state

- At preparation time, the tracked workflow was inert because
  `AZURE_CLIENT_ID` was not configured for this repository. This change does
  not create that variable, any secret, or any environment control.
- No fresh read-only Azure reconciliation was performed in this source-only
  rung. The current live digest, configuration, schema, and deployed-only
  behavior therefore remain unknown and must be reconciled before promotion.
- The repository currently has no configured GitHub environments. The
  workflow's `pilot` reference therefore has no required-reviewer gate, and a
  future run could implicitly create an unprotected environment unless it is
  separately configured. Founder-only execution is self-authorization; the
  exact-SHA independent-agent review record is a separate source gate and is
  not a GitHub approval.
- Reconciliation decision: pending. Never deploy repository `main` over live
  drift without an explicit keep, assimilate, or replace decision.
- Previous known-good artifact/data boundary: unresolved pending that same
  reconciliation.

## Release control

N/A. This is deployment-process containment, not reversible user-visible
behavior, so a feature flag would not be a recovery control.

## Schema/data sequence

N/A for this source-only change. A future run may invoke the existing migration
job; its exact migration rung, rehearsal, and recovery evidence must be added
to that release record before the run is authorized.

## Rollout and promotion

1. Validate the workflow policy and normal repository CI.
2. Obtain independent non-implementer agent review of the final substantive
   pre-merge commit and record its exact SHA in a durable PR comment.
3. Merge without changing the repository's current human-approval settings.
4. Independently review the exact merged commit now at `main` and add the
   workflow-format `PASS`, reviewed merge SHA, and reviewer-agent lines to a
   durable comment on that merged PR. The pre-merge review does not substitute
   for this exact-current-`main` promotion record.
5. Reconcile the live digest, configuration, schema, and messaging latches.
6. Treat credential/environment wiring as a separate Tier 2 change.
7. If a later release is authorized, `JohnClem` must manually dispatch the
   workflow from exact current `main`; reruns by any other actor or after
   `main` advances fail closed.

Hold immediately if the review record does not bind the release SHA, `main`
advances, the deploy latch is not exactly `true`, the digest is not immutable,
the migration fails, messaging gates change, or the protected API smoke is not
401.

## Security and recovery

- Open security acceptance: real-PHI readiness is not established; synthetic
  or properly de-identified data only.
- Recovery for this source rung: revert the workflow commit. If credentials are
  later wired, first ensure no run is in flight. A source revert does not undo
  an image publication, migration, or Azure revision.
- Runtime recovery remains release-specific. Additive migration structures are
  left dormant rather than dropped as an immediate rollback.
- Residual architectural gap: a separate secretless workflow can now retain a
  reviewed image archive, but build/publish and deployment still occur in one
  manual workflow run. Promotion does not yet consume that retained artifact.

## Completion

- Required evidence: policy tests, shell/YAML validation, repository CI, and
  independent agent review tied to the final commit.
- Follow-up: make founder promotion verify and consume the retained image
  artifact, then record live reconciliation before any credential wiring or
  deployment. That promotion work is deliberately outside this source-only
  containment rung.
- Records to update: the deployment source-of-truth map and the eventual
  post-promotion evidence record. No live-state claim is made by this change.
