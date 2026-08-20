# Second extraction engine: Azure Text Analytics for health

How the multi-engine path works, how to switch it on, and what it guarantees.
For *why* we integrated it (value, HIPAA posture, cost, alternatives), see
[`TA4H_MULTI_MODEL_ASSESSMENT.md`](TA4H_MULTI_MODEL_ASSESSMENT.md).

## Switching it on

The second engine is **off by default**, on the same terms as Azure OpenAI
inference: a deployment opts in, it never drifts on. With nothing configured the
externally visible pipeline is the single-engine pipeline it has always been —
no reviewer agreement fields, no extra audit lines, no behavior change.

| Setting | Effect |
|---|---|
| *(nothing set)* | Second engine off. Single-engine coding. |
| `COSMANEURA_AZURE_LANGUAGE_MOCK=1` | Offline deterministic mock. Development only — `Settings` refuses it in staging/production, so mock output can never be presented as engine output. |
| `COSMANEURA_AZURE_LANGUAGE_ENABLED=1` + `AZURE_LANGUAGE_ENDPOINT` + `AZURE_CLIENT_ID` | Live Azure Text Analytics for health via managed identity. |
| `AZURE_LANGUAGE_API_VERSION` | Override the API version (defaults to the GA `2023-04-01`). |

**Managed identity only.** There is no key-based auth path, matching the Azure
OpenAI stance: `AZURE_LANGUAGE_KEY`, `AZURE_LANGUAGE_AD_TOKEN` and
`AZURE_AI_LANGUAGE_KEY` are *prohibited* — present in the environment with the
engine enabled (or in a deployed environment), `Settings.from_env` refuses to
start rather than quietly honouring a secret. Azure AI Language shares the
`https://cognitiveservices.azure.com/.default` token scope with Azure OpenAI, so
the API's existing user-assigned identity needs only a role assignment on the
Language resource — **no new secret enters the deployment**.

`Settings` validates the configured resource endpoint. Before attaching a
managed-identity bearer token, `AzureLanguageTA4H` also validates every
service-returned `operation-location` URL against that exact resource endpoint
and refuses redirects.

Provision Azure AI Language only through `infra/app.bicep`. The account must
disable public network access and local authentication, use the existing private
network/DNS pattern, and grant only the API user-assigned identity the
account-scoped *Cognitive Services Language Reader* role. Provisioning the
account does not enable extraction; every deployed parameter set keeps
`COSMANEURA_AZURE_LANGUAGE_ENABLED=false`.

The following is future operator reference only. Do not enable this
configuration until Azure OpenAI inference is enabled and the shadow milestone
is approved.

```bash
export COSMANEURA_AZURE_LANGUAGE_ENABLED=1
export AZURE_LANGUAGE_ENDPOINT="https://cosmaneura-language.cognitiveservices.azure.com"
export AZURE_CLIENT_ID="<api-managed-identity-client-id>"
uvicorn cosmaneura_billing.api:app --reload
```

The F0 (free) tier gives 5,000 text records/month — a text record is 1,000
characters, so a typical visit bundle is a handful of records. That covers the
whole shadow-mode validation before any spend. Use `--sku S` for volume.

**No new HTTP dependency.** The transport is `urllib` against the REST API, the
same stance as the AMA CPT fetcher, so no SDK is added; only `azure-identity`
(already required for inference) is needed, declared as the `language` extra.

The engine needs the *primary* engine running to have anything meaningful to
reconcile against. Do not run the shadow study while
`COSMANEURA_AZURE_OPENAI_ENABLED=false`. The offline primary model only covers
the bundled demonstration encounter, so any two-engine agreement measurement
would be meaningless.

## PHI posture

* Azure AI Language is HIPAA-eligible under the Microsoft BAA, which is included
  by default via the Product Terms / DPA — no separate signature.
* **`loggingOptOut: true` is sent on every analysis submission, and is not configurable.**
  Azure otherwise retains input text for up to 48 hours for troubleshooting;
  with chart text in the payload that retention is a HIPAA consideration.
* With input logging opted out, processing is stateless; inputs are not retained
  and are not used to train models.
* Managed identity is the only auth path (see above). The deployed resource must
  use the reviewed private endpoint and VNet-scoped DNS path.

## What each engine contributes

| | Our engine (primary) | TA4H (secondary) |
|---|---|---|
| E/M level | **yes** — 2021 AMA MDM grid | never |
| ICD-10 candidates | LLM concepts → descriptor lookup | UMLS entity links → our active code set |
| Negation / family-history / hypothetical | inferred from LLM output | purpose-built assertion detection |
| Evidence spans | LLM quotes, fuzzy-verified | character offsets, exact by construction |
| Medical necessity, NCCI, denial risk, coverage | **yes** | never |

TA4H is an extractor, not a coder. Every billing *decision* stays with the rule
engine.

## The consensus policy

Three rules, all one-directional, in [`agents/consensus.py`](../cosmaneura_billing/agents/consensus.py):

1. **The E/M ceiling comes from the primary engine's facts alone.** TA4H's facts
   are namespaced `ta4h_*` and never land on the keys the leveling grid reads.
   `merge_facts` then re-computes the ceiling both ways and reverts to
   primary-only facts if they ever differ — so a second engine cannot raise a
   level even if a future edit tried to let it.
2. **A code only one engine found is held for review, never captured.** It
   carries a `consensus_hold` note that the compliance guardrail honours the
   same way it honours a learned reviewer hold. Agreement buys a code the right
   to be *considered* for capture; the guardrail still has to agree.
3. **Either engine may veto.** A finding TA4H reads as negated, hypothetical, or
   about a family member is rejected even if the primary engine proposed it
   confidently.

Two consequences of those rules are easy to get wrong, and `consensus_fuzz` found
both as real bugs on 2026-08-19 — after every hand-written test, including the
end-to-end two-engine pipeline test, had passed them straight through:

4. **The secondary engine's suggestions are filtered to diagnoses** before
   anything downstream sees them. The guardrail's E/M branch deliberately does
   not consult consensus holds, so an out-of-contract E/M code would have been
   promoted on its own evidence.
5. **The secondary engine's evidence is additive only to a code that already has
   a span.** Evidence is the guardrail's necessity gate, so letting the second
   engine supply the *first* span converted a held code into a captured one.

Net effect: the ensemble is only ever *more* conservative than the primary
engine alone. `prove_no_upcode.py` proves it across 2,000 adversarial two-engine
cases in CI (C1 ceiling never moves · C2 captured set never grows · C3 a
single-engine code is never captured · C4 a vetoed code stays rejected · C5 no
E/M from the second engine), re-checking every single-engine guardrail invariant
on the merged output, alongside the original guardrail fuzz.

A hold for a clinical reason ("documented but not addressed in plan") outranks
the engine-split explanation, so coders still get the useful reason.

## Deferred reviewer surface

After the shadow study clears its acceptance criteria, reviewer endpoints may
expose `agreement`, `sourceEngine`, and `multiEngine`, and the coder UI may
render agreement badges. None of these fields or badges is reviewer-visible
during shadow mode. The proposed future meanings are:

The current `COSMANEURA_AZURE_LANGUAGE_ENABLED` path performs active,
conservative consensus and can change held/rejected outcomes and audit text. It
is **not** the shadow seam. Do not enable it on a reviewer-serving deployment
until an out-of-band comparison path exists that cannot change any response.

| Badge | Meaning | Coder action |
|---|---|---|
| **both agree** (green) | Both engines independently found this code | Clear it fast |
| **1 of 2 engines** / **2nd engine only** (amber) | One engine only — held, never auto-billed | Confirm or drop |
| **conflict** (red) | TA4H reads the finding as denied / hypothetical / family-member | Check the note |

Agreed codes carry **two independent evidence spans**, one from each engine.

## Failure behaviour

A TA4H outage, timeout, or malformed response degrades to single-engine
operation and writes one audit line
(`CONSENSUS secondary extraction engine unavailable …`). It never fails an
encounter. Both engines run concurrently, so the added latency is the slower of
the two, not their sum.

Response parsing is defensive: an unrecognised or off-shape entity is dropped
rather than raised, so a service-side schema change degrades to "fewer
entities", never to a broken encounter.

## Rollout

1. **Development verification** — use
   `COSMANEURA_AZURE_LANGUAGE_MOCK=1` only for deterministic local tests. Mock
   results are not shadow evidence.
2. **Shadow** — after Azure OpenAI inference is enabled, run the live F0 resource
   against the frozen 40–60-case gold set. Record agreement rate, each direction
   of single-engine finds, veto precision, and added latency; expose nothing to
   reviewers. This requires a future isolated comparison seam; the current
   active-consensus setting does not satisfy this step.
3. **Surface** — only after shadow acceptance, enable reviewer agreement badges
   and collect adjudications.
4. **Policy** — only after those stages consider whether agreement should
   promote capture; the default remains no.

## Files

| File | Role |
|---|---|
| `cosmaneura_billing/ta4h.py` | REST client (`AzureLanguageTA4H`), `MockTA4H`, entity parsing |
| `cosmaneura_billing/agents/ta4h_coding.py` | Secondary coder: entities → ICD-10 candidates + vetoes |
| `cosmaneura_billing/agents/consensus.py` | Merge policy |
| `cosmaneura_billing/orchestrator.py` | `coding=` / `secondary_coding=` / `ta4h=` injection, concurrent run |
| `tests/test_ta4h.py`, `tests/test_consensus.py`, `tests/test_consensus_fuzz.py`, `tests/test_multi_engine_pipeline.py` | Coverage |
| `cosmaneura_billing/consensus_fuzz.py` | Adversarial two-engine property tests |
| `cosmaneura_billing/settings.py` | `azure_language_*` config + validation |
| `prove_no_upcode.py` | The invariant proof, extended to the ensemble |
