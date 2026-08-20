# Platform handoff — second extraction engine (Azure Text Analytics for health)

**Date:** 2026-08-19

**Scope:** extraction/consensus lane. No coding-policy, E/M-rule,
discovery-contract, or currently enabled reviewer-facing UI change. Agreement
surfacing remains deferred to the inference-on milestone. No infrastructure
change is included in this pull request.

**Pull request:** [#4](https://github.com/CosmaNeura/Cosmas-CAB-Tool/pull/4) —
`claude/billing-analysis-multi-model-3k99ke`, merged to `main` as `de2b16f`.
Its first parent is the previously accepted/live image source `1c5d7e7`; current
`main` is not yet deployed. See the dated
[release-candidate evidence](docs/deployment/2026-08-19-demo-release-candidate-evidence.md).

**Design docs:**
[`cosmaneura_billing/docs/TA4H_INTEGRATION.md`](cosmaneura_billing/docs/TA4H_INTEGRATION.md) (config, policy, rollout gates)
· [`cosmaneura_billing/docs/TA4H_MULTI_MODEL_ASSESSMENT.md`](cosmaneura_billing/docs/TA4H_MULTI_MODEL_ASSESSMENT.md) (cost, HIPAA, value)

This document **adds** the extraction-lane position to `HANDOFF_CODEX_2026-08-18.md`.
It does not supersede that document's platform sequencing, and it does not supersede
`CLAUDE.md`, the anti-inflation invariant, evidence requirements, or the discovery
contracts. Where this document and the 2026-08-18 platform order appear to compete,
the platform order wins: the release-candidate slice ships first.

## Current position

- A second extraction engine exists in the tree and is **off by default**. With
  nothing configured the externally visible pipeline remains the single-engine
  pipeline — no reviewer agreement fields, no extra audit lines, and no
  behavior change. Internal defaulted provenance fields are inert.
- The engine is Azure AI Language *Text Analytics for health* (TA4H). It performs
  clinical NER, assertion detection (negated / hypothetical / family-member) and
  entity linking to UMLS vocabularies including ICD-10-CM. It returns character
  offsets, so its evidence spans verify exactly against the documentation rather
  than fuzzily.
- TA4H **never** levels E/M, proposes CPT, or reasons about medical necessity.
  Every billing decision remains with the deterministic rule engine. The
  consensus layer enforces this structurally, not by convention.
- Enablement follows the inference pattern exactly: `azure_language_enabled`,
  `azure_language_endpoint`, `azure_language_api_version`, `azure_language_mock`
  on `Settings`, validated together, default off.
- Authentication is the API user-assigned managed identity against the
  `https://cognitiveservices.azure.com/.default` scope — **the same scope Azure
  OpenAI already uses**. `AZURE_LANGUAGE_KEY`, `AZURE_LANGUAGE_AD_TOKEN` and
  `AZURE_AI_LANGUAGE_KEY` are prohibited; `Settings.from_env` refuses to start if
  one is present with the engine enabled or in a deployed environment. **No new
  secret enters the deployment.**
- `loggingOptOut` is always true and is not configurable. Azure otherwise retains
  input text for 48 hours for troubleshooting.
- Transport is stdlib `urllib`. No HTTP or SDK dependency was added to the core
  engine; only `azure-identity`, already required for inference, declared as a
  new `language` extra.
- The offline `MockTA4H` is refused by `Settings` in staging and production, so
  mock output can never be presented as engine output.
- **Nothing is provisioned and nothing is enabled.** The application Bicep now
  defines dormant private F0 provisioning and API-only RBAC, but both provisioning
  and extraction default false and require separate cost/deployment approval.

## What the consensus layer guarantees

Five one-directional rules in `cosmaneura_billing/agents/consensus.py`. The
ensemble can only ever be **more** conservative than the primary engine alone.

1. The E/M ceiling comes from the primary engine's facts alone. TA4H facts are
   namespaced `ta4h_*` and never reach a leveling key; `merge_facts` recomputes
   the ceiling both ways and reverts to primary-only facts if they ever differ.
2. A code only one engine found is held for review, never captured, via a
   `consensus_hold` note the guardrail honours like a learned reviewer hold.
3. Either engine may veto. A finding TA4H reads as negated, hypothetical, or
   about a family member is rejected even if the primary engine proposed it
   confidently.
4. Secondary suggestions are filtered to diagnoses before anything downstream
   sees them.
5. Secondary evidence is additive only to a code that already has a span.

Rules 4 and 5 are **regression fixes, not preferences.** `consensus_fuzz` found
both as real defects on 2026-08-19, after every hand-written test — including the
end-to-end two-engine pipeline test — passed them straight through:

- a non-diagnosis code from the secondary engine reached the guardrail carrying a
  `consensus_hold` note, but the guardrail's E/M branch deliberately does not
  consult those notes, so it was promoted to CAPTURED on its own evidence;
- merging secondary evidence into a primary code that had none turned a HELD code
  into a CAPTURED one, because evidence is the guardrail's necessity gate.

Do not relax either rule. If a future change needs secondary output to influence
capture, the change is to the guardrail's gates with its own proof, not to these.

## Invariants that do not move

Everything in `HANDOFF_CODEX_2026-08-18.md` §"Invariants that do not move" holds
unchanged. The extraction lane adds these:

- `prove_no_upcode.py` runs **two** harnesses and both must stay green:
  `run_fuzz` (2,000 single-engine cases, I1–I8) and `run_consensus_fuzz` (2,000
  two-engine cases, C1–C5 plus I1–I8 re-checked on merged output).
- **C2 is the load-bearing one:** the CAPTURED set with two engines is always a
  subset of what the primary engine alone would capture. Adding an engine cannot
  add a billable code. If a change makes C2 fail, the change is wrong.
- No key-based authentication path may be added for Azure AI Language.
- `loggingOptOut` must not become configurable.
- `MockTA4H` must remain refused in staging and production.
- The second engine must remain default-off.

## Required order from here

The 2026-08-18 platform order is unchanged and comes first. Fold two items into
the existing "Demo Release Candidate & Canonical Evidence" slice; defer the rest.

**Inside the current slice (small, additive):**

1. Add the Language resource and the role assignment to `infra/app.bicep`, with
   public network access disabled and local authentication disabled, matching the
   Azure OpenAI account shape. Grant only the API user-assigned identity a
   *Cognitive Services Language Reader* role on the account. Keep
   `COSMANEURA_AZURE_LANGUAGE_ENABLED` false in every deployed parameter file.
   Provisioning the resource is not enabling it.
2. Add `run_consensus_fuzz` to the required-CI set alongside the existing
   guardrail proof. `prove_no_upcode.py` already invokes it, so this is a matter
   of keeping that step required under the new branch protection.

**Deferred to the inference-on milestone, in this order:**

3. After separate approval of the $7.80/month private endpoint and DNS standing
   cost, provision the Language resource on F0 (5,000 text records/month, a text
   record being 1,000 characters). F0 should cover the study's service usage;
   Private Link data processing remains separately metered.
4. Implement an isolated shadow seam before enabling the engine on any
   reviewer-serving process. The current `azure_language_enabled` path performs
   active conservative consensus; hiding badges alone is not shadow mode. The
   future seam must log comparison results while leaving reviewer responses,
   dispositions, rationales, and audit output unchanged. Then measure, against
   the 40–60 golden cases from slice item 7:
   agreement rate, codes found only by TA4H, codes found only by the primary
   engine, veto precision against the known-correct answer, and added latency.
   The recorded disagreement set may grow; the live review queue remains
   unchanged until the surface milestone.
5. Surface agreement badges to coders, and collect their adjudications through
   the existing feedback loop.
6. Only after 4 and 5 clear, consider whether agreement should promote capture.
   Nothing in the current policy does this, and the default answer is no.

**Do not** run step 4 before inference is enabled, and do not use the current
active-consensus switch as a substitute for the isolated shadow seam. Consensus
requires two engines;
with the primary path on the offline model the second engine has only the bundled
demo encounter to reconcile against, so the measurement would be meaningless.

## Real-PHI gate

Unchanged and still blocked. This work adds no real-PHI implication and removes
no gate. Two clarifications for the entitlement and minimum-necessary review:

- TA4H introduces **no new trust boundary**: same tenant, same Microsoft BAA
  (in-scope via the Product Terms / DPA), same managed identity, same token
  scope, no new subprocessor, no new secret. It is a role assignment on an
  additional account in the existing subscription.
- Azure AI Language is HIPAA-eligible under that BAA, processing is stateless,
  and inputs are not used to train models. BAA eligibility is not compliance; our
  `VALIDATION_PROTOCOL.md` §5 obligations are unchanged.
- The on-premises/VNet container deployment option exists if the review ever
  requires that chart text not leave the perimeter. Not implemented.

## Corrections to earlier documentation

Two statements in `TA4H_MULTI_MODEL_ASSESSMENT.md` were written before the Azure
OpenAI migration landed and are corrected in place, with the original claim struck
rather than silently rewritten:

- the pipeline no longer calls a public model API;
- the Azure footprint now exists.

Both make the case for TA4H stronger, not weaker. No conclusion changed.

## GitHub control-plane follow-up

The earlier finding that `main` was unprotected is resolved as of 2026-08-19.
The live repository settings now enforce seven strict required contexts:
`infra`, both Python `test` jobs, `frontend`, `container`, `Analyze (python)`,
and `Analyze (javascript-typescript)`. Administrators are included, conversation
resolution is required, and force-pushes/deletions are disabled.
Dependabot security updates, code security, secret scanning, push protection,
validity checks, and non-provider patterns are enabled; default CodeQL setup is
configured for Python and JavaScript/TypeScript.
Post-merge CI run `32317475914` and CodeQL setup run `32317672236` passed.

`prove_no_upcode.py` remains the `Guardrail invariant proof` step inside both
required Python test contexts and invokes both the single-engine and consensus
harnesses. Do not remove or bypass that step while leaving the job name green.

## Files

| File | Role |
|---|---|
| `cosmaneura_billing/cosmaneura_billing/ta4h.py` | REST client (`AzureLanguageTA4H`), `MockTA4H`, entity parsing |
| `cosmaneura_billing/cosmaneura_billing/agents/ta4h_coding.py` | Secondary coder: entities → ICD-10 candidates + assertion vetoes |
| `cosmaneura_billing/cosmaneura_billing/agents/consensus.py` | The merge policy |
| `cosmaneura_billing/cosmaneura_billing/consensus_fuzz.py` | Adversarial two-engine property tests |
| `cosmaneura_billing/cosmaneura_billing/settings.py` | `azure_language_*` config, endpoint validation, prohibited credentials |
| `cosmaneura_billing/cosmaneura_billing/orchestrator.py` | `coding=` / `secondary_coding=` / `ta4h=` seams; concurrent extraction |
| `cosmaneura_billing/prove_no_upcode.py` | Both invariant harnesses |
| `cosmaneura_billing/tests/test_ta4h.py` · `test_consensus.py` · `test_consensus_fuzz.py` · `test_multi_engine_pipeline.py` | Coverage |
