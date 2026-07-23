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

Production inference (clinical fact extraction via Claude) turns on automatically
when a key is present, and the full AHRQ code set loads from a path you point at:

```bash
ANTHROPIC_API_KEY=sk-...   python run_demo.py
ICD10_CCSR_CSV=/path/to/DXCCSR_v2023-1.csv   python run_demo.py
```

## What's in the box

```
cosmaneura_billing/
  schemas.py                 dataclasses; the "evidence-or-it-isn't-billable" rule lives here
  llm.py                     LLMClient protocol + offline MockLLM + AnthropicLLM adapter
  orchestrator.py            runs the agents and assembles both role views
  knowledge/
    em_ruleset.py            2021+ E/M leveling (MDM grid + time), rule-based and explainable
    icd10_ccsr.py            ICD-10 <-> CCSR lookup on the AHRQ data (+ ported embedding search)
  agents/
    coding.py                proposes E/M + ICD-10, always with a documentation span
    documentation_integrity.py   rejects contradicted codes, flags documentation gaps
    compliance_guardrail.py  the hard gate: can only downgrade or remove, never inflate
    denial_risk.py           specificity / linkage / modifier checks
    feedback.py              emits the typed nudges (patient-benefit framed)
  data/icd10_ccsr_subset.csv authentic CMS ICD-10-CM rows for the demo
demo_visit.py                the GI-bleed sample encounter
run_demo.py                  end-to-end demo
prove_no_upcode.py           guardrail invariant test
DESIGN.md                    architecture + the design decisions and why
```

See `DESIGN.md` for the architecture, the compliance stance, one design change
from the original brief, and a security item about the prototype repo.
