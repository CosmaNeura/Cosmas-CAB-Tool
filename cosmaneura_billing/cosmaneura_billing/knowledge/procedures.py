"""Procedure / add-on CPT candidates from documented language.

Why this module exists
----------------------
The engine proposed E/M levels and diagnoses but never a procedure code:
``CodeKind.PROCEDURE`` was defined, the worksheet collected procedures, and no
agent ever populated it. MCD's billing team asked for one specifically —
**CPT 99058**, the rapid-access add-on, where missing it is straight lost
revenue (Grace Targonski, 2026-08-13).

Design
------
The trigger for an add-on like 99058 is a *documented phrase*, not a clinical
inference: the note either says the visit disrupted the schedule or it doesn't.
That puts this entirely on the deterministic side of the boundary — regex over
the documentation, no model call — and it means every candidate can carry the
verbatim sentence that triggered it as its evidence span.

Rules are conservative on purpose. A false positive costs a coder review
minute, and the standing constraint from the billing team is that net coder
minutes must go *down*. Better to miss an ambiguous case than to add work.

Nothing here captures a code. Every candidate is emitted for review and, where
the payer restricts the code, marked ``requires_provider_confirmation`` so the
compliance guardrail holds it even when evidence is present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schemas import CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle

# Places of service where a code is billable. 99058 is defined as a service
# "provided on an emergency basis in the office" — on a telehealth encounter
# (POS 02 / 10) it does not apply, and proposing it there invites a denial.
_OFFICE_POS = frozenset({"11"})

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class ProcedureRule:
    code: str
    label: str
    why: str
    patterns: tuple[re.Pattern[str], ...]
    # POS codes the service is billable in; empty means "any".
    places_of_service: frozenset[str] = frozenset()
    # Requires an E/M service on the same encounter (true for add-on codes).
    requires_em: bool = True
    # Payer restricts the code (e.g. CMS MUE of 0 with a clinical
    # date-of-service edit) — never auto-capture, always route to a human.
    requires_provider_confirmation: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


# CPT 99058 — service provided on an emergency basis in the office, disrupting
# other scheduled office services. Billed as an add-on alongside the E/M.
#
# Phrases are deliberately specific. "urgent" alone is far too common in
# clinical prose to be a billing trigger; what 99058 needs documented is that
# the visit was unscheduled *and* displaced other scheduled work.
RULE_99058 = ProcedureRule(
    code="99058",
    label="Office service on an emergency basis, disrupting scheduled services",
    why=("Documentation describes an unscheduled visit accommodated on an "
         "emergency basis that disrupted other scheduled office services, which "
         "is what 99058 reports as an add-on to the E/M service."),
    patterns=(
        re.compile(r"\bon an emergency basis\b", re.I),
        re.compile(r"\b(?:worked|squeezed|fit)\s+(?:her|him|them|the patient)?\s*in\b", re.I),
        re.compile(r"\bwork[- ]in (?:visit|appointment|patient)\b", re.I),
        re.compile(r"\brapid[- ]access (?:visit|appointment)\b", re.I),
        re.compile(r"\bdisrupt(?:ed|ing) (?:the )?(?:other )?"
                   r"(?:scheduled|clinic|office) (?:visits|services|schedule)\b", re.I),
        re.compile(r"\bunscheduled\b.{0,40}\bsame[- ]day\b", re.I),
        re.compile(r"\bsame[- ]day\b.{0,40}\bunscheduled\b", re.I),
    ),
    places_of_service=_OFFICE_POS,
    requires_em=True,
    # CMS publishes 99058 with an MUE of 0 and a clinical date-of-service edit,
    # and commercial coverage is inconsistent. The engine surfaces it with its
    # evidence; a human decides whether this payer will pay it.
    requires_provider_confirmation=True,
    notes=("cms_mue_0", "payer_coverage_varies"),
)

RULES: tuple[ProcedureRule, ...] = (RULE_99058,)


def _sources(bundle: VisitBundle) -> list[tuple[str, str]]:
    return [
        ("signed_note", bundle.signed_note),
        ("transcript", bundle.transcript),
        ("intake_text", bundle.intake_text),
    ]


def _matching_sentence(pattern: re.Pattern[str], text: str) -> str | None:
    """The verbatim sentence containing the first match.

    Returned as-is from the source so the evidence verifier can find it — a
    reconstructed or normalised quote would not verify, and would rightly be
    stripped.
    """
    if not text:
        return None
    for sentence in _SENTENCE_SPLIT.split(text):
        cleaned = sentence.strip()
        if cleaned and pattern.search(cleaned):
            return cleaned
    return None


class ProcedureRules:
    """Deterministic procedure/add-on candidate proposals."""

    def __init__(self, rules: tuple[ProcedureRule, ...] = RULES):
        self.rules = rules

    def propose(
        self, bundle: VisitBundle, *, has_em: bool = True
    ) -> tuple[list[CodeSuggestion], list[str]]:
        """Return (candidates, audit lines).

        Candidates are always REVIEW. A rule that matches but fails a
        gate — wrong place of service, no E/M on the encounter — produces an
        audit line and no candidate, so the reason a code was *not* proposed is
        recoverable rather than invisible.
        """
        out: list[CodeSuggestion] = []
        audit: list[str] = []

        for rule in self.rules:
            hit = next(
                ((src, quote) for src, text in _sources(bundle)
                 for pattern in rule.patterns
                 if (quote := _matching_sentence(pattern, text))),
                None,
            )
            if hit is None:
                continue
            source, quote = hit

            if rule.places_of_service and bundle.place_of_service not in rule.places_of_service:
                audit.append(
                    f"SKIP    {rule.code}: documentation supports it, but place of "
                    f"service {bundle.place_of_service} is not billable for this code "
                    f"(requires {'/'.join(sorted(rule.places_of_service))}).")
                continue
            if rule.requires_em and not has_em:
                audit.append(
                    f"SKIP    {rule.code}: add-on code requires an E/M service on the "
                    "same encounter; none was proposed.")
                continue

            notes = list(rule.notes)
            if rule.requires_provider_confirmation:
                notes.append("requires_provider_confirmation")
            out.append(CodeSuggestion(
                kind=CodeKind.PROCEDURE,
                code=rule.code,
                label=rule.label,
                disposition=Disposition.REVIEW,
                evidence=[Evidence(source, quote, "documents the add-on's trigger")],
                rationale=rule.why,
                notes=notes,
            ))
            audit.append(f"PROPOSE {rule.code}: add-on candidate from documented language.")

        return out, audit
