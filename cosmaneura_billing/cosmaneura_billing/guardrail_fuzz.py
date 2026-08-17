"""Randomised adversarial cases for the anti-inflation invariant.

The compliance guardrail's promise is the one thing customers have been told on
the record: *"if it suggests the code, you can be pretty sure that you've met all
the criteria and you're not over billing."* A single hand-written scenario cannot
back that sentence — it only proves the guardrail handles the case its author
thought of. This module generates adversarial inputs and checks the invariant as
a property.

The generator deliberately produces inputs an upstream agent should never emit:
levels above what the facts support, codes pre-marked CAPTURED, suggestions with
no evidence, modifier-suffixed and malformed code strings, empty fact sets. The
guardrail must survive all of them without ever inflating.

Invariants checked (each independent of the guardrail's own arithmetic):

  I1  No suggestion is added or removed.
  I2  An E/M code's level never rises: ``level(out) <= level(in)``.
  I3  A CAPTURED E/M code never exceeds the ceiling the facts support.
  I4  No evidence span is added to any suggestion.
  I5  No suggestion ends CAPTURED without evidence.
  I6  A REJECTED suggestion stays REJECTED.
  I7  A non-E/M code string is never rewritten.
  I8  An E/M code that is not an exact office-visit CPT code is never CAPTURED
      (fail-closed: the level cannot be verified, so a human resolves it).

I3 is scoped to CAPTURED on purpose. When the guardrail cannot parse a code it
holds it for review **without rewriting it** — the reviewer needs to see exactly
what was proposed, and silently normalising "99214 (telehealth)" would destroy
the annotation that explains it. I8 is the guarantee that covers those: unparsed
means held, never billed.

Used by ``tests/test_guardrail_fuzz.py`` and by ``prove_no_upcode.py``.
"""
from __future__ import annotations

import random
import re

from .agents.coding import _facts_to_em
from .agents.compliance_guardrail import ComplianceGuardrailAgent
from .schemas import CodeKind, CodeSuggestion, Disposition, Evidence

# An office-visit E/M code is exactly five digits; anything after it (a "-25"
# modifier, whitespace, stray text) is not part of the level. Parsed here
# independently of the guardrail so a parsing bug in either shows up as a
# failed invariant rather than cancelling out.
_CPT5 = re.compile(r"^\s*(9921[1-5]|9920[2-5])\b")
# Exact match — no annotation, no modifier suffix, nothing trailing.
_CPT5_EXACT = re.compile(r"^(?:9921[1-5]|9920[2-5])$")

_RISK_LEVELS = ["straightforward", "low", "moderate", "high", "unknown", ""]
_EM_CODES = ["99211", "99212", "99213", "99214", "99215",
             "99202", "99203", "99204", "99205"]
# Strings an upstream agent could plausibly produce that must not confuse the
# level comparison. The modifier-suffixed forms are the dangerous ones: naive
# last-character parsing reads "99213-25" as level 5.
_ADVERSARIAL_CODES = ["99213-25", "99212-95", "99214 (telehealth)", "G0463",
                      "99215X", "992", "", "9921", "99213.0"]
_DX_CODES = ["K62.5", "K59.00", "R19.7", "E66.9", "E66.01", "Z80.0", "N97.9",
             "(unresolved)"]


def em_level(code: str) -> int:
    """Level 1-5 of an office-visit E/M code, or 0 if the string isn't one."""
    m = _CPT5.match(code or "")
    return int(m.group(1)[-1]) if m else 0


def generate_case(rng: random.Random) -> tuple[dict, list[CodeSuggestion], bool]:
    """One adversarial (facts, suggestions, new_patient) triple."""
    n_problems = rng.choice([0, 0, 1, 1, 2, 3, 5])
    problems = [{
        "name": f"problem-{i}",
        "status": rng.choice(["new", "established", "stable", "worsening", ""]),
        "acuity": rng.choice(["acute", "chronic", ""]),
        "uncertain_prognosis": rng.choice([True, False, None]),
        "quote": rng.choice(["blood per rectum", "", "documented finding"]),
    } for i in range(n_problems)]

    n_data = rng.choice([0, 1, 2, 3, 4])
    data = [{"category": rng.choice(["review_external_labs", "order_labs",
                                     "independent_historian", "discuss_management"]),
             "item": f"item-{i}", "quote": ""} for i in range(n_data)]

    facts: dict = {
        "chief_complaint": rng.choice(["cc", ""]),
        "problems": problems,
        "data_reviewed": data,
        "risk": {"level": rng.choice(_RISK_LEVELS),
                 "quote": rng.choice(["refer for colonoscopy", ""])},
        # A degenerate/hostile time value must never inflate a level.
        "total_time_minutes": rng.choice([None, 5, 15, 25, 45, 90, 10_000, -3, 0]),
        "vitals": rng.choice([{}, {"documented": True, "addressed_in_plan": False,
                                   "bmi": 37.6}]),
        "family_history_colon_cancer": rng.choice([None, True, False]),
        "family_history_quote": "I do not have a family history of colon cancer",
    }

    suggestions: list[CodeSuggestion] = []
    for _ in range(rng.randint(1, 5)):
        kind = rng.choices(
            [CodeKind.EM, CodeKind.ICD10, CodeKind.PROCEDURE, CodeKind.MODIFIER],
            weights=[4, 4, 1, 1])[0]
        if kind == CodeKind.EM:
            code = rng.choice(_EM_CODES + _ADVERSARIAL_CODES)
            label = "Office visit"
        elif kind == CodeKind.ICD10:
            code, label = rng.choice(_DX_CODES), rng.choice(
                ["Hemorrhage of anus and rectum", "Other obesity",
                 "Family history of malignant neoplasm of digestive organs"])
        else:
            code, label = rng.choice(["99058", "36415", "25", "95"]), "procedure/modifier"
        evidence = ([Evidence(rng.choice(["signed_note", "transcript", "intake_text"]),
                              "refer for colonoscopy", "risk")]
                    if rng.random() < 0.6 else [])
        suggestions.append(CodeSuggestion(
            kind=kind, code=code, label=label,
            # Pre-marking CAPTURED is the adversarial move: an upstream agent
            # asserting a code is billable before the gate has looked at it.
            disposition=rng.choice(list(Disposition)),
            evidence=evidence,
        ))

    return facts, suggestions, rng.random() < 0.3


def check_invariants(
    facts: dict,
    before: list[tuple[CodeKind, str, Disposition, int]],
    after: list[CodeSuggestion],
    *,
    new_patient: bool,
) -> list[str]:
    """Return a list of invariant violations (empty when the guardrail behaved)."""
    problems: list[str] = []
    ceiling_lvl = em_level(_facts_to_em(facts, new_patient=new_patient).code)

    if len(before) != len(after):
        problems.append(f"I1 suggestion count changed: {len(before)} -> {len(after)}")
        return problems

    for (kind, code_in, disp_in, n_ev_in), s in zip(before, after):
        where = f"{kind.value} {code_in!r}"
        # A REJECTED code never reaches a claim or a reviewer's candidate list,
        # so its level is not a compliance statement.
        if kind == CodeKind.EM and s.disposition != Disposition.REJECTED:
            lvl_in, lvl_out = em_level(code_in), em_level(s.code)
            if lvl_out > lvl_in:
                problems.append(
                    f"I2 {where}: level raised {code_in} (L{lvl_in}) -> {s.code} (L{lvl_out})")
            if s.disposition == Disposition.CAPTURED:
                if lvl_out > ceiling_lvl:
                    problems.append(
                        f"I3 {where}: captured {s.code} (L{lvl_out}) exceeds ceiling L{ceiling_lvl}")
                if not _CPT5_EXACT.match((s.code or "").strip()):
                    problems.append(
                        f"I8 {where}: captured {s.code!r}, which is not an exact office-visit "
                        "E/M code, so its level was never verified")
        elif code_in != s.code:
            problems.append(f"I7 {where}: code rewritten to {s.code!r}")

        if len(s.evidence) > n_ev_in:
            problems.append(f"I4 {where}: evidence added ({n_ev_in} -> {len(s.evidence)})")
        if s.disposition == Disposition.CAPTURED and not s.evidence:
            problems.append(f"I5 {where}: CAPTURED with no evidence span")
        if disp_in == Disposition.REJECTED and s.disposition != Disposition.REJECTED:
            problems.append(f"I6 {where}: REJECTED -> {s.disposition.value}")

    return problems


def run_fuzz(cases: int = 2000, seed: int = 20260817) -> list[str]:
    """Run ``cases`` adversarial cases. Returns all violations found."""
    rng = random.Random(seed)
    agent = ComplianceGuardrailAgent()
    violations: list[str] = []
    for i in range(cases):
        facts, suggestions, new_patient = generate_case(rng)
        before = [(s.kind, s.code, s.disposition, len(s.evidence)) for s in suggestions]
        out, _audit = agent.run(facts, suggestions, new_patient=new_patient)
        for v in check_invariants(facts, before, out, new_patient=new_patient):
            violations.append(f"case {i}: {v}")
    return violations
