# Code status review — readiness for medical-coder use (2026-08-06)

Prepared ahead of the meeting with medical coders. Question answered: **how far
is this repo from being usable by coders in a web browser on a laptop?**

## TL;DR

A coder-facing web app **already exists and works**, but it lives on the
unmerged branch `claude/opus-prototype-review-ncqyat` (work dated July 23–29),
not on `main`. `main` still holds only the June 4 prototype scripts.

Verified today on that branch: **all 155 tests pass**, the FastAPI backend
boots with real 2026 regulatory data, the React frontend builds, and the full
coder review loop works end to end in a browser.

## What exists (verified, not just claimed)

- **Engine**: five agents behind one orchestrator — coding (2021 MDM-grid E/M
  leveling + ICD-10 via CCSR mapping), documentation-integrity (rejects
  contradicted codes, holds documented-but-unaddressed findings), a compliance
  guardrail that structurally cannot upcode (keep/downgrade/remove only, with a
  proof script), denial-risk checks, and a typed feedback agent.
- **Real data loaded at boot**: full ICD-10-CM FY2026 (74,549 codes), CMS NCCI
  PTP/MUE 2026Q3 edits, NGS-jurisdiction coverage articles, HCPCS Level 2, and
  8,540 denial priors from CMS PSPS 2024 (including an NH slice) plus NH payer
  denial rates (TC-PUF).
- **Coder review UI**: pick an encounter; see the engine's E/M + diagnoses,
  each with a "why" and the exact documentation quote; see withheld codes and
  why; accept/edit/reject with structured reason codes; add missed codes;
  submit. Feedback persists (JSONL) and drives an accuracy summary
  (acceptance rate, over/under-coding, seconds per chart) and a tuning
  backlog. A per-coder preference layer adapts future output ("you've
  declined this 3×" hints).
- **Also on the branch**: pytest suite + CI, packaging, Postgres/pgvector
  migrations (written, not stood up), and `VALIDATION_PROTOCOL.md` — a
  phased plan for measuring under-coding recovery with certified coders.

## Distance to "usable in a browser on a laptop" — three bars

1. **Coders click through it today: done.** Two commands on a laptop (from the
   branch):
   `uvicorn cosmaneura_billing.api:app --port 8000` and
   `cd frontend && npm install && npm run dev` → open `http://localhost:5173`.
   No API keys needed. Limitation: offline mode only truly codes the 11
   bundled encounters (GI-bleed demo + 10 ACI-Bench visits).
2. **Coders run their own de-identified notes: ~a day of setup.** Without
   `ANTHROPIC_API_KEY`, fact extraction is a canned demo model (a posted
   heart-failure note came back with GI-bleed suggestions; the evidence
   verifier correctly refused to capture them, but no real extraction
   happened). The real Anthropic adapter is written and tested — it activates
   when the key is set. De-identified notes only until a BAA is in place.
3. **Coders use it unsupervised from their desks: a few weeks.** Missing:
   hosting/deployment (currently a two-process localhost dev setup; the built
   frontend isn't served by the API), authentication (coder identity is a
   query parameter; CORS is wide open), a database (feedback is a local JSONL
   file; migrations exist but no Postgres), and the HIPAA posture (BAAs,
   access controls, audit logging) once real PHI flows.

## Scope gaps to probe with the coders

1. **E/M + ICD-10 only.** No CPT procedure-code proposals yet (schema and NCCI
   infrastructure support them; the coding agent doesn't propose them).
   Procedure-heavy specialties would hit this immediately; primary-care /
   telehealth E/M visits are covered.
2. **No licensed CPT descriptors** (`cptDescriptors: 0` at boot). Long
   descriptors are AMA-licensed; the engine deliberately shows code numbers
   with generic labels. Do coders need official descriptors? (→ AMA license.)
3. **Input is pasted/stored text.** No EHR integration and no claim export
   (837/CSV). What would coders need to act on the output?
4. **Trust/validation**: `VALIDATION_PROTOCOL.md` Phase 0 needs exactly one
   contract coder doing chart over-reads to produce an interim accuracy and
   under-coding-recovery number. The meeting attendees are the people that
   protocol needs.

## Housekeeping

- The July work is **not merged** — it exists only on
  `claude/opus-prototype-review-ncqyat`. Merge it to `main` so it can't be
  lost.
- Checked while following up a warning in the branch's design doc: **no
  credentials exist anywhere in this repo's git history** (that warning
  referred to an older local copy of the prototype).
- Verification performed in a clean session container: `pip install` +
  `pytest` (155 passed), API smoke tests against all coder-workflow endpoints,
  `npm run build` (clean), feedback write path exercised and cleaned up.
