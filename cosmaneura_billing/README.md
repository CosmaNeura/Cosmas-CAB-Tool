# CosmaNeura — Agentic Billing Layer

A multi-agent pipeline that turns a finished visit (intake text + transcript +
signed note) into accurate, fully-auditable billing codes, with two role-scoped
outputs and a feedback loop back into the rest of the platform. It is built on
top of Homen's **FluencyMed** prototype and consumes that prototype's existing
outputs directly.

## Run it

No API key and no network needed — the demo runs on a deterministic offline
model and an authentic (public-domain) ICD-10 subset:

```bash
pip install pandas pyarrow          # pyarrow only needed for the full code set
python run_demo.py                  # prints the provider view + biller worksheet + feedback
python prove_no_upcode.py           # proves the guardrail cannot upcode
```

## The pilot demo (API + web UI)

Two terminals:

```bash
# 1. the engine API (FastAPI; first request loads the NCCI edits, ~18s)
pip install fastapi uvicorn
uvicorn cosmaneura_billing.api:app --port 8000

# 2. the web UI (provider card, biller worksheet, coder review)
cd frontend && npm install && npm run dev     # -> http://localhost:5173
```

The UI shows a live-engine status pill (NCCI / coverage / CPT), lets you pick
an encounter (the GI-bleed demo plus ACI-Bench visits), edit the documentation,
run the pipeline, and — on the Coder review tab — accept/edit/reject each code
and submit feedback that lands in the JSONL store for `scripts/analyze_feedback.py`.
Coder feedback is persisted to `coder_feedback.jsonl` (override with
`COSMANEURA_FEEDBACK_PATH`).

Production inference (clinical fact extraction via Claude) turns on automatically
when a key is present, and the full AHRQ code set loads from a path you point at:

```bash
ANTHROPIC_API_KEY=sk-...   python run_demo.py
ICD10_CCSR_CSV=/path/to/DXCCSR_v2023-1.csv   python run_demo.py
```

## Official CPT descriptors (AMA developer account)

Out of the box the engine shows CPT/E&M codes with short generic labels,
because the official descriptors are AMA-licensed. If you hold a CPT license
with API access (AMA Intelligent Platform / CPT Developer Program), the engine
auto-fetches the CPT Standard Data File at startup and swaps in the official
short descriptors. Set the four values from your Developer Portal application
(Keys tab) before starting the API:

```bash
export AMA_TOKEN_URL='https://<gateway-host>/oauth2/token'
export AMA_BASE_URL='https://<gateway-host>'
export AMA_CONSUMER_KEY='...'          # Application Consumer Key
export AMA_CONSUMER_SECRET='...'       # Application Consumer Secret

# optional: validate credentials + subscription first (2 API calls, no download)
python scripts/fetch_cpt_zip.py --check

uvicorn cosmaneura_billing.api:app --port 8000
```

`GET /api/health` then reports the loaded descriptor count in `cptDescriptors`
and a human-readable `cptStatus` (which also explains any fetch failure); the
UI status pill shows `CPT ✓` with the detail on hover. The download is cached
under `~/.cache/cosmaneura/cpt`, so the fetch runs once, not on every boot —
which matters because AMA production credentials are capped at ~10 API calls
per day (staging/sample credentials are uncapped; the endpoint defaults were
verified against the production gateway, CPT 2026 Standard Update 4, 10,988
descriptors). Alternatively, point `CPT_DATA_DIR` at an already-extracted
Standard Data File directory, or set `CPT_AUTO_FETCH=0` to disable fetching.

Never commit descriptor files, the downloaded ZIP, or the AMA key/secret —
CPT content stays under your license, outside the repository.

## Second extraction engine (Azure Text Analytics for health)

The pipeline can run a second, independent extraction engine alongside its own
and show coders where the two agree. Azure's Text Analytics for health
contributes clinical NER, assertion detection (negated / hypothetical /
family-member) and UMLS entity links to ICD-10-CM; it never levels E/M and never
makes a billing decision. Agreement is a **triage signal for the reviewer**, not
a licence to bill: a code only one engine found is held for a human, and either
engine may veto.

Off by default. To try it offline, or point it at a real Azure resource:

```bash
COSMANEURA_TA4H=mock  uvicorn cosmaneura_billing.api:app --port 8000   # offline mock

export AZURE_LANGUAGE_ENDPOINT='https://<resource>.cognitiveservices.azure.com'
export AZURE_LANGUAGE_KEY='...'
uvicorn cosmaneura_billing.api:app --port 8000                          # live
```

`GET /api/health` reports `multiEngine` and a human-readable `ta4hStatus`; the UI
shows a "2 extraction engines" pill and an agreement badge per code. Input
logging is opted out on every request (PHI), no new Python dependency is added,
and a TA4H outage degrades to single-engine operation rather than failing an
encounter. See `docs/TA4H_INTEGRATION.md` for the consensus policy and rollout
gates, and `docs/TA4H_MULTI_MODEL_ASSESSMENT.md` for the cost/HIPAA/value
assessment.

## What's in the box

```
cosmaneura_billing/
  schemas.py                 dataclasses; the "evidence-or-it-isn't-billable" rule lives here
  llm.py                     LLMClient protocol + offline MockLLM + AnthropicLLM adapter
  ta4h.py                    Azure Text Analytics for health client + offline MockTA4H
  orchestrator.py            runs the agents and assembles both role views
  knowledge/
    em_ruleset.py            2021+ E/M leveling (MDM grid + time), rule-based and explainable
    icd10_ccsr.py            ICD-10 <-> CCSR lookup on the AHRQ data (+ ported embedding search)
  agents/
    coding.py                proposes E/M + ICD-10, always with a documentation span
    ta4h_coding.py           optional 2nd extractor: UMLS-linked ICD-10 + assertion vetoes
    consensus.py             reconciles the two extractors; only ever more conservative
    documentation_integrity.py   rejects contradicted codes, flags documentation gaps
    compliance_guardrail.py  the hard gate: can only downgrade or remove, never inflate
    denial_risk.py           specificity / linkage / modifier checks
    feedback.py              emits the typed nudges (patient-benefit framed)
  data/icd10_ccsr_subset.csv authentic CMS ICD-10-CM rows for the demo
demo_visit.py                the GI-bleed sample encounter
run_demo.py                  end-to-end demo
prove_no_upcode.py           guardrail invariant test (single- and two-engine)
DESIGN.md                    architecture + the design decisions and why
docs/TA4H_INTEGRATION.md     the multi-engine path: config, policy, rollout
```

See `DESIGN.md` for the architecture, the compliance stance, one design change
from the original brief, and a security item about the prototype repo.
