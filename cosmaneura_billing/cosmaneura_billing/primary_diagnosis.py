"""Preserve extracted sequencing intent without promoting a substitute code.

Call after evidence, clinical and current-code gates. Intent is not evidence or
final human sequencing. This resolver does not change any code or disposition.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .schemas import CodeKind, CodeSuggestion, Disposition

VERSION = "diagnosis-sequencing.2026-09-30.v1"
INTENT = "primary_candidate"
INVALID_INTENT = "primary_intent_invalid"
_MESSAGES = {
    "resolved_explicit": "An extracted primary diagnosis candidate is supported. Confirm final sequencing in human review.",
    "unresolved_explicit": "The extracted primary diagnosis candidate is unresolved or held. Secondary diagnoses remain available; confirm the primary diagnosis and sequencing before use.",
    "conflicting_explicit": "More than one primary diagnosis intent remains. No primary was selected; confirm diagnosis sequencing in human review.",
    "legacy_fallback": "No explicit primary diagnosis was extracted. The first captured diagnosis retains provisional order only; confirm sequencing in human review.",
    "not_established": "Primary diagnosis sequencing is not established. Review the source and confirm the diagnosis order before use.",
}


def intent_notes(value: dict) -> list[str]:
    """JSON true alone creates intent; malformed truthy values cannot do so."""
    if value.get("primary") is True:
        return [INTENT]
    if "primary" in value and type(value["primary"]) is not bool:
        return [INVALID_INTENT]
    return []


def _captured(s: CodeSuggestion) -> bool:
    return (s.disposition == Disposition.CAPTURED and s.necessity_supported is True
            and s.has_evidence and isinstance(s.code, str)
            and re.fullmatch(r"[A-Z][0-9][A-Z0-9](?:\.[A-Z0-9]{1,4})?", s.code) is not None)


def _identity(s: CodeSuggestion) -> tuple:
    # Equal code strings alone do not erase conflicting evidence/dispositions.
    return (s.code, s.label, s.disposition, s.necessity_supported,
            tuple((e.source, e.quote, e.rationale) for e in s.evidence))


@dataclass(frozen=True)
class PrimaryResolution:
    status: str
    primary: CodeSuggestion | None = None
    intents: tuple[CodeSuggestion, ...] = ()

    @property
    def public(self) -> dict:
        return {
            "rule_version": VERSION, "status": self.status,
            "selection_basis": ("explicit_extracted" if self.status == "resolved_explicit"
                                else "legacy_first_captured" if self.status == "legacy_fallback"
                                else "none"),
            "requires_review": self.status != "resolved_explicit",
            "message": _MESSAGES[self.status],
        }


def resolve_primary(suggestions: list[CodeSuggestion]) -> PrimaryResolution:
    """Inspect every diagnosis, including held/rejected explicit candidates."""
    diagnoses = [s for s in suggestions if s.kind == CodeKind.ICD10]
    intents, seen = [], set()
    for s in diagnoses:
        if INTENT in s.notes and _identity(s) not in seen:
            seen.add(_identity(s))
            intents.append(s)
    candidates = tuple(intents)
    if any(INVALID_INTENT in s.notes for s in diagnoses):
        return PrimaryResolution("not_established", intents=candidates)
    if len(intents) > 1:
        return PrimaryResolution("conflicting_explicit", intents=candidates)
    if intents:
        if _captured(intents[0]):
            return PrimaryResolution("resolved_explicit", intents[0], candidates)
        return PrimaryResolution("unresolved_explicit", intents=candidates)
    captured = [s for s in diagnoses if _captured(s)]
    return (PrimaryResolution("legacy_fallback", captured[0]) if captured
            else PrimaryResolution("not_established"))


def result_primary(result) -> PrimaryResolution:
    """Recheck current objects rather than trusting stale worksheet metadata."""
    return resolve_primary(list(result.provider_card.captured_codes)
                           + list(result.provider_card.declined_codes))
