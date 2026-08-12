"""Anti-inflation fuzz: 2,000 randomized cases against the compliance guardrail.

The invariant under test (CLAUDE.md constraint #1): the guardrail can only
keep, downgrade, or remove — across arbitrary facts and arbitrary (including
adversarial) suggestion sets it must never

  * raise an E/M level above the input level or the documented MDM ceiling,
  * add a suggestion that wasn't in its input,
  * resurrect a REJECTED suggestion,
  * capture any code that lacks a documentation span.

Seeded RNG keeps every run identical; a failure prints the case number so the
exact scenario reproduces with the same seed.
"""
from __future__ import annotations

import random

from cosmaneura_billing.agents import ComplianceGuardrailAgent
from cosmaneura_billing.agents.coding import _facts_to_em
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence

SEED = 20260812
N_CASES = 2_000

_EM_CODES = ["99211", "99212", "99213", "99214", "99215",
             "99202", "99203", "99204", "99205"]
_ICD_CODES = ["K62.5", "E66.9", "Z80.0", "R19.7", "I10", "E11.9", "(unresolved)"]
_PROC_CODES = ["82270", "36415", "93000"]
_RISK_LEVELS = ["low", "moderate", "high"]
_STATUSES = ["new", "stable", "worsening"]


def _level(code: str) -> int:
    return int(code[-1]) if code[:2] == "99" and code[-1].isdigit() else 0


def _random_facts(rng: random.Random) -> dict:
    facts: dict = {
        "chief_complaint": rng.choice(["bleeding", "cough", "follow-up", ""]),
        "problems": [
            {
                "name": rng.choice(["Hematochezia", "Obesity", "Hypertension", "URI"]),
                "status": rng.choice(_STATUSES),
                "uncertain_prognosis": rng.random() < 0.3,
                "quote": rng.choice(["blood per rectum", "BMI 37.6", ""]),
            }
            for _ in range(rng.randint(0, 3))
        ],
        "data_reviewed": [
            {"category": rng.choice(["review_external_labs", "ordered_test"]),
             "item": rng.choice(["CBC", "FOBT", "CMP"])}
            for _ in range(rng.randint(0, 3))
        ],
        "risk": (
            {"level": rng.choice(_RISK_LEVELS),
             "quote": rng.choice(["refer for colonoscopy", "", "monitor"])}
            if rng.random() < 0.85 else {}
        ),
    }
    # Occasionally feed junk shapes; sanitize_facts must cope.
    if rng.random() < 0.1:
        facts["problems"].append("not-a-dict")
    if rng.random() < 0.05:
        facts["risk"] = "garbage"
    return facts


def _random_suggestion(rng: random.Random) -> CodeSuggestion:
    kind = rng.choice([CodeKind.EM, CodeKind.ICD10, CodeKind.ICD10,
                       CodeKind.PROCEDURE, CodeKind.MODIFIER])
    if kind == CodeKind.EM:
        code = rng.choice(_EM_CODES)
    elif kind == CodeKind.ICD10:
        code = rng.choice(_ICD_CODES)
    elif kind == CodeKind.PROCEDURE:
        code = rng.choice(_PROC_CODES)
    else:
        code = rng.choice(["25", "95"])
    s = CodeSuggestion(
        kind=kind, code=code, label=f"fuzz {code}",
        disposition=rng.choice([Disposition.CAPTURED, Disposition.REVIEW,
                                Disposition.REJECTED]),
        evidence=([Evidence("signed_note", "documented finding", "fuzz")]
                  if rng.random() < 0.7 else []),
        rationale="fuzz case",
    )
    if kind == CodeKind.ICD10 and rng.random() < 0.25:
        s.notes.append("not_addressed_in_plan")
    if kind == CodeKind.ICD10 and rng.random() < 0.15:
        s.notes.append("preference_hold: reviewer declined this 3x")
    return s


def test_guardrail_never_inflates_2000_cases():
    rng = random.Random(SEED)
    agent = ComplianceGuardrailAgent()

    for case in range(N_CASES):
        facts = _random_facts(rng)
        new_patient = rng.random() < 0.3
        suggestions = [_random_suggestion(rng) for _ in range(rng.randint(1, 6))]

        inputs = [(s, s.kind, s.code, s.disposition, list(s.evidence))
                  for s in suggestions]
        ceiling = _level(_facts_to_em(facts, new_patient=new_patient).code)

        out, audit = agent.run(facts, suggestions, new_patient=new_patient)

        # No additions, no removals, no substitutions — identity preserved.
        assert len(out) == len(inputs), f"case {case}: count changed"
        for obj, (orig, *_rest) in zip(out, inputs):
            assert obj is orig, f"case {case}: suggestion object replaced"

        for obj, (_, kind, in_code, in_disp, in_evidence) in zip(out, inputs):
            # A rejected input stays rejected, untouched.
            if in_disp == Disposition.REJECTED:
                assert obj.disposition == Disposition.REJECTED, \
                    f"case {case}: rejected {in_code} resurrected"
                assert obj.code == in_code
                continue

            if kind == CodeKind.EM:
                in_lvl, out_lvl = _level(in_code), _level(obj.code)
                # keep or downgrade — never above input, never above ceiling
                assert out_lvl <= in_lvl, \
                    f"case {case}: E/M raised {in_code} -> {obj.code}"
                assert out_lvl <= max(ceiling, 0) or out_lvl == in_lvl <= ceiling, \
                    f"case {case}: E/M {obj.code} above ceiling {ceiling}"
                if in_lvl > ceiling:
                    assert out_lvl == ceiling, \
                        f"case {case}: over-level not capped to ceiling"
                if obj.disposition == Disposition.CAPTURED:
                    assert obj.has_evidence, f"case {case}: captured E/M without span"
                    assert obj.necessity_supported, \
                        f"case {case}: captured E/M without necessity"

            elif kind == CodeKind.ICD10:
                assert obj.code == in_code, f"case {case}: ICD code mutated"
                if obj.disposition == Disposition.CAPTURED:
                    assert obj.has_evidence, f"case {case}: captured dx without span"
                    assert "not_addressed_in_plan" not in obj.notes, \
                        f"case {case}: unaddressed dx captured"
                    assert obj.code != "(unresolved)", \
                        f"case {case}: unresolved dx captured"
                if any(n.startswith("preference_hold") for n in obj.notes):
                    assert obj.disposition == Disposition.REVIEW, \
                        f"case {case}: preference_hold not honored"

            else:  # PROCEDURE / MODIFIER pass through untouched
                assert obj.code == in_code and obj.disposition == in_disp, \
                    f"case {case}: non-EM/dx suggestion mutated"


def test_fuzz_is_deterministic():
    """Same seed twice -> byte-identical scenario stream (reproducibility)."""
    a, b = random.Random(SEED), random.Random(SEED)
    for _ in range(200):
        assert _random_facts(a) == _random_facts(b)
