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

import re

from .coding import _facts_to_em
from ..schemas import CodeKind, CodeSuggestion, Disposition

# An office-visit E/M code is exactly five digits and nothing else. Parsing the
# last character instead (the obvious shortcut) is unsafe in both directions:
# "99213-25" reads as level 5, so clamping it to a level-4 ceiling would *raise*
# a level-3 visit; "99214 (telehealth)" reads as level 0, so it would bypass the
# ceiling entirely. Anything this pattern does not match is treated as
# unverifiable and held — the guardrail never rewrites a code it cannot parse.
_EM_CODE = re.compile(r"^(?:9921[1-5]|9920[2-5])$")


def _level(code: str) -> int | None:
    """Level 1-5 of an office-visit E/M code, or None if it isn't one."""
    stripped = (code or "").strip()
    return int(stripped[-1]) if _EM_CODE.match(stripped) else None


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
        ceiling_lvl = _level(ceiling) or 0

        for s in suggestions:
            if s.disposition == Disposition.REJECTED:
                audit.append(f"REJECT  {s.code} ({s.label}): {s.rationale}")
                continue

            # Procedure / add-on / modifier codes. There is no MDM ceiling to
            # clamp these against, so the evidence gate is the guardrail's only
            # lever — and a code carrying `requires_provider_confirmation` is
            # held even with evidence (e.g. 99058, which CMS restricts with a
            # clinical date-of-service edit and an MUE of 0).
            if s.kind in (CodeKind.PROCEDURE, CodeKind.MODIFIER):
                needs_confirm = any(n == "requires_provider_confirmation" for n in s.notes)
                if s.has_evidence and not needs_confirm:
                    s.necessity_supported = True
                    s.disposition = Disposition.CAPTURED
                    audit.append(f"CAPTURE {s.code} ({s.label}): evidence attached.")
                else:
                    s.disposition = Disposition.REVIEW
                    reason = ("payer restricts this code — provider must confirm"
                              if needs_confirm else "no documentation span attached")
                    audit.append(f"HOLD    {s.code} ({s.label}): {reason}.")
                continue

            if s.kind == CodeKind.EM:
                lvl = _level(s.code)
                if lvl is None:
                    # Fail closed: we cannot verify an unparseable code against
                    # the ceiling, and rewriting it could change its meaning.
                    s.disposition = Disposition.REVIEW
                    s.notes.append("unverifiable E/M code format")
                    audit.append(
                        f"HOLD    {s.code}: not a recognizable office-visit E/M code; "
                        f"level cannot be verified against the {ceiling} ceiling.")
                    continue
                if lvl > ceiling_lvl:
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
