# CLAUDE.md — Cosmas-CAB-Tool

Before building any feature, read `docs/discovery/00-requirements-synthesis.md`
for consolidated user requirements and the current build/hold split.

## What this repo is

Two generations in one repo:

- **`cosmaneura_billing/`** — the active codebase: the agentic billing layer
  (engine + FastAPI service + React frontends). All new work happens here.
- Repo root `feature*.py`, `*.feather`, `BioGPT-*.py` — the June 2023
  "FluencyMed" prototype, kept for history. Do not build on it.

## Commands

```bash
cd cosmaneura_billing
python3 -m venv .venv
# On Python 3.13+ (matches pyproject):  .venv/bin/pip install -e ".[dev]"
# On an older interpreter (e.g. stock macOS 3.9) the >=3.10 pin blocks the
# editable install — install the deps directly instead:
.venv/bin/pip install pytest numpy fastapi uvicorn httpx pandas pyarrow eval_type_backport
.venv/bin/python -m pytest tests/ -q      # full suite must stay green
.venv/bin/python run_demo.py              # engine demo, offline, no keys
.venv/bin/python prove_no_upcode.py       # guardrail invariant proof
.venv/bin/python run_chase_demo.py        # F9 insurance-chase demo (mock athenaNet)
```

`pyproject.toml` pins `requires-python >=3.10`; the code currently also runs on
3.9 (verified Aug 2026) — prefer 3.10+ when available.

## Hard constraints (user needs never override these)

1. **Anti-inflation invariant:** the compliance gate can only **hold /
   downgrade / remove** codes. No code path raises an E/M level or adds a code
   as an adjudication action. Enforced in deterministic Python, never prompts.
   The fuzz test (`tests/test_guardrail_fuzz.py`, 2,000 seeded cases) stays
   green on every change.
2. **E/M level is an *output* of documented MDM facts, never a target.**
   Under-coding recovery (suggesting a higher level than the provider selected)
   is legitimate **only** with documentation evidence, and every suggestion
   carries evidence spans.
3. **Provider-facing billing suggestions route through the engine pipeline**
   (CodingAgent → DocumentationIntegrityAgent → ComplianceGuardrailAgent →
   DenialRiskAgent). No prompt-only implementation.
4. **PHI boundary:** no real patient data (notes, transcripts, cards, labs)
   touches the system until the BAA and deployment layer (auth, encryption at
   rest, access controls) exist. Until then: **synthetic and de-identified data
   only.** LLM calls fail closed on schema/parse errors.
5. **No patient steering; no revenue-motivated documentation prompts.** UI copy
   uses "capture documented, medically necessary care" — never "increase
   revenue" / up-billing framing.
6. **Direct-contract hard gate:** any pathway that could charge or dun a
   patient must call `cosmaneura_billing.guards.check_direct_contract` first
   and hard-stop on match. Unknown/missing list ⇒ fail closed.
7. **All Athena protocol knowledge lives in
   `cosmaneura_billing/athena_client.py`.** Mock-first; real integration gated
   on sandbox → Marketplace.
8. **Human verification before persistence** everywhere a value enters a
   record: coder review for claims, staff confirmation for patient
   match/package, provider confirmation for extracted lab values. Never
   auto-select, never auto-write. Every patient contact requires explicit
   staff approval before send.

   > **Open tension — owner decision pending (Aug 13, §8 Q3/Q10).** The
   > `auto_continue` outreach mode (`ChaseConfig.outreach_mode`) implements
   > approve-**once-per-patient**: staff enroll a patient, then the system
   > sends cadenced reminders without a fresh approval per message. That is
   > narrower than the sentence above, which reads per-message. It ships
   > **off by default** (`staff_triggered`), its sends are recorded as
   > machine actions (never counted as staff approvals or staff touches),
   > and it auto-escalates back to a human at the reminder cap. If the
   > practice chooses approve-once, amend this constraint explicitly and
   > record who decided; until then the per-message rule stands as written
   > and the mode stays a proposal.
9. **Determinism boundary:** LLMs extract; deterministic Python adjudicates.
   Any new check lands on the deterministic side with tests.
10. **Evidence spans on everything** a user sees suggested — no span, no
    render. Never commit secrets; `.env` stays in `.gitignore`.

## Working rhythm

Each feature ships as a complete, tested unit before the next. Hold-list items
(`docs/discovery/00-requirements-synthesis.md` §6) wait for the Aug 13 biller
follow-up — ship their knobs as configuration, not code.
