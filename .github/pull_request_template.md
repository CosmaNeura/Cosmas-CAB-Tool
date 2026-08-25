<!-- deploy-kit v0.2.1 -->

## Outcome and scope

<!-- State one independently deployable outcome and explicit non-goals. -->

## Risk class

- [ ] **Tier 0 — routine:** no billing-decision, deployment, schema/data,
      auth, outreach, PHI, security-boundary, or external-contract impact
- [ ] **Tier 1 — deployment-sensitive/reversible:** deployment or release
      behavior changes and has a deliberate recovery move
- [ ] **Tier 2 — irreversible/high blast radius:** billing adjudication,
      destructive/large data, auth, outreach, PHI, security, production
      identity/network/promotion, breaking contract, or broad infrastructure

Classification reason:

## Evidence required for every code change

- [ ] Repository CI is green for the final substantive push
- [ ] `prove_no_upcode.py` and applicable guardrail tests are green
- [ ] Independent non-implementer agent review identifies the reviewed commit:
      <!-- durable PR comment/check link -->
- [ ] Agent findings are resolved or explicitly accepted in the PR
- [ ] Existing GitHub human-approval requirements are satisfied
- [ ] This PR is one independently deployable outcome; otherwise link the
      dependency-ordered split/stack decision

Agent evidence is not a GitHub approval. A later substantive push requires a
new review.

## Conditional controls

<!-- Fill what applies and mark the rest N/A with a reason. -->

- **User-visible reversible behavior:** release control, owner, expiry, and
  first cohort; otherwise N/A
- **Flight plan:** link for Tier 2, schema/data, cross-repo, multi-deploy, or
  cohort work; otherwise N/A
- **Schema/data:** one rung, rehearsal status/evidence, and rung-specific
  recovery; otherwise N/A
- **Cross-repo:** contract version and receiver-before-sender order; otherwise
  N/A
- **Security acceptance:** record, scope, expiry, and allowed environment/data
  boundary; otherwise N/A

## Deployment and recovery

- Deployment/runtime effect:
- Current live digest/config/schema comparison: evidence or reason N/A:
- Health/canary signal:
- Exact rollback or recovery move:
- Previous known-good artifact/data boundary:

Target production policy is founder-only manual promotion from merged `main`
and an already-built digest, recorded as self-authorization. The tracked
workflow now enforces the founder/manual/current-main/review boundary once
credentials are separately wired, but still builds during that promotion run
and has not established commit-to-live traceability. This checklist does not
claim those remaining blockers are resolved.

## Change record

- Systems and data affected:
- PHI impact: none / read / write / schema-data
- Tests and rehearsal actually run:
- Billing/clinical evidence impact:
- Documentation or runbook changed:
