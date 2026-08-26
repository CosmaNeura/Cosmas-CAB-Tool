# Deployment evidence and source-of-truth map

The GitHub repository is the source of truth for application code, tests,
documentation, and Bicep. Azure is the source of truth only for the state of an
actual deployed resource. A passing commit is not deployed evidence, and live
Azure state is not a substitute for reviewed infrastructure code.

## Canonical map

| Question | Canonical source |
|---|---|
| What code should be built? | GitHub `main`, identified by a full commit SHA |
| What retained image artifact was built? | The `Build CAB Release Artifact` run for that full commit SHA, its GitHub artifact digest, and `release-metadata.json` |
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

## Workflow containment status

The tracked `Build & Deploy` workflow is manual-only. Both Azure-capable jobs
depend on a permissionless authorization job and directly require
`workflow_dispatch`, exact current `main`, `github.actor == JohnClem`,
`github.triggering_actor == JohnClem`, and a durable independent-agent review
record for that commit. The workflow rechecks `main` immediately before image,
migration, and application mutations so a stale rerun fails closed.

At the time this containment was prepared, `AZURE_CLIENT_ID` was not configured
for this repository, so the workflow was inert. This source change does not
create credentials, environments, or a deployment. The existing `pilot`
environment reference does not resolve to a configured GitHub environment:
the repository currently has no environments and therefore no required-reviewer
gate. A future run could implicitly create an unprotected `pilot` environment
unless it is separately configured. Founder promotion is self-authorization,
and the independent-agent record is not a GitHub approval.

This is containment, not the complete target state. Build/publish and deploy
remain coupled in one manual run rather than promotion consuming a separately
retained, already-built and attested digest. Live digest/configuration/schema
reconciliation also remains required before any promotion. See the
[containment flight plan](../flight-plans/2026-08-25-cab-deployment-containment.md).

## Secretless image-artifact rung

The manual `Build CAB Release Artifact` workflow is a smaller, non-deploying
first rung. Before checkout, an inline preflight requires a founder-started run
from `main`; proves that the requested full SHA is both the workflow SHA and the
current GitHub `main` SHA; and validates the durable independent-agent review
comment for that merged commit. It then builds and retains:

- `image.tar.gz`, a loadable archive of the locally built CAB container image;
- `migrations.sha256`, a sorted SHA-256 manifest of every committed SQL
  migration, verified byte-for-byte against `/app/migrations` in that image;
  and
- `release-metadata.json`, deterministically binding the reviewed source SHA,
  review record, image ID, image archive digest, and migration-manifest digest.

The files are uploaded with the immutable v4 artifact service under a name
that includes the source SHA, with a requested 90-day retention period (subject
to the repository's retention policy). The workflow uses only the read-only
GitHub token needed to inspect this private repository and its review record,
and to check out the commit. It has no OIDC permission, cloud credential,
registry operation, database operation, or deployment step.

The existing `Build & Deploy` workflow does not download, verify, or consume
this image archive; it still builds its own container during the separately
gated promotion run. Do not cite the retained archive as commit-to-live
evidence or use its existence to skip live digest, configuration, schema, and
deployment reconciliation.
