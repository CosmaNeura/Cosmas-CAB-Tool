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


# Notes that force a code to REVIEW. Both are one-directional: they can only
# hold a code back, never argue one into the claim, so honouring them keeps the
# keep/downgrade/remove invariant intact.
#   preference_hold -- a learned reviewer preference (preferences.py); checked
#                      first, because the reviewer's own history outranks our
#                      reasoning about the chart
#   consensus_hold  -- only one extraction engine proposed it (consensus.py);
#                      checked last, so a code held for a *clinical* reason
#                      reports that reason rather than the engine split
_PREFERENCE_HOLD = "preference_hold"
_CONSENSUS_HOLD = "consensus_hold"


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
                # a learned reviewer preference can hold a code for review; it
                # never argues one INTO the claim, so honoring it here keeps
                # the guardrail's keep/downgrade/remove invariant intact
                hint = next((n for n in s.notes if n.startswith(_PREFERENCE_HOLD)), None)
                if hint is not None:
                    s.disposition = Disposition.REVIEW
                    audit.append(f"HOLD    {s.code} ({s.label}): {hint.split(': ', 1)[-1]}.")
                    continue
                addressed = "not_addressed_in_plan" not in s.notes
                s.necessity_supported = s.has_evidence and addressed
                if s.necessity_supported and s.disposition != Disposition.REJECTED:
                    # Clinically capturable. One last gate: a code only one
                    # extraction engine found still needs a human, so it is held
                    # rather than captured. Checked here (and not earlier) so a
                    # code held for a clinical reason reports that reason.
                    split = next((n for n in s.notes
                                  if n.startswith(_CONSENSUS_HOLD)), None)
                    if split is not None:
                        s.disposition = Disposition.REVIEW
                        audit.append(f"HOLD    {s.code} ({s.label}): {split.split(': ', 1)[-1]}.")
                    else:
                        s.disposition = Disposition.CAPTURED
                        audit.append(f"CAPTURE {s.code} ({s.label}): linked to documented finding.")
                else:
                    s.disposition = Disposition.REVIEW
                    reason = ("documented but not addressed in plan" if not addressed
                              else "no documented finding to support necessity")
                    audit.append(f"HOLD    {s.code} ({s.label}): {reason}.")

        return suggestions, audit
