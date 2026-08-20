# Platform handoff — Azure OpenAI migration

**Date:** 2026-08-18

**Scope:** platform/deployment lane; no coding-policy or UI/UX changes

**Cost plan:**
[`docs/deployment/2026-08-18-azure-openai-cost-plan.md`](docs/deployment/2026-08-18-azure-openai-cost-plan.md)

**Current evidence:**
[`docs/deployment/2026-08-19-demo-release-candidate-evidence.md`](docs/deployment/2026-08-19-demo-release-candidate-evidence.md)

This document supersedes the Anthropic-specific and pre-deployment instructions
in `HANDOFF_CODEX_2026-08-17.md`. It does not supersede `CLAUDE.md`, the
anti-inflation invariant, evidence requirements, or the discovery contracts.

## Current position

- The Azure pilot platform is private and inference remains off. No Anthropic
  credential or Anthropic environment variable is deployed.
- The application live-extraction adapter is Azure OpenAI only. It authenticates
  through the API user-assigned managed identity, rejects model API keys/static
  tokens, requires explicit enablement, and otherwise uses the deterministic
  offline model.
- Infrastructure provisioned one dedicated private Azure OpenAI account in North
  Central US. Public network access and local authentication are disabled. The
  application-specific account assignment is on the API identity; separately,
  the signed-in human inherits subscription-level Foundry User and requires a
  minimum-necessary review before any real-PHI decision.
- The pinned deployment contract is `gpt-5.6-sol`, version `2026-07-09`,
  `DataZoneStandard`, capacity 200, no automatic model upgrade. The data-plane
  API version is `2025-04-01-preview`.
- Live catalog/ARM validation and bounded synthetic data-plane acceptance passed
  on image `1c5d7e7`; the application was then returned to inference-off. The
  2026-08-19 release-candidate baseline `de2b16f` was not deployed, so acceptance
  must be repeated for the eventual merged release digest. Neither result proves compliance coverage or production
  readiness.
- The adapter selects strict JSON Schema Structured Outputs for locally
  compatible schemas and selects JSON-object mode up front for schemas outside
  the supported strict subset. Both paths still pass deterministic schema
  validation; a provider 4xx is terminal and never triggers a weaker retry.
  OpenAI documents the API distinction in its
  [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs);
  Azure hosting, model deployment, identity, networking, quota, and data-zone
  operations are Microsoft Azure concerns.

## Executed through synthetic acceptance

The cost addendum was approved, the private account/model/network path was
provisioned, and a bounded synthetic acceptance run passed strict-schema,
JSON-object, private-DNS, managed-identity, closed-log-schema, and canary checks
on image `1c5d7e7`. The final live revision is private and inference-off. GitHub
Actions run `32244337843` passed for `1c5d7e7`; post-merge run `32317475914`
passed for the release-candidate baseline `de2b16f`, and CodeQL setup run `32317672236`
passed both configured languages. Current `main` is not yet deployed.

## Required order from here

1. Merge the reviewed release candidate, build its commit-derived tag, resolve
   and record its immutable manifest digest, and deploy that digest
   privately with inference, Language, scheduler, and messaging latches false,
   and rerun the repository-owned synthetic acceptance harness.
2. Reconfirm private DNS, managed-identity scope, closed-schema metrics, the PHI
   canary scan, and a final zero-LLM-event observation after disabling inference.
3. Review the signed-in human's inherited subscription-level Foundry User role
   for minimum-necessary access.
4. Recalculate the operating forecast from measured p50/p95 input, cached-input,
   and output tokens and expected encounter volume.
5. Keep inference disabled and continue with synthetic/de-identified data until
   the real-PHI gate below is documented and approved.
6. Never fix an anti-inflation failure by weakening its proof.

## Real-PHI gate

Real chart text remains prohibited until all of the following are true:

- the executed Microsoft agreement/BAA is verified to cover Azure OpenAI and
  the selected US data zone;
- the service's data-processing, retention, abuse-monitoring, and regional
  behavior is documented for this deployment;
- private-network, managed-identity, access-control, logging/redaction, and
  deployment acceptance are green;
- inherited human cloud entitlements have passed a minimum-necessary review;
  and
- the operational owner explicitly authorizes application enablement.

Creating the resource, passing ARM validation, or setting an enable parameter is
not evidence that these gates are complete.

## Separate dormant embedding lane

The historical `OpenAIEmbedder` and ICD semantic-search helpers call direct
`api.openai.com`. They are development-only, are not part of the pilot runtime,
and are prohibited in staging and production. They must remain disabled until a
separate Azure OpenAI embedding migration supplies a private model deployment,
managed identity, dimension migration/re-index plan, cost review, and the same
PHI acceptance controls.

## Invariants that do not move

- The model extracts typed facts; deterministic Python adjudicates codes.
- The compliance gate may only keep, hold, downgrade, or remove. It never raises
  an E/M level or adds a code as an adjudication action.
- No real PHI flows before every applicable contractual and technical gate.
- No model or deployment change authorizes UI restyling or coding-accuracy
  retuning.
