# Second extraction engine: Azure Text Analytics for health

How the multi-engine path works, how to switch it on, and what it guarantees.
For *why* we integrated it (value, HIPAA posture, cost, alternatives), see
[`TA4H_MULTI_MODEL_ASSESSMENT.md`](TA4H_MULTI_MODEL_ASSESSMENT.md).

## Switching it on

The second engine is **off by default**. With nothing configured the pipeline is
byte-for-byte the single-engine pipeline it has always been — no agreement tags,
no extra audit lines, no behaviour change.

| Setting | Effect |
|---|---|
| *(nothing set)* | Second engine off. Single-engine coding. |
| `COSMANEURA_TA4H=mock` | Offline deterministic mock. Use this to walk the ensemble UI with no Azure resource and no network. |
| `AZURE_LANGUAGE_ENDPOINT` + `AZURE_LANGUAGE_KEY` | Live Azure Text Analytics for health. |
| `COSMANEURA_TA4H=off` | Hard off, even when the Azure variables are set. |
| `AZURE_LANGUAGE_API_VERSION` | Override the API version (defaults to the GA `2023-04-01`). |

```bash
# Provision (one time). Any region where your data may live.
az cognitiveservices account create \
  --name cosmaneura-language --resource-group <rg> \
  --kind TextAnalytics --sku S --location eastus --yes

az cognitiveservices account show \
  --name cosmaneura-language --resource-group <rg> --query properties.endpoint -o tsv
az cognitiveservices account keys list \
  --name cosmaneura-language --resource-group <rg> --query key1 -o tsv
```

```bash
export AZURE_LANGUAGE_ENDPOINT="https://cosmaneura-language.cognitiveservices.azure.com"
export AZURE_LANGUAGE_KEY="<key>"
uvicorn cosmaneura_billing.api:app --reload
# GET /api/health -> "multiEngine": true, plus a "ta4hStatus" line
```

The F0 (free) tier gives 5,000 text records/month — a text record is 1,000
characters, so a typical visit bundle is a handful of records. That is enough to
run the whole shadow-mode validation before any spend. Swap `--sku F0` above to
start there.

**No new Python dependency.** The client is `urllib` against the REST API, the
same stance as `llm.py` and the AMA CPT fetcher, so the core engine keeps its
stdlib-only guarantee. There is no `azure-ai-textanalytics` install step.

## PHI posture

* Azure AI Language is HIPAA-eligible under the Microsoft BAA, which is included
  by default via the Product Terms / DPA — no separate signature.
* **`loggingOptOut: true` is sent on every request.** Azure otherwise retains
  input text for up to 48 hours for troubleshooting. `AzureTA4H(logging_opt_out=False)`
  exists but is a deliberate, documented act — do not set it with PHI in play.
* Processing is stateless; inputs are not retained and not used to train models.
* Prefer a private endpoint / VNet-scoped resource and a managed identity over a
  key once the app is hosted in Azure.

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

Net effect: the ensemble is only ever *more* conservative than the primary
engine alone. `prove_no_upcode.py` asserts all three properties in CI, alongside
the original single-engine guardrail proof.

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
| `cosmaneura_billing/ta4h.py` | REST client (`AzureTA4H`), `MockTA4H`, entity parsing |
| `cosmaneura_billing/agents/ta4h_coding.py` | Secondary coder: entities → ICD-10 candidates + vetoes |
| `cosmaneura_billing/agents/consensus.py` | Merge policy |
| `cosmaneura_billing/orchestrator.py` | `coding=` / `secondary_coding=` / `ta4h=` injection, concurrent run |
| `tests/test_ta4h.py`, `tests/test_consensus.py`, `tests/test_multi_engine_pipeline.py` | Coverage |
| `prove_no_upcode.py` | The invariant proof, extended to the ensemble |
