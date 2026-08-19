# Second extraction engine: Azure Text Analytics for health

How the multi-engine path works, how to switch it on, and what it guarantees.
For *why* we integrated it (value, HIPAA posture, cost, alternatives), see
[`TA4H_MULTI_MODEL_ASSESSMENT.md`](TA4H_MULTI_MODEL_ASSESSMENT.md).

## Switching it on

The second engine is **off by default**, on the same terms as Azure OpenAI
inference: a deployment opts in, it never drifts on. With nothing configured the
pipeline is byte-for-byte the single-engine pipeline it has always been — no
agreement tags, no extra audit lines, no behaviour change.

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

`Settings` also validates that the endpoint is a resource-scoped Azure hostname,
and `AzureLanguageTA4H` re-validates its own effective values at construction, so
a caller cannot swap a reviewed endpoint for another host and have a
managed-identity bearer token sent to it.

```bash
# Provision the Language resource and grant the API identity read access.
az cognitiveservices account create \
  --name cosmaneura-language --resource-group <rg> \
  --kind TextAnalytics --sku F0 --location eastus --yes

az role assignment create \
  --assignee <api-managed-identity-principal-id> \
  --role "Cognitive Services Language Reader" \
  --scope $(az cognitiveservices account show --name cosmaneura-language \
              --resource-group <rg> --query id -o tsv)
```

```bash
export COSMANEURA_AZURE_LANGUAGE_ENABLED=1
export AZURE_LANGUAGE_ENDPOINT="https://cosmaneura-language.cognitiveservices.azure.com"
export AZURE_CLIENT_ID="<api-managed-identity-client-id>"
uvicorn cosmaneura_billing.api:app --reload
# GET /api/health -> "multiEngine": true, "extractionEngines": 2, plus "ta4hStatus"
```

The F0 (free) tier gives 5,000 text records/month — a text record is 1,000
characters, so a typical visit bundle is a handful of records. That covers the
whole shadow-mode validation before any spend. Use `--sku S` for volume.

**No new HTTP dependency.** The transport is `urllib` against the REST API, the
same stance as the AMA CPT fetcher, so no SDK is added; only `azure-identity`
(already required for inference) is needed, declared as the `language` extra.

The engine needs the *primary* engine running to have anything to reconcile
against. With `COSMANEURA_AZURE_OPENAI_ENABLED` false the primary path is the
offline mock, which only codes the bundled demo encounter — so a two-engine
demo is only meaningful on that encounter until inference is enabled.

## PHI posture

* Azure AI Language is HIPAA-eligible under the Microsoft BAA, which is included
  by default via the Product Terms / DPA — no separate signature.
* **`loggingOptOut: true` is sent on every request, and is not configurable.**
  Azure otherwise retains input text for up to 48 hours for troubleshooting;
  with chart text in the payload that retention is a HIPAA consideration.
* Processing is stateless; inputs are not retained and not used to train models.
* Managed identity is the only auth path (see above). Prefer a private endpoint
  / VNet-scoped Language resource as well.

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

## What the reviewer sees

`/api/encounters/{id}/coder-view` and `/api/billing` gain per-code
`agreement` and `sourceEngine`, and `multiEngine` at the top level. The coder UI
renders an agreement badge per row:

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

1. **Shadow** — run with `COSMANEURA_TA4H=mock` in dev, then a live F0 resource
   against the frozen gold set. Measure agreement rate, TA4H-only finds, and
   veto precision per `VALIDATION_PROTOCOL.md` §11/§13. Expect the review queue
   to *grow* at first: that is the second engine finding things, which is the
   point.
2. **Surface** — turn on the agreement badges for coders. Collect their
   adjudications through the existing feedback loop.
3. **Gate** — only once the shadow metrics clear the protocol, consider letting
   agreement influence capture. Nothing in the current policy does this: today
   agreement never captures a code on its own.

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
