"""Note completeness checker — deterministic rule packs over a note, pre-submission.

Why
---
Grace Targonski, MCD Director of Billing, 2026-08-06, verbatim: *"run something
that… reviews their note and says, hey, you forgot this, you forgot this… and
maybe they can override it."* The root cause on record is hard denials from
Optum/UHC because telehealth notes didn't clearly state that the visit was
telehealth, how long it ran, and where each party was. Reconfirmed 2026-08-13,
with the placement specified: a standardized blurb at the *top* of the note, and
a 24–48 hour window in which telehealth documentation must be complete to be
billable at all.

Where this sits in the workflow is the whole point. Athena derives the billing
slip from the provider's note, so a gap fixed in the note is a gap that never
reaches a coder. That is how this feature satisfies the billing team's standing
constraint — net coder minutes must go *down*, never up.

Design
------
Deterministic Python decides ``present`` / ``missing`` / ``ambiguous``; there is
no model call here. A rule that cannot find its evidence says ``missing`` rather
than guessing, so the checker never reports a note complete on the strength of
something it didn't actually find.

Providers can override any result, but an override requires a reason and is
logged — the reason is what tells the next rule-pack revision which rule is
wrong. The attestation block renders only once every rule passes or is
overridden.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

Status = Literal["present", "missing", "ambiguous"]

# Place-of-service codes that make a visit telehealth, and their plain meaning.
TELEHEALTH_POS = {
    "02": "Telehealth provided other than in the patient's home",
    "10": "Telehealth provided in the patient's home",
}

# The billability window from the Aug 13 follow-up. Documentation completed
# after this is not merely denial-prone — it can fall out of billability.
TELEHEALTH_DOCUMENTATION_HOURS = 48


@dataclass
class Rule:
    rule_id: str
    # What the provider is being asked to state, in their words not ours.
    asks_for: str
    fix_hint: str
    patterns: tuple[re.Pattern[str], ...]
    # Patterns that look like the right topic but don't actually state the fact.
    # A hit here with no primary hit is `ambiguous`: something is there, but it
    # would not satisfy a payer.
    weak_patterns: tuple[re.Pattern[str], ...] = ()


@dataclass
class RuleResult:
    rule_id: str
    status: Status
    asks_for: str
    fix_hint: str
    evidence: Optional[str] = None
    overridden: bool = False
    override_reason: Optional[str] = None

    @property
    def satisfied(self) -> bool:
        return self.status == "present" or self.overridden

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "asks_for": self.asks_for,
            "evidence": self.evidence,
            "fix_hint": self.fix_hint,
            "provider_override": {
                "overridden": self.overridden,
                "reason": self.override_reason,
            },
        }


# --------------------------------------------------------------------------- #
# telehealth.v1 — the payers actually denying MCD's claims
# --------------------------------------------------------------------------- #
_TELEHEALTH_V1 = (
    Rule(
        rule_id="telehealth.modality",
        asks_for="that this was a telehealth visit, and how it was conducted",
        fix_hint=("State the modality explicitly, e.g. \"This visit was conducted "
                  "via two-way audio-video telehealth.\""),
        # The rule asks *how* the visit was conducted, so a match needs a modality
        # descriptor. Accepting "telehealth" near "visit" let an unrelated
        # sentence — "Patient consented to this telehealth visit" — satisfy it,
        # and a note missing its modality line then reported complete.
        patterns=(
            re.compile(r"\b(?:two[- ]way )?audio[- ]video\b", re.I),
            re.compile(r"\bvideo (?:visit|encounter|call)\b", re.I),
            re.compile(r"\baudio[- ]only\b", re.I),
            re.compile(r"\btelephone (?:visit|encounter)\b", re.I),
            re.compile(r"\b(?:conducted|performed|held|completed|seen)\b[^.\n]{0,30}"
                       r"\b(?:via|by|using|over)\b[^.\n]{0,25}"
                       r"\b(?:telehealth|video|telephone)\b", re.I),
        ),
        weak_patterns=(re.compile(r"\b(?:telehealth|virtual|remote)\b", re.I),),
    ),
    Rule(
        rule_id="telehealth.duration",
        asks_for="how long the visit took",
        fix_hint=("State total time on the date of the encounter in minutes, e.g. "
                  "\"Total time spent on the date of the encounter: 25 minutes.\""),
        patterns=(
            re.compile(r"\b\d{1,3}\s*(?:minutes|mins|min)\b", re.I),
            re.compile(r"\btotal time\b.{0,30}?\b\d{1,3}\b", re.I),
        ),
        weak_patterns=(
            re.compile(r"\btotal time\b", re.I),
            re.compile(r"\b(?:duration|time spent)\b", re.I),
        ),
    ),
    Rule(
        rule_id="telehealth.patient_location",
        asks_for="where the patient was during the visit",
        fix_hint=("State the patient's physical location, e.g. \"Patient was at "
                  "home in Nashua, NH.\""),
        # A match must name a place. "Patient location on file" mentions the
        # topic and states nothing, so it is ambiguous rather than present —
        # otherwise a note with no location at all reports complete.
        patterns=(
            re.compile(r"\bpatient\b[^.\n]{0,40}\b(?:at|in|from)\b[^.\n]{0,15}"
                       r"\b(?:home|residence|workplace|work|office|facility|"
                       r"hospital|school)\b", re.I),
            re.compile(r"\bpatient location\s*[:\-]\s*\S+", re.I),
            re.compile(r"\bpatient\b[^.\n]{0,40}\b(?:located|physically)\b"
                       r"[^.\n]{0,40}\b[A-Z]{2}\b"),
        ),
        weak_patterns=(
            re.compile(r"\bpatient location\b", re.I),
            re.compile(r"\b(?:at home|from home)\b", re.I),
        ),
    ),
    Rule(
        rule_id="telehealth.provider_location",
        asks_for="where the provider was during the visit",
        fix_hint=("State the provider's physical location, e.g. \"Provider was at "
                  "the St. Petersburg, FL office.\""),
        patterns=(
            re.compile(r"\bprovider\b[^.\n]{0,60}\b(?:was |located )?(?:at|in)\b"
                       r"[^.\n]{0,40}", re.I),
            re.compile(r"\b(?:clinician|physician|I was)\b[^.\n]{0,20}"
                       r"\b(?:located|at|in)\b[^.\n]{0,40}\b(?:office|clinic|home)\b", re.I),
        ),
        weak_patterns=(re.compile(r"\bprovider location\b", re.I),),
    ),
    Rule(
        rule_id="telehealth.consent",
        asks_for="that the patient consented to a telehealth visit",
        fix_hint=("Record consent, e.g. \"Patient verbally consented to this "
                  "telehealth visit.\""),
        patterns=(
            re.compile(r"\b(?:verbal(?:ly)? )?consent(?:ed|s)?\b[^.\n]{0,60}"
                       r"\b(?:telehealth|video|visit|encounter)\b", re.I),
            re.compile(r"\b(?:telehealth|video)\b[^.\n]{0,40}\bconsent(?:ed|s)?\b", re.I),
            re.compile(r"\bagreed to (?:a )?(?:telehealth|video) (?:visit|encounter)\b", re.I),
        ),
        weak_patterns=(re.compile(r"\bconsent\b", re.I),),
    ),
)

RULE_PACKS: dict[str, tuple[Rule, ...]] = {"telehealth.v1": _TELEHEALTH_V1}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _find(rule: Rule, note: str) -> tuple[Status, Optional[str]]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(note or "") if s.strip()]
    for pattern in rule.patterns:
        for sentence in sentences:
            if pattern.search(sentence):
                return "present", sentence
    for pattern in rule.weak_patterns:
        for sentence in sentences:
            if pattern.search(sentence):
                # The topic is mentioned but the fact isn't stated. Reported
                # separately from `missing` so the provider knows to sharpen
                # what's there rather than write something new.
                return "ambiguous", sentence
    return "missing", None


class OverrideRequiresReason(ValueError):
    """An override without a reason is rejected.

    The reason is the whole value of the override: it is what tells the next
    revision of the rule pack that the rule, not the note, was wrong.
    """


@dataclass
class CompletenessReport:
    rulepack: str
    results: list[RuleResult] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(r.satisfied for r in self.results)

    @property
    def outstanding(self) -> list[RuleResult]:
        return [r for r in self.results if not r.satisfied]

    def attestation_block(self, *, pos: str = "", modality: str = "") -> Optional[str]:
        """The ready-to-insert note header, or None while anything is outstanding.

        Withholding it until the note passes is deliberate: an attestation is a
        statement that the facts are documented, so emitting one over a note with
        a known gap would assert something untrue.
        """
        if not self.complete:
            return None
        lines = ["--- Billing attestation ---"]
        if pos:
            lines.append(f"Place of service: {pos}"
                         + (f" ({TELEHEALTH_POS[pos]})" if pos in TELEHEALTH_POS else ""))
        if modality:
            lines.append(f"Modality: {modality}")
        for r in self.results:
            detail = (r.evidence if r.status == "present" and r.evidence
                      else f"provider override — {r.override_reason}")
            lines.append(f"{r.rule_id}: {detail}")
        lines.append(f"Documentation completed within {TELEHEALTH_DOCUMENTATION_HOURS}h "
                     "of the encounter as required for billability.")
        lines.append("--- end attestation ---")
        return "\n".join(lines)

    def to_dict(self, *, pos: str = "", modality: str = "") -> dict:
        return {
            "rulepack": self.rulepack,
            "complete": self.complete,
            "results": [r.to_dict() for r in self.results],
            "attestation_block": self.attestation_block(pos=pos, modality=modality),
        }


def applicable_rulepack(place_of_service: str, encounter_type: str = "") -> Optional[str]:
    """Which pack applies, or None when no pack covers this encounter.

    Only telehealth is covered today. Returning None rather than an empty report
    keeps "this encounter has no rules yet" distinct from "this note passed".
    """
    if place_of_service in TELEHEALTH_POS or "telehealth" in (encounter_type or "").lower():
        return "telehealth.v1"
    return None


def check_note(
    note: str,
    *,
    rulepack: str = "telehealth.v1",
    overrides: Optional[dict[str, str]] = None,
) -> CompletenessReport:
    """Evaluate ``note`` against ``rulepack``.

    ``overrides`` maps rule_id -> reason. A blank or missing reason raises
    ``OverrideRequiresReason``.
    """
    if rulepack not in RULE_PACKS:
        raise KeyError(f"unknown rule pack '{rulepack}'")
    overrides = overrides or {}
    for rule_id, reason in overrides.items():
        if not (reason or "").strip():
            raise OverrideRequiresReason(
                f"override of '{rule_id}' needs a reason: it is the record of why "
                "the rule did not apply to this note.")

    results = []
    for rule in RULE_PACKS[rulepack]:
        status, evidence = _find(rule, note)
        reason = overrides.get(rule.rule_id)
        results.append(RuleResult(
            rule_id=rule.rule_id, status=status, asks_for=rule.asks_for,
            fix_hint=rule.fix_hint, evidence=evidence,
            overridden=bool(reason), override_reason=reason,
        ))
    return CompletenessReport(rulepack=rulepack, results=results)
