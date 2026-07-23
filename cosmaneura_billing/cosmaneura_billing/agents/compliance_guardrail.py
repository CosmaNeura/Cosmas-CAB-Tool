"""Compliance guardrail agent -- the hard gate.

Invariants this agent enforces (and that no other agent may relax):

  * It can move a code from REVIEW -> CAPTURED, or REVIEW/CAPTURED -> REJECTED,
    and it can DOWNGRADE an E/M level. It can NEVER raise a level, add a code,
    or promote a code that lacks a documentation span.
  * An E/M code may not exceed the MDM ceiling supported by the documentation.
  * Every captured diagnosis must link to a documented sign/symptom/finding
    (medical necessity).

Because the only directions of travel are "keep", "downgrade", and "remove",
the system is structurally incapable of upcoding. Every decision is written to
an audit trail.
"""
from __future__ import annotations

from .coding import _facts_to_em
from ..schemas import CodeKind, CodeSuggestion, Disposition


def _level(code: str) -> int:
    return int(code[-1]) if code[:2] in ("99", "99") and code[-1].isdigit() else 0


class ComplianceGuardrailAgent:
    def run(
        self,
        facts: dict,
        suggestions: list[CodeSuggestion],
        *,
        new_patient: bool,
    ) -> tuple[list[CodeSuggestion], list[str]]:
        audit: list[str] = []
        ceiling = _facts_to_em(facts, new_patient=new_patient).code
        ceiling_lvl = _level(ceiling)

        for s in suggestions:
            if s.disposition == Disposition.REJECTED:
                audit.append(f"REJECT  {s.code} ({s.label}): {s.rationale}")
                continue

            if s.kind == CodeKind.EM:
                if _level(s.code) > ceiling_lvl:
                    audit.append(
                        f"DOWNGRADE {s.code} -> {ceiling}: documentation supports at most {ceiling}.")
                    s.code, s.label = ceiling, f"Office visit capped at supported level ({ceiling})"
                s.necessity_supported = bool(facts.get("problems"))
                if s.has_evidence and s.necessity_supported:
                    s.disposition = Disposition.CAPTURED
                    audit.append(f"CAPTURE {s.code}: within MDM ceiling, evidence attached.")
                else:
                    s.disposition = Disposition.REVIEW
                    audit.append(f"HOLD    {s.code}: missing evidence or necessity; provider to confirm.")
                continue

            if s.kind == CodeKind.ICD10:
                if s.code == "(unresolved)":
                    s.disposition = Disposition.REVIEW
                    audit.append(f"HOLD    unresolved dx '{s.label}': map to a valid ICD-10 before billing.")
                    continue
                addressed = "not_addressed_in_plan" not in s.notes
                s.necessity_supported = s.has_evidence and addressed
                if s.necessity_supported and s.disposition != Disposition.REJECTED:
                    s.disposition = Disposition.CAPTURED
                    audit.append(f"CAPTURE {s.code} ({s.label}): linked to documented finding.")
                else:
                    s.disposition = Disposition.REVIEW
                    reason = ("documented but not addressed in plan" if not addressed
                              else "no documented finding to support necessity")
                    audit.append(f"HOLD    {s.code} ({s.label}): {reason}.")

        return suggestions, audit
