"""Evidence-span verification — quotes must actually exist in the documentation.

The whole compliance story rests on ``CodeSuggestion.evidence`` pointing at real
documentation, but the quotes are produced by an LLM. An extraction model that
hallucinates (or paraphrases) a quote would otherwise manufacture "evidence" that
no auditor could find in the chart. This module closes that gap:

  * every Evidence.quote is searched for in the actual VisitBundle text
    (intake, transcript, signed note), tolerantly — case, whitespace and
    punctuation-style differences are ignored, and quotes elided with "..."
    are checked part by part;
  * an unverifiable quote is REMOVED from the suggestion (and audited). A
    suggestion stripped of all its evidence then fails the guardrail's
    evidence gate and is held for provider review rather than captured;
  * a quote found in a different source than labeled is relabeled (and
    audited), so the audit trail always points where the text really lives.

Verification is deliberately conservative: a quote must match a contiguous span
at >= MATCH_THRESHOLD similarity. Paraphrase is not evidence.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .schemas import CodeSuggestion, VisitBundle

MATCH_THRESHOLD = 0.90
_ELLIPSIS = re.compile(r"\.\.\.|…")
_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return _WS.sub(" ", text).strip()


def _best_span_score(part: str, source_text: str) -> float:
    """Similarity of the best contiguous span of ``source_text`` to ``part``.

    Uses the longest matching block relative to the quote length: an exact
    (normalized) substring scores 1.0; a paraphrase that only shares a prefix
    scores by the shared span only, so it fails the threshold.
    """
    if not part or not source_text:
        return 0.0
    m = SequenceMatcher(None, part, source_text, autojunk=False)
    match = m.find_longest_match(0, len(part), 0, len(source_text))
    return match.size / len(part)


def verify_quote(quote: str, sources: dict[str, str]) -> tuple[bool, str | None]:
    """Return (verified, best_source_name). Every '...'-separated part of the
    quote must be found (>= MATCH_THRESHOLD) in a single source."""
    parts = [_normalize(p) for p in _ELLIPSIS.split(quote) if _normalize(p)]
    if not parts:
        return False, None
    norm_sources = {name: _normalize(text) for name, text in sources.items() if text}
    best_source, best_score = None, 0.0
    for name, text in norm_sources.items():
        score = min(_best_span_score(p, text) for p in parts)
        if score > best_score:
            best_source, best_score = name, score
    return best_score >= MATCH_THRESHOLD, best_source


def verify_suggestion_evidence(
    bundle: VisitBundle, suggestions: list[CodeSuggestion]
) -> list[str]:
    """Verify (in place) every evidence span on every suggestion.

    Unverifiable evidence is removed and noted; mislabeled sources are fixed.
    Returns audit lines for the worksheet's audit trail.
    """
    sources = {
        "intake_text": bundle.intake_text,
        "transcript": bundle.transcript,
        "signed_note": bundle.signed_note,
    }
    audit: list[str] = []
    for s in suggestions:
        kept = []
        for ev in s.evidence:
            verified, found_in = verify_quote(ev.quote, sources)
            if not verified:
                audit.append(
                    f"EVIDENCE-REMOVED {s.code}: quote not found in documentation "
                    f"(\"{ev.quote[:60]}\")."
                )
                s.notes.append("evidence removed: quote not found in documentation")
                continue
            if found_in and ev.source != found_in and ev.source in sources:
                audit.append(
                    f"EVIDENCE-RELABELED {s.code}: source corrected "
                    f"{ev.source} -> {found_in}."
                )
                ev.source = found_in
            elif found_in and ev.source not in sources:
                # e.g. "vitals" — point it at the text that actually contains it
                ev.source = found_in
            kept.append(ev)
        s.evidence = kept
    return audit
