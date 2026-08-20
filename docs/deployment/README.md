# Deployment evidence and source-of-truth map

The GitHub repository is the source of truth for application code, tests,
documentation, and Bicep. Azure is the source of truth only for the state of an
actual deployed resource. A passing commit is not deployed evidence, and live
Azure state is not a substitute for reviewed infrastructure code.

## Canonical map

| Question | Canonical source |
|---|---|
| What code should be built? | GitHub `main`, identified by a full commit SHA |
| What image is live? | OCI manifest digest from the deployment record, reconciled to the live Container Apps revision and its commit-derived discovery tag |
| What Azure state was accepted? | [2026-08-19 deployment and acceptance evidence](2026-08-19-demo-release-candidate-evidence.md) plus a fresh read-only operator audit |
| What was approved to cost? | [Azure OpenAI cost-plan addendum](2026-08-18-azure-openai-cost-plan.md) |
| How is the private stack deployed? | [`infra/README.md`](../../infra/README.md) and the tracked Bicep templates |
| What is the architecture/security boundary? | [`cosmaneura_billing/DESIGN.md`](../../cosmaneura_billing/DESIGN.md) and [`ACCESS_CONTROL.md`](../../cosmaneura_billing/docs/ACCESS_CONTROL.md) |
| What platform sequence controls? | [`HANDOFF_CODEX_2026-08-18.md`](../../HANDOFF_CODEX_2026-08-18.md) |
| What does PR #4 add? | [`HANDOFF_CODEX_2026-08-19.md`](../../HANDOFF_CODEX_2026-08-19.md), additive to the platform handoff |
| What proves anti-inflation? | `cosmaneura_billing/prove_no_upcode.py` and its required CI job |
| What validates Azure OpenAI with safe output? | `python -m cosmaneura_billing.azure_openai_acceptance`, followed by `python -m cosmaneura_billing.log_scanner` |
| What user/discovery claims are authoritative? | [`docs/discovery/README.md`](../discovery/README.md) and its dated source records |

## Current divergence

At the 2026-08-19 release-candidate baseline, `main` was `de2b16f`; the accepted
live image was still based on its first parent, `1c5d7e7`. The release-candidate
branch intentionally moves beyond that baseline. Always resolve current GitHub
`main`, the deployed manifest digest, and fresh Azure state before describing
repository or deployment status.

Real-PHI readiness has not been established.
