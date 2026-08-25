# AGENTS.md — Cosmaneura engineering guardrails

> Canonical copy lives in `cosmaneura-architecture/deploy-kit/`. Sync on Contract Friday.
> Keep this file under ~150 lines: only non-inferable rules belong here.

## Stack

GitHub + GitHub Actions (no Azure DevOps) · Azure Container Apps · ACR ·
Azure Database for PostgreSQL Flexible Server · Clerk + Microsoft Entra ID
(sign-in; varies by service). Core deploys from `master`. Key Vault + App
Insights are target-state, not yet live in core prod.
**This repo: Computer-Assisted Billing. ACA **pilot**, private. Engine lives in `cosmaneura_billing/`.**

## Commands

- Install: `cd cosmaneura_billing && python -m pip install -e ".[dev]"`
- Test (all): `python -m pytest -v && python prove_no_upcode.py`
- Test (single file): `python -m pytest -v <path>`
- Lint + typecheck: `python -m cosmaneura_billing.log_scanner --self-test`
- Local dev: `uvicorn cosmaneura_billing.api:app --port 8000  # frontend: cd frontend && npm install && npm run dev`
- DB migration create: **not Alembic** — this repo has its own ordered-SQL runner. Add a new ordered SQL file per its convention.
- Deploy: **there is no deploy workflow in this repo yet.** The running pilot image was pushed by hand. Until that is fixed, do not assume merging deploys anything.

## Risk tiers (declare one on every PR)

- **Tier 0** — copy, styling, docs, config text, additive UI behind an existing flag. CI green → merge.
- **Tier 1** — new feature logic, new endpoints, additive-only schema changes (new tables/columns, nullable). Requires: feature flag + flight plan reference.
- **Tier 2** — anything touching `cosmaneura_billing/cosmaneura_billing/auth.py`, the `chase` module (patient outreach), `agents/consensus.py`, `evidence.py`, the compliance gate, and any patient-data schema, or any destructive migration. Requires: flight plan, migration ladder compliance (docs/MIGRATION_PLAYBOOK.md), rehearsal run, founder sign-off.

Tier is set by the *paths a change touches*, never by urgency.

## Permission boundaries

**Allowed freely**
- Creating branches, PRs, and stacked PRs; small refactors inside a feature's own module
- Adding feature flags; additive migrations (new nullable columns, new tables, new indexes)
- Updating docs, tests, and flight plans

**Ask first**
- Any change under the Tier 2 paths above — even one line
- Adding a new external dependency or third-party service (BAA implications)
- Changing CI workflows, environment config, or anything under `.github/`
- Backfill scripts that write to production data

**Never**
- Destructive migrations (DROP/RENAME column or table, type changes) outside a labeled `contract` PR whose hold period has elapsed
- Importing auth/billing internals from feature code — go through the public interface of the module only
- Turning a feature flag on for all users in code; rollout happens through the flag system, by cohort
- Touching production directly (`az` commands against prod resources), force-pushing the default branch, committing secrets, or putting real PHI in tests, fixtures, seeds, staging, or logs

## Architecture rules (the non-inferable ones)

- Identity: only this repo's designated auth adapter module(s) — `cosmaneura_billing/cosmaneura_billing/auth.py` (Microsoft Entra ID) — may import vendor SDKs (Clerk, Entra/MSAL) or parse provider claims. All domain code consumes a normalized principal from the adapter — never raw tokens, never provider-specific branches. One adapter per service; there is no cross-service facade.
- Reversible user-visible behavior ships behind a flag/cohort control from its first commit (name: `ff_<feature>_<yyyymm>`, owner + expiry in the flight plan). Refactors, security fixes, infra, and schema work are NOT flagged — small PRs + migration ladder + revert.
- All schema changes follow docs/MIGRATION_PLAYBOOK.md. Additive first; destructive only as a final `contract` step. A "rung" = one independently deployable, observable, backward-compatible transition (may span coordinated cross-repo PRs; receiver/auth deploys before sender activation).
- Users carry an `auth_version` (or equivalent) marker during auth migrations; both old and new paths must work until the contract step.

## Workflow

1. New feature? FIRST draft `docs/flight-plans/<feature>.md` from the template. Do not write feature code before the flight plan exists.
2. Build as a stack of small PRs (interface/seam → logic → UI → flag wiring). One session per layer. Keep diffs reviewable by one human.
3. Every PR fills in the PR template completely, including the tier and the change record.
4. If a PR changes deploy-relevant behavior, update this file in the same PR.

## Repo notes

- **The four hard rules in `CLAUDE.md` outrank this file.** Anti-inflation invariant,
  E/M level as output never target, no-evidence-no-render, and the determinism
  boundary (LLMs extract facts, Python adjudicates). `prove_no_upcode.py` and
  `tests/test_guardrail_fuzz.py` stay green on every change. A PR that violates one
  is wrong even if CI passes and a customer asked for it.
- Patient outreach (`chase`) is the highest-consequence surface in the product.
  Live gates today: `COSMANEURA_PATIENT_MESSAGING_ENABLED=false`,
  `COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED=false`, chase worker on a
  manual trigger with no cron. Never change these from code or `az`.
- Whether this repo falls under `ARCHITECTURE.md` §9 outreach governance or is a
  separate product with its own standard is **an open question with the founder**.
  Until it is answered, treat §9 as binding.
- Repo root also holds the 2023 FluencyMed prototype (`feature1`–`feature5`,
  `BioGPT-*`) and a stale `README.md` describing that prototype. Historical; not
  part of the tool. The engine is `cosmaneura_billing/`.
- Branch protection **is** on for `main` (strict checks, admins included), but
  required approving reviews is **0** — CI merges without a human.
- The running pilot image cannot be traced to a commit. Fixing that is the first
  deployment task for this repo.
