# CosmaNeura CAB Tool — agent guardrails

Computer-assisted billing for independent Catholic practices. The engine turns a
finished visit (intake + transcript + signed note) into evidence-linked billing
codes with two role-scoped views and a coder feedback loop.

**Before building any feature, read `docs/discovery/00-requirements-synthesis.md`
for consolidated user requirements and the current build/hold split.** This file
is authoritative for the hard constraints below; the synthesis is authoritative
for feature scope and data contracts.

## Layout

- `cosmaneura_billing/` — the engine package, API, and React frontends. All
  engine work happens here.
- `docs/discovery/` — customer evidence (six conversations, Jul 21 – Aug 13 2026).
  Evidence, not spec. Nothing in discovery overrides the constraints below.
- Repo root also holds the 2023 FluencyMed prototype (`feature1`–`feature5`,
  `BioGPT-*`). Historical; not part of the tool.

## Commands

```
cd cosmaneura_billing
pip install -e ".[dev]"
python -m pytest                 # full suite
python prove_no_upcode.py        # anti-inflation invariant proof — must stay green
python run_demo.py               # offline end-to-end
uvicorn cosmaneura_billing.api:app --port 8000
cd frontend && npm install && npm run dev
```

## Hard rules

These are not preferences. A change that violates one is wrong even if the tests
pass and the customer asked for it.

1. **Anti-inflation invariant.** The compliance gate may only **hold, downgrade,
   or remove** a code. No code path raises an E/M level or adds a code as an
   adjudication action. Enforced in deterministic Python, never in prompts.
   `tests/test_guardrail_fuzz.py` and `prove_no_upcode.py` stay green on every
   change.
2. **E/M level is an output of documented facts, never a target.** Suggesting a
   level above what the provider selected is legitimate *only* with documentation
   evidence attached. No field anywhere may express a desired level or revenue
   goal.
3. **No evidence, no render.** Every code a user sees suggested carries at least
   one verified evidence span. A suggestion stripped of evidence by
   `evidence.py` is held for review, never captured.
4. **Determinism boundary.** LLMs extract facts; Python adjudicates. Any new
   check lands on the deterministic side with tests. LLM calls fail closed on
   schema or parse errors.
5. **Provider-facing billing suggestions route through the full pipeline**
   (CodingAgent → DocumentationIntegrityAgent → ComplianceGuardrailAgent →
   DenialRiskAgent). No prompt-only implementation. This is load-bearing: the
   promise made to customers — *"if it suggests the code, you can be pretty sure
   you've met all the criteria and you're not over billing"* (Maura, 7/24) — is
   only true because the deterministic pipeline checked it.
6. **PHI boundary.** No real patient data touches the system until the
   deployment layer (auth, encryption at rest, access controls) exists and every
   service receiving PHI is covered by the applicable executed agreement/BAA
   and approved data-handling configuration. A practice BAA does not by itself
   authorize disclosure to a cloud or model service. Until every applicable
   gate is complete: synthetic and properly de-identified data only.
7. **No patient steering, no revenue-motivated documentation prompts.** UI copy
   and prompts say *"capture documented, medically necessary care"* — never
   "increase revenue" or any up-billing framing.
8. **Coverage may order, never add.** Coverage-aware ordering ranks and labels
   codes that are already evidence-backed. It never adds a code to reach
   coverage, and never suppresses a documented one to avoid a non-covered label.
9. **Human verification before persistence.** Coder review for claims, provider
   confirmation for extracted values. Never auto-select, never auto-write.
10. **Direct-contract hard gate.** Any pathway that could charge or dun a patient
    checks the direct-contract flag first and hard-stops on match. Unknown or
    missing list fails closed.
11. **Provider identity and transaction boundary.** CAB is advisory billing
    intelligence, not a claim submitter. It does not emit 837P, enroll with a
    clearinghouse or payer, or write billing recommendations back to an EHR,
    and it does not require NPI during signup, onboarding, Checkout,
    entitlement, or ordinary tool use. NPI is public registry data—not
    authentication, licensure, credential proof, or tenant authority. The
    separately governed, default-off Card Chase outreach workflow is not an
    EHR/claim-write exception and retains all existing messaging gates. A future
    payer-facing standard transaction or EHR write requires a new approved architecture
    decision and just-in-time identifier collection where applicable. See
    `cosmaneura_billing/docs/NPI_AND_CLAIM_SUBMISSION_SCOPE.md`.
12. **CPT descriptors are licensed.** Reference codes by number with short
    generic labels. Official descriptors load only from the practice's own AMA
    license (`AMA_*` env vars or `CPT_DATA_DIR`). Never commit descriptor files,
    the downloaded ZIP, or AMA credentials.
13. **Never commit secrets.** `.env` stays in `.gitignore`.

## Definition of done

Tests green (including the guardrail fuzz suite), no new lint errors, evidence
spans present on every new suggestion path, and a golden test for any new
user-visible output contract.
