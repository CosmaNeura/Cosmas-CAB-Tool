# AGENTS.md — Cosmas CAB engineering guardrails (v0.2.1)

> The canonical baseline lives in `cosmaneura-architecture/deploy-kit/`.
> `CLAUDE.md` and the billing safety invariants remain stricter and authoritative.

## Operating objective

Reduce deployment risk created by rapid code growth. Make changes small,
independently deployable, observable, and reversible. Documentation supports
that work; it does not replace decomposition or deployed-state reconciliation.

## Repository facts and verified commands

- This is the private Computer-Assisted Billing pilot. The active engine is
  `cosmaneura_billing/`; the root FluencyMed prototype is historical.
- Default branch: `main`. CI validates Bicep, Python 3.10/3.12, guardrails,
  demos, frontend, and a disposable container/PostgreSQL topology.
- Install: `cd cosmaneura_billing && python -m pip install -e ".[dev]"`.
- Full Python checks: `cd cosmaneura_billing && python -m pytest -v && python -m cosmaneura_billing.log_scanner --self-test && python prove_no_upcode.py && python run_demo.py && python demo_chase.py`.
- Focused test: `cd cosmaneura_billing && python -m pytest -v <path>`.
- Frontend checks: `cd cosmaneura_billing/frontend && npm ci && npm test && npm run build`.
- Lint/typecheck: N/A; no general linter/type checker is configured. The log
  scanner self-test is a PHI-boundary check, not a linter.
- Local API: `cd cosmaneura_billing && uvicorn cosmaneura_billing.api:app --port 8000`.
- Local frontend: `cd cosmaneura_billing/frontend && npm install && npm run dev`.
- Migrations use ordered SQL and the locked runner described in
  `cosmaneura_billing/migrations/README.md`; there is no migration generator.

## Risk classes (declare one on every PR)

- **Tier 0 — routine:** localized tests, docs, copy/style, or
  behavior-preserving refactors with no billing-decision, deployment, schema,
  auth, outreach, PHI, security-boundary, or external-contract impact.
- **Tier 1 — deployment-sensitive and reversible:** dependencies, CI/build,
  runtime configuration, additive API/contract/schema, or reversible
  user-visible behavior needing a deliberate recovery move.
- **Tier 2 — irreversible or high blast radius:** billing adjudication or
  evidence behavior; auth/session; patient outreach; patient/PHI schema or data;
  destructive/large-scale data work; security boundaries; production
  identity/network/promotion; breaking contracts; or broad infrastructure.

Tier 2 minimum paths include `cosmaneura_billing/cosmaneura_billing/auth.py`,
`agents/consensus.py`, `evidence.py`, the compliance gate, patient-facing
`chase` paths, patient-data migrations, and production `infra/` or promotion
machinery. Urgency never lowers a tier. Split mixed concerns when they can be
deployed independently.

## Conditional controls and review

- Use a flag/release control only for reversible user-visible behavior. It is
  not rollback for schema, data, infrastructure, security, or refactoring.
- A flight plan is required for Tier 2, schema/data sequences, cross-repo
  rollouts, or changes spanning multiple deploys/cohorts.
- A migration rung and rehearsal apply only to schema/data work.
- Every code-changing PR receives independent agent review after its final
  substantive push. The reviewer cannot be the implementer. Durable PR
  evidence identifies the reviewed commit, findings, and disposition. A later
  substantive push invalidates it. Agent review is not GitHub approval.

## Approval and promotion boundaries

- Preserve current GitHub approval settings. Current repository guidance
  records strict protected checks/admin enforcement but zero required approving
  reviews; this document does not change that process.
- Target production policy is founder-only manual promotion from a merged
  `main` commit and an already-built digest. The run is explicit
  self-authorization, not independent review.
- There is no tracked build/promotion workflow, and the running pilot image is
  not traceable to a commit. Those are deployment blockers, not permissions to
  repeat a manual push. Do not claim the target control exists.
- Reconcile live digest, configuration, schema, and deployed-only behavior
  before promotion; choose keep, assimilate, or replace. Never blindly deploy
  `main` over drift.
- Unless explicitly broader, security-risk acceptance permits dark,
  synthetic-only non-PHI development and blocks production/PHI promotion.

## Permission boundaries

**Allowed without a separate production decision**

- Create small/stacked PRs, tests, flight plans, and reversible refactors.
- Add a dormant additive structure through an approved expand rung.
- Inspect live state read-only and prepare reconciliation options.

**Ask first**

- Change billing adjudication, auth, outreach, patient schemas, security
  boundaries, CI/deployment, environment configuration, or production data.
- Add an external dependency/service, broaden a cohort, or activate messaging,
  inference, external ingress, Athena, or another dark capability.
- Execute production promotion, migration, backfill, or destructive cleanup.

**Never**

- Violate any hard rule in `CLAUDE.md`, including anti-inflation,
  evidence-before-render, deterministic adjudication, human verification,
  direct-contract gating, and the PHI boundary.
- Enable real PHI until every deployment, service agreement/BAA, and
  data-handling gate is complete. Synthetic or properly de-identified data only.
- Put PHI/secrets in source, tests, fixtures, logs, PRs, or agent prompts;
  force-push `main`; deploy an unmerged branch; or treat agent output as a
  GitHub approval.
- Drop an expand structure as immediate rollback; leave it dormant.
- Archive/delete repositories or production resources under this doctrine.

## Architecture and workflow

- LLMs extract facts; deterministic Python adjudicates. New checks land on the
  deterministic side with tests. `prove_no_upcode.py` and
  `tests/test_guardrail_fuzz.py` remain green.
- Every rendered suggestion has verified evidence and traverses the full
  billing pipeline. Coverage may order but never add; users verify before
  persistence.
- Keep patient messaging and scheduling latches off unless a separately
  reviewed activation satisfies every gate. The API remains transport-inert.
- Auth/vendor parsing stays in `cosmaneura_billing/cosmaneura_billing/auth.py`;
  domain code consumes the normalized principal.
- Build cross-repo work receiver-first with backward-compatible contracts.
- Keep each PR to one deployable outcome, reviewable in roughly ten minutes.
- Obey the repository-specific blocking change-budget check once installed.
  Its numeric threshold belongs in a small, separately reviewed policy
  workflow; an oversized PR cannot waive its own limit by label or checkbox.
  No such tracked check exists today, so automated enforcement remains an
  adoption gap and reviewers must require decomposition meanwhile.
- Every PR records tier, tests, exact recovery, independent agent review, and
  only controls that actually apply. Docs-only changes must not deploy.
