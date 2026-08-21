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
| `COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED=1` + `AZURE_LANGUAGE_ENDPOINT` + `AZURE_CLIENT_ID` | Live observation-only TA4H. This milestone requires primary Azure OpenAI **and** `COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY=1`; TA4H output can enter only aggregate health metrics. |
| `COSMANEURA_AZURE_LANGUAGE_ENABLED=1` + `AZURE_LANGUAGE_ENDPOINT` + `AZURE_CLIENT_ID` | Live **active-consensus** TA4H. This separate gate may change holds, rejections, rationale, and audit text. |
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
account-scoped *Cognitive Services Language Reader* role. Set
`provisionAzureLanguage=true` to create that dormant F0 account while keeping
`enableAzureLanguageExtraction=false` and `enableAzureLanguageShadow=false`.
Provisioning the account does not enable extraction; every tracked parameter
set keeps both runtime flags false. The template supplies
`COSMANEURA_AZURE_LANGUAGE_ENABLED=false` and
`COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED=false` to the API and does not inject
the endpoint or API version until one mutually exclusive runtime flag is true.

The observation-only lane is selected through the shadow flag. The application
and Bicep template fail closed unless primary Azure OpenAI is enabled, the
reserved synthetic-only inference policy is enabled, the Language account is
provisioned, and active consensus is off.

```bash
export COSMANEURA_AZURE_OPENAI_ENABLED=1
export COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY=1
export COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED=1
export COSMANEURA_AZURE_LANGUAGE_ENABLED=0
export AZURE_LANGUAGE_ENDPOINT="https://cosmaneura-language.cognitiveservices.azure.com"
export AZURE_CLIENT_ID="<api-managed-identity-client-id>"
uvicorn cosmaneura_billing.api:app --reload
```

The F0 (free) tier gives 5,000 text records/month — a text record is 1,000
characters, so a typical visit bundle is a handful of records. That should
cover the study's Language service usage, but the required private endpoint and
DNS add $7.80/month in standing cost, plus separately metered Private Link data
processing. Use the S tier for volume only after separate pricing approval.

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

`COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED` is the isolated seam: it returns the
primary result byte-for-byte, including dispositions, rationales, evidence,
audit, provider card, worksheet, and serialized API output. TA4H success or
failure changes only the bounded aggregate `ta4h` object on `/api/health`.
`COSMANEURA_AZURE_LANGUAGE_ENABLED` remains the separate active-consensus path;
it is **not** a synonym for shadow mode.

| Badge | Meaning | Coder action |
|---|---|---|
| **both agree** (green) | Both engines independently found this code | Clear it fast |
| **1 of 2 engines** / **2nd engine only** (amber) | One engine only — held, never auto-billed | Confirm or drop |
| **conflict** (red) | TA4H reads the finding as denied / hypothetical / family-member | Check the note |

Agreed codes carry **two independent evidence spans**, one from each engine.

## Failure behaviour

In shadow mode a TA4H outage, timeout, or malformed response returns the exact
primary-only result and increments only aggregate failure/latency counters. It
adds no response or audit line and never includes exception text in metrics.
Active-consensus mode retains its prior single-engine fallback audit line
(`CONSENSUS secondary extraction engine unavailable …`) and concurrent engine
execution. Shadow work starts on a bounded daemon worker after the primary
extraction. Its queued `VisitBundle` is a recursive detached copy, including
mutable code lists, so a buggy secondary cannot race or mutate any value the
primary pipeline still reads. A full queue drops the observation and a stuck
TA4H call cannot delay reviewer output or process shutdown. Each short-lived
task frame clears service results and the daemon deletes its task reference
before publishing idle, so the last chart/result is not retained between runs.
Shutdown latches before draining and a worker re-checks that latch immediately
after dequeue, preventing a queued task from starting a new service call.

Response parsing is defensive: an unrecognised or off-shape entity is dropped
rather than raised, so a service-side schema change degrades to "fewer
entities", never to a broken encounter.

## Rollout

1. **Development verification** — use
   `COSMANEURA_AZURE_LANGUAGE_MOCK=1` only for deterministic local tests. Mock
   results are not shadow evidence.
2. **Synthetic deployment acceptance** — with ingress closed, run
   `python -m cosmaneura_billing.ta4h_shadow_acceptance`. It accepts no chart
   input and checks private DNS, forbidden credentials, managed-identity
   routing, redirect refusal, synthetic analysis, output non-influence, and the
   closed observability schema.
3. **Shadow study** — after Azure OpenAI inference is enabled, run the live F0
   resource against a frozen 40–60-case coder-labelled gold set. The runtime
   records aggregate agreement directions, veto counts, entity counts,
   failures, and added latency; it deliberately does not retain case-level
   codes or adjudications. **The coder-labelled gold set remains external and
   pending**, so this step is not yet complete and aggregate counters are not
   accuracy evidence.
4. **Surface** — only after shadow acceptance, enable reviewer agreement badges
   and collect adjudications.
5. **Policy** — only after those stages consider whether agreement should
   promote capture; the default remains no.

## Files

| File | Role |
|---|---|
| `cosmaneura_billing/ta4h.py` | REST client (`AzureLanguageTA4H`), `MockTA4H`, entity parsing |
| `cosmaneura_billing/agents/ta4h_coding.py` | Secondary coder: entities → ICD-10 candidates + vetoes |
| `cosmaneura_billing/agents/consensus.py` | Merge policy |
| `cosmaneura_billing/orchestrator.py` | Active-consensus concurrency plus non-blocking shadow dispatch |
| `cosmaneura_billing/ta4h_shadow.py` | Closed aggregate metrics; retains no identifiers, text, codes, evidence, responses, or exceptions |
| `cosmaneura_billing/ta4h_shadow_acceptance.py` | Fixed synthetic deployment acceptance harness |
| `tests/test_ta4h.py`, `tests/test_ta4h_shadow.py`, `tests/test_ta4h_shadow_acceptance.py`, `tests/test_consensus.py`, `tests/test_consensus_fuzz.py`, `tests/test_multi_engine_pipeline.py` | Coverage |
| `cosmaneura_billing/consensus_fuzz.py` | Adversarial two-engine property tests |
| `cosmaneura_billing/settings.py` | `azure_language_*` config + validation |
| `prove_no_upcode.py` | The invariant proof, extended to the ensemble |
