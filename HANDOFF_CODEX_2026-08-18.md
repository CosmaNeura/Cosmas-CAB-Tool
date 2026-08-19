# Platform handoff — Azure OpenAI migration

**Date:** 2026-08-18

**Scope:** platform/deployment lane; no coding-policy or UI/UX changes

**Cost plan:**
[`docs/deployment/2026-08-18-azure-openai-cost-plan.md`](docs/deployment/2026-08-18-azure-openai-cost-plan.md)

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
- Infrastructure targets one dedicated private Azure OpenAI account in North
  Central US. Public network access and local authentication are disabled; only
  the API identity receives the account-scoped inference role.
- The pinned deployment contract is `gpt-5.6-sol`, version `2026-07-09`,
  `DataZoneStandard`, capacity 200, no automatic model upgrade. The data-plane
  API version is `2025-04-01-preview`.
- Live catalog discovery and ARM validation accepted that resource shape. This
  proves deployability, not model behavior, compliance coverage, or production
  readiness.
- The adapter selects strict JSON Schema Structured Outputs for locally
  compatible schemas and selects JSON-object mode up front for schemas outside
  the supported strict subset. Both paths still pass deterministic schema
  validation; a provider 4xx is terminal and never triggers a weaker retry.
  OpenAI documents the API distinction in its
  [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs);
  Azure hosting, model deployment, identity, networking, quota, and data-zone
  operations are Microsoft Azure concerns.

## Required order from here

1. Review and approve the linked cost addendum. The exact fixed private-network
   delta is $7.80/month; model tokens and Azure Private Link per-GB processing
   are usage-based and must be repriced from measured synthetic acceptance data.
2. Deploy the foundation resource, model deployment, private endpoint/DNS, and
   API-only role assignment with application inference still disabled.
3. Verify public access/local authentication are disabled, private DNS resolves
   only on the intended network, the API identity can obtain an inference token,
   and migration/worker identities cannot call the account.
4. Run synthetic/de-identified data-plane acceptance for both strict-schema and
   JSON-object fallback paths. Cover refusal, content filter, truncation, the
   8,000-token output ceiling, retries, latency, and provider token accounting.
5. Verify P5 logs and metrics contain only the closed allowlist: provider/model,
   status, attempt, latency, and numeric token counters. Re-run the PHI canary
   scanner against deployed logs.
6. Recalculate the operating forecast from measured p50/p95 input, cached-input,
   and output tokens and expected encounter volume.
7. Keep inference disabled and continue with synthetic/de-identified data until
   the real-PHI gate below is documented and approved.
8. Run the full local verification suite and GitHub Actions before merging the
   branch to `main`; never fix an anti-inflation failure by weakening its test.

## Real-PHI gate

Real chart text remains prohibited until all of the following are true:

- the executed Microsoft agreement/BAA is verified to cover Azure OpenAI and
  the selected US data zone;
- the service's data-processing, retention, abuse-monitoring, and regional
  behavior is documented for this deployment;
- private-network, managed-identity, access-control, logging/redaction, and
  deployment acceptance are green; and
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
