"""Randomised adversarial cases for the two-engine consensus invariant.

``guardrail_fuzz`` proves the compliance guardrail cannot be pushed to inflate.
Running a *second* extraction engine adds a new way to break that promise: the
merge could raise the E/M ceiling, or introduce a billable code the primary
engine never proposed. This module proves it cannot, as a property rather than
a scenario.

The generator reuses ``guardrail_fuzz.generate_case`` for the primary engine and
then builds a deliberately hostile secondary engine on top: extra problems and
data (which would raise a level if they ever reached the leveling grid), codes
the primary engine never proposed, codes pre-marked CAPTURED, E/M codes the
secondary engine has no business proposing at all, and vetoes against codes the
primary engine proposed confidently.

Invariants checked, independent of the consensus agent's own arithmetic:

  C1  The E/M ceiling from merged facts equals the ceiling from the primary
      engine's facts alone — a second engine can never move it.
  C2  The set of CAPTURED codes after (consensus -> guardrail) is a subset of
      the set the primary engine alone would have produced. This is the whole
      promise in one line: adding an engine cannot add a billable code.
  C3  A code only the secondary engine proposed is never CAPTURED.
  C4  A code the secondary engine vetoed ends REJECTED.
  C5  No E/M suggestion in the merged output originated from the secondary
      engine.

Every ``guardrail_fuzz`` invariant is also re-checked on the merged output, so
the guarantees proven for one engine still hold for two.

Used by ``tests/test_consensus_fuzz.py`` and by ``prove_no_upcode.py``.
"""
from __future__ import annotations

import random

from .agents.coding import _facts_to_em
from .agents.compliance_guardrail import ComplianceGuardrailAgent
from .agents.consensus import ConsensusAgent
from .guardrail_fuzz import (
    _DX_CODES, _EM_CODES, check_invariants, em_level, generate_case,
)
from .schemas import CodeKind, CodeSuggestion, Disposition, Evidence

_VETO_REASONS = (
    "documentation states this finding is absent/denied",
    "finding is attributed to a family member, not the patient",
    "finding is hypothetical, not an active diagnosis",
)


def _clone(s: CodeSuggestion) -> CodeSuggestion:
    """A detached copy, so the single- and two-engine runs cannot share state."""
    return CodeSuggestion(
        kind=s.kind, code=s.code, label=s.label, disposition=s.disposition,
        evidence=[Evidence(e.source, e.quote, e.rationale) for e in s.evidence],
        rationale=s.rationale, necessity_supported=s.necessity_supported,
        denial_risk=s.denial_risk, notes=list(s.notes),
        source_engine=s.source_engine, agreement=s.agreement,
    )


def generate_secondary(
    rng: random.Random, primary: list[CodeSuggestion]
) -> tuple[dict, list[CodeSuggestion]]:
    """A hostile secondary engine for a given primary result."""
    # Facts that would raise a level if they ever reached the leveling grid.
    facts: dict = {
        "ta4h_problems": [{"name": f"ta4h-problem-{i}", "quote": "q"}
                          for i in range(rng.choice([0, 3, 8, 15]))],
        "ta4h_data_reviewed": [{"item": f"ta4h-lab-{i}", "category": f"c{i}"}
                               for i in range(rng.choice([0, 2, 6, 12]))],
        "ta4h_contradictions": {},
        "ta4h_entity_count": rng.randint(0, 40),
    }

    # Veto a subset of the primary engine's diagnosis codes.
    primary_dx = [s.code for s in primary if s.kind == CodeKind.ICD10]
    for code in primary_dx:
        if rng.random() < 0.3:
            facts["ta4h_contradictions"][code.replace(".", "").upper()] = \
                rng.choice(_VETO_REASONS)

    suggestions: list[CodeSuggestion] = []
    for _ in range(rng.randint(0, 4)):
        # Mostly diagnoses, but sometimes an E/M code the secondary engine has
        # no business proposing — C5 proves it never lands as one.
        kind = rng.choices([CodeKind.ICD10, CodeKind.EM, CodeKind.PROCEDURE],
                           weights=[8, 1, 1])[0]
        if kind == CodeKind.EM:
            code, label = rng.choice(_EM_CODES), "Office visit (from secondary)"
        elif kind == CodeKind.ICD10:
            code, label = rng.choice(_DX_CODES), "secondary-engine dx"
        else:
            code, label = rng.choice(["99058", "36415"]), "secondary-engine proc"
        suggestions.append(CodeSuggestion(
            kind=kind, code=code, label=label,
            # The adversarial move: the secondary engine asserting billability.
            disposition=rng.choice(list(Disposition)),
            evidence=([Evidence("signed_note", "refer for colonoscopy", "ta4h")]
                      if rng.random() < 0.7 else []),
            source_engine="ta4h",
        ))
    return facts, suggestions


def check_consensus_invariants(
    primary_facts: dict,
    merged_facts: dict,
    secondary_codes: set[str],
    vetoed: set[str],
    merged: list[CodeSuggestion],
    captured_single: set[str],
    captured_dual: set[str],
    *,
    new_patient: bool,
) -> list[str]:
    problems: list[str] = []

    ceiling_primary = em_level(_facts_to_em(primary_facts, new_patient=new_patient).code)
    ceiling_merged = em_level(_facts_to_em(merged_facts, new_patient=new_patient).code)
    if ceiling_merged != ceiling_primary:
        problems.append(
            f"C1 ceiling moved L{ceiling_primary} -> L{ceiling_merged} on merged facts")

    grew = captured_dual - captured_single
    if grew:
        problems.append(
            f"C2 captured set grew with a second engine: added {sorted(grew)}")

    for s in merged:
        norm = s.code.replace(".", "").upper()
        if s.agreement == "ta4h_only" and s.disposition == Disposition.CAPTURED:
            problems.append(f"C3 secondary-only code {s.code!r} was CAPTURED")
        if norm in vetoed and s.disposition != Disposition.REJECTED:
            problems.append(
                f"C4 vetoed code {s.code!r} ended {s.disposition.value}, not rejected")
        if s.kind == CodeKind.EM and s.source_engine == "ta4h":
            problems.append(f"C5 E/M code {s.code!r} attributed to the secondary engine")

    return problems


def run_consensus_fuzz(cases: int = 2000, seed: int = 20260819) -> list[str]:
    """Run ``cases`` adversarial two-engine cases. Returns all violations found."""
    rng = random.Random(seed)
    guardrail = ComplianceGuardrailAgent()
    consensus = ConsensusAgent()
    violations: list[str] = []

    for i in range(cases):
        primary_facts, primary, new_patient = generate_case(rng)
        secondary_facts, secondary = generate_secondary(rng, primary)

        # Baseline: what the primary engine alone would have captured.
        single_out, _ = guardrail.run(
            primary_facts, [_clone(s) for s in primary], new_patient=new_patient)
        captured_single = {s.code for s in single_out
                           if s.disposition == Disposition.CAPTURED}

        # The two-engine path, from the same primary input.
        dual_in = [_clone(s) for s in primary]
        before = [(s.kind, s.code, s.disposition, len(s.evidence)) for s in dual_in]
        merged_facts, merged, _audit = consensus.run(
            (primary_facts, dual_in), (secondary_facts, [_clone(s) for s in secondary]),
            new_patient=new_patient)
        dual_out, _ = guardrail.run(merged_facts, merged, new_patient=new_patient)
        captured_dual = {s.code for s in dual_out
                         if s.disposition == Disposition.CAPTURED}

        for v in check_consensus_invariants(
            primary_facts, merged_facts,
            {s.code for s in secondary}, set(secondary_facts["ta4h_contradictions"]),
            dual_out, captured_single, captured_dual, new_patient=new_patient,
        ):
            violations.append(f"case {i}: {v}")

        # Every single-engine guardrail invariant must still hold for the
        # primary engine's codes inside the merged output.
        primary_slice = dual_out[:len(before)]
        for v in check_invariants(
            merged_facts, before, primary_slice, new_patient=new_patient,
        ):
            violations.append(f"case {i} (merged): {v}")

    return violations
