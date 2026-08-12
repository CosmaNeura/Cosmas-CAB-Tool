"""F2 — Note Completeness Checker (first rule pack: ``telehealth.v1``).

Grace (8/6): "run something that… reviews their note and says, hey, you forgot
this, you forgot this… and maybe they can override it." Root cause on record:
Optum/UHC denials because notes didn't clearly say it was a telehealth visit,
how long, and where.

Determinism boundary (CLAUDE.md constraint #9): an LLM may be used only to
*locate* candidate spans; deterministic Python decides
``present | missing | ambiguous`` and **fails closed** — an unverifiable or
off-note candidate span is ignored, an ambiguous mention never passes, and a
rule with no verified evidence is missing.

Overrides: the provider may override a failed rule, but only with a reason;
every override is logged and returned as a FeedbackEvent for the feedback
stream. When every rule passes or is overridden, ``attestation_block`` emits a
ready-to-insert header for the top of the note. This runs provider-side,
*before* the claim reaches billers — that is how it reduces, not adds, coder
minutes.

``telehealth.v1`` covers: modality, duration, patient location, provider
location. Payer-required consent language is shipped as configuration
(``require_consent``), OFF until the Aug 13 payer-list answer (§8).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .schemas import FeedbackEvent, FeedbackKind, FeedbackTarget

STATUS_PRESENT = "present"
STATUS_MISSING = "missing"
STATUS_AMBIGUOUS = "ambiguous"

# a locator proposes candidate spans; deterministic code adjudicates them
Locator = Callable[[str, str], list[str]]   # (rule_id, note) -> candidate texts


@dataclass
class RuleResult:
    rule_id: str
    status: str
    evidence: Optional[dict]           # {"text": …, "loc": "note:a-b"} | None
    fix_hint: str
    provider_override: dict = field(default_factory=lambda: {
        "overridden": False, "reason": None})

    @property
    def passes(self) -> bool:
        return self.status == STATUS_PRESENT or self.provider_override["overridden"]

    def to_json(self) -> dict:
        return {"rule_id": self.rule_id, "status": self.status,
                "evidence": self.evidence, "fix_hint": self.fix_hint,
                "provider_override": self.provider_override}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    fix_hint: str
    strong: tuple[str, ...]            # regexes proving the element is stated
    weak: tuple[str, ...] = ()         # regexes that only hint → ambiguous
    validate: Optional[Callable[[re.Match], bool]] = None


def _duration_plausible(m: re.Match) -> bool:
    try:
        return 1 <= int(m.group(1)) <= 240
    except (ValueError, IndexError):
        return False


TELEHEALTH_V1_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="telehealth.modality",
        fix_hint="State the visit modality explicitly (e.g. \"telehealth video visit\" or \"audio-only telehealth\").",
        strong=(r"\btelehealth\b", r"\btelemedicine\b",
                r"\bvideo (?:visit|call|encounter)\b", r"\bvirtual visit\b",
                r"\bvia video\b", r"\baudio[- ]only\b"),
        weak=(r"\b(?:tele)?phone (?:call|visit)\b", r"\bspoke by phone\b"),
    ),
    Rule(
        rule_id="telehealth.duration",
        fix_hint="State total visit time in minutes.",
        strong=(r"\b(\d{1,3})\s*(?:minutes?|mins?)\b",
                r"\btotal time[:\s]+(\d{1,3})\b"),
        validate=_duration_plausible,
    ),
    Rule(
        rule_id="telehealth.patient_location",
        fix_hint="Document where the patient was located during the visit (e.g. \"patient located at home in NH\").",
        strong=(r"\bpatient (?:is |was )?locat(?:ed|ion)\b",
                r"\bpatient (?:at|in) (?:home|work|school)\b",
                r"\bfrom (?:home|their home|his home|her home)\b"),
    ),
    Rule(
        rule_id="telehealth.provider_location",
        fix_hint="Document where the provider was located during the visit (e.g. \"provider located at the clinic\").",
        strong=(r"\bprovider (?:is |was )?locat(?:ed|ion)\b",
                r"\bclinician (?:is |was )?locat(?:ed|ion)\b",
                r"\bprovider (?:at|in) (?:the )?(?:office|clinic|home)\b"),
    ),
)

RULE_CONSENT = Rule(
    rule_id="telehealth.consent",
    fix_hint="Document that the patient consented to a telehealth visit (payer-required).",
    strong=(r"\bconsent(?:ed)? (?:to|for) telehealth\b",
            r"\bverbal consent\b", r"\bconsent obtained\b"),
)


@dataclass(frozen=True)
class RulePack:
    name: str
    rules: tuple[Rule, ...]


def telehealth_v1(*, require_consent: bool = False) -> RulePack:
    """Consent stays OFF until the Aug 13 payer list lands (config, not code)."""
    rules = TELEHEALTH_V1_RULES + ((RULE_CONSENT,) if require_consent else ())
    return RulePack(name="telehealth.v1", rules=rules)


# --------------------------------------------------------------------------- #
# the checker — deterministic adjudication, fail closed
# --------------------------------------------------------------------------- #
def _first_verified_match(note: str, patterns: tuple[str, ...],
                          validate) -> Optional[re.Match]:
    for pat in patterns:
        for m in re.finditer(pat, note, re.IGNORECASE):
            if validate is None or validate(m):
                return m
    return None


def _adjudicate(rule: Rule, note: str,
                candidates: list[str]) -> tuple[str, Optional[dict]]:
    # locator candidates count only if they verify against the note AND match
    # the rule's own strong patterns — everything else is ignored (fail closed)
    for cand in candidates:
        cand = (cand or "").strip()
        idx = note.find(cand) if cand else -1
        if idx < 0:
            continue
        if _first_verified_match(cand, rule.strong, rule.validate):
            return STATUS_PRESENT, {"text": cand,
                                    "loc": f"note:{idx}-{idx + len(cand)}"}
    m = _first_verified_match(note, rule.strong, rule.validate)
    if m is not None:
        return STATUS_PRESENT, {"text": m.group(0),
                                "loc": f"note:{m.start()}-{m.end()}"}
    w = _first_verified_match(note, rule.weak, None) if rule.weak else None
    if w is not None:
        return STATUS_AMBIGUOUS, {"text": w.group(0),
                                  "loc": f"note:{w.start()}-{w.end()}"}
    return STATUS_MISSING, None


def check_note(note: str, pack: Optional[RulePack] = None,
               locator: Optional[Locator] = None) -> list[RuleResult]:
    """Evaluate the rule pack over the note. ``locator`` is the optional LLM
    span-finder seam; adjudication is deterministic regardless."""
    pack = pack or telehealth_v1()
    results = []
    for rule in pack.rules:
        candidates: list[str] = []
        if locator is not None:
            try:
                candidates = list(locator(rule.rule_id, note) or [])
            except Exception:
                candidates = []          # a broken locator can't fail open
        status, evidence = _adjudicate(rule, note, candidates)
        results.append(RuleResult(rule_id=rule.rule_id, status=status,
                                  evidence=evidence, fix_hint=rule.fix_hint))
    return results


def is_complete(results: list[RuleResult]) -> bool:
    return all(r.passes for r in results)


# --------------------------------------------------------------------------- #
# provider override — requires a reason, always logged
# --------------------------------------------------------------------------- #
def apply_override(result: RuleResult, *, reason: str,
                   provider_id: str) -> FeedbackEvent:
    """Override a non-passing rule. Refuses empty reasons; returns the
    FeedbackEvent the caller routes into the feedback stream."""
    if result.status == STATUS_PRESENT:
        raise ValueError(f"{result.rule_id} already passed — nothing to override")
    if not (reason or "").strip():
        raise ValueError("override requires a reason")
    if not (provider_id or "").strip():
        raise ValueError("override requires the overriding provider's id")
    result.provider_override = {"overridden": True, "reason": reason.strip(),
                                "provider_id": provider_id.strip()}
    return FeedbackEvent(
        kind=FeedbackKind.DOC_GAP,
        target=FeedbackTarget.DOCUMENTATION_AGENT,
        message=(f"Completeness rule {result.rule_id} overridden by "
                 f"{provider_id}: {reason.strip()}"),
        tone="informational",
        patient_benefit="Keeps the encounter record accurate while capturing why the template rule did not apply.",
    )


# --------------------------------------------------------------------------- #
# attestation header — renders only on pass/override
# --------------------------------------------------------------------------- #
_ATTEST_LABEL = {
    "telehealth.modality": "Modality",
    "telehealth.duration": "Duration",
    "telehealth.patient_location": "Patient location",
    "telehealth.provider_location": "Provider location",
    "telehealth.consent": "Consent",
}


def attestation_block(results: list[RuleResult],
                      pack_name: str = "telehealth.v1") -> Optional[str]:
    """The ready-to-insert note-top block — or None while any rule fails."""
    if not results or not is_complete(results):
        return None
    lines = [f"--- Telehealth attestation ({pack_name}) ---"]
    for r in results:
        label = _ATTEST_LABEL.get(r.rule_id, r.rule_id)
        if r.status == STATUS_PRESENT:
            lines.append(f"{label}: {r.evidence['text']}")
        else:
            ov = r.provider_override
            lines.append(f"{label}: attested by provider override "
                         f"({ov.get('provider_id', 'provider')}: {ov['reason']})")
    lines.append("--- end attestation ---")
    return "\n".join(lines)
