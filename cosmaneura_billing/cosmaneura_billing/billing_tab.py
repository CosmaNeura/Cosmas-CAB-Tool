"""BillingTabView — the provider-facing billing surface (F1).

Requested by Sherin George (2026-08-10): a separate tab showing the same visit
reorganised as a billing reference, with copy-pasteable ICD and CPT candidates,
the assessment/plan split to code granularity, and **the clinical note left
untouched** — "clinical tool first". Maura Buete (2026-07-24) added the
family-medicine breadth case and the expectation that a suggested code is
criteria-met.

This module is an *adapter*, not a second engine. Every code it renders has
already been through CodingAgent → DocumentationIntegrityAgent →
ComplianceGuardrailAgent → DenialRiskAgent. It reshapes, orders, dedupes and
labels; it never adds a code, never changes a level, and never mutates the note.
That is what makes the promise made to customers true.

Two rules are enforced here rather than assumed:

* **A code never renders without at least one exactly anchored evidence span.**
  The pipeline's verifier intentionally tolerates harmless case and whitespace
  differences, but the provider surface also needs a concrete character range
  to highlight. A code whose verified quote cannot be located exactly in its
  declared source is therefore dropped from this view, not shown bare.
* **Anything not suggested carries a reason.**

  Removed codes still need both their reason and an anchored documentation span.
  A rejection rationale alone is not source evidence. For example, a rejected
  family-history code is visible only when the patient's denial is attached as
  exact decision evidence; otherwise the surface suppresses the code rather
  than rendering an unlocatable assertion.

On confidence: the contract sketch in the requirements synthesis includes a
numeric ``confidence``. This engine is rule-based and produces no calibrated
probability, so the field is present and ``null`` rather than filled with an
invented number — a fabricated 0.92 next to a real evidence span would
misrepresent how the code was chosen.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .completeness import applicable_rulepack, check_note
from .schemas import (
    BillingResult, CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle,
)

# Ordering key for coverage-aware ranking. Lead with what is documented and
# likely to be paid; never lead with a code the payer is likely to reject.
_RISK_RANK = {"low": 0, "unknown": 1, "medium": 2, "high": 3}

_STATUS = {
    Disposition.CAPTURED: "suggested",
    Disposition.REVIEW: "held",
    Disposition.REJECTED: "removed",
}

_PLAN_HEADER = re.compile(r"\b(?:plan|a/p|assessment and plan)\b\s*[:\-]", re.I)
_PLAN_SPLIT = re.compile(r"[;\n]|(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z]{4,}")

# Words too generic to link a plan item to a diagnosis.
_STOPWORDS = frozenset({
    "unspecified", "other", "specified", "disease", "disorder", "chronic",
    "acute", "history", "with", "without", "patient", "type", "left", "right",
    "site", "part", "region", "complication", "complications", "primary",
})


def _anchored_evidence(
    s: CodeSuggestion, bundle: VisitBundle,
) -> list[tuple[Evidence, dict[str, Any]]]:
    """Pair each retained evidence object with its exact source location.

    Upstream verification can accept normalized case, whitespace, punctuation,
    or ellipsis differences. Those are useful for the coding guardrail, but do
    not provide a safe offset for a provider UI. Exact source slices are the
    stricter rendering boundary. Keeping the evidence object paired with its
    span also prevents a filtered item from shifting MDM rationale attribution.
    """
    sources = {
        "signed_note": bundle.signed_note,
        "transcript": bundle.transcript,
        "intake_text": bundle.intake_text,
    }
    out: list[tuple[Evidence, dict[str, Any]]] = []
    for ev in s.evidence:
        text = sources.get(ev.source, "")
        idx = text.find(ev.quote) if text and ev.quote else -1
        if idx < 0:
            continue
        out.append((ev, {
            "source": ev.source,
            "text": ev.quote,
            "loc": {"start": idx, "end": idx + len(ev.quote)},
        }))
    return out


def _spans(s: CodeSuggestion, bundle: VisitBundle) -> list[dict[str, Any]]:
    """Return only evidence spans with exact, highlightable locations."""
    return [span for _, span in _anchored_evidence(s, bundle)]


def _note_term(label: str) -> set[str]:
    return {w for w in _WORD.findall(label.lower()) if w not in _STOPWORDS}


def _plan_items(bundle: VisitBundle) -> list[str]:
    """Plan/assessment items from the signed note, verbatim.

    The note is read, never rewritten — this returns slices of it.
    """
    note = bundle.signed_note or ""
    match = _PLAN_HEADER.search(note)
    section = note[match.end():] if match else ""
    return [i.strip(" -•\t") for i in _PLAN_SPLIT.split(section) if i.strip(" -•\t")]


def _link_plan_items(s: CodeSuggestion, items: list[str]) -> list[str]:
    """The plan items this code justifies (Sherin's A/P decomposition).

    Deliberately conservative: an item is linked only when it shares a
    distinctive word with the code's label or its evidence. An unlinked item is
    left unlinked rather than attached to a plausible-looking code — a wrong
    linkage is worse than none, because linkage is what the denial-risk story
    rests on.
    """
    terms = _note_term(s.label) | {
        w for ev in s.evidence for w in _note_term(ev.quote)}
    if not terms:
        return []
    return [item for item in items if terms & _note_term(item)]


def _renderable(s: CodeSuggestion, bundle: VisitBundle) -> bool:
    """May this code be shown?

    Every visible code needs an exact source anchor. Held and removed codes also
    need an explanation so the surface never presents an unexplained decision.
    """
    if not _spans(s, bundle):
        return False
    if s.disposition != Disposition.CAPTURED:
        return bool(s.rationale)
    return True


def _candidate(
    s: CodeSuggestion, bundle: VisitBundle, plan_items: list[str],
) -> dict[str, Any]:
    status = _STATUS.get(s.disposition, "held")
    coverage = next((n.split(":", 1)[1] for n in s.notes
                     if n.startswith("coverage:")), None)
    return {
        "code": s.code,
        "description": s.label,
        "status": status,
        # Held and removed always say why. Suggested codes carry their rationale
        # too — it is the "why this code" a provider opens.
        "status_reason": s.rationale if status != "suggested" else None,
        "why": s.rationale,
        "confidence": None,          # see module docstring
        "evidence_spans": _spans(s, bundle),
        "coverage_note": coverage,
        "denial_risk": s.denial_risk,
        "alternates": [],            # filled by _dedupe
        "linked_plan_items": _link_plan_items(s, plan_items),
        "copy_text": s.code,
    }


def _dedupe(
    candidates: list[dict[str, Any]], *, preferred_code: Optional[str] = None,
) -> list[dict[str, Any]]:
    """One row per documented finding, with the other codes as alternates.

    Sherin's progesterone-deficiency case: a finding that legitimately maps to
    several codes must appear once. Two candidates are the same finding when they
    cite the same documentation span — that is what "the same finding" means
    here, and it avoids guessing at clinical equivalence.
    """
    by_finding: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for cand in candidates:
        spans = cand["evidence_spans"]
        key = spans[0]["text"].strip().lower() if spans else f"__{cand['code']}"
        if key not in by_finding:
            by_finding[key] = cand
            order.append(key)
            continue
        kept = by_finding[key]
        if cand["code"] != kept["code"] and cand["code"] not in kept["alternates"]:
            if cand["code"] == preferred_code:
                cand["alternates"] = list(dict.fromkeys(
                    [kept["code"], *kept["alternates"], *cand["alternates"]]
                ))
                by_finding[key] = cand
            else:
                kept["alternates"].append(cand["code"])
    return [by_finding[k] for k in order]


def _order(candidates: list[dict[str, Any]], primary: Optional[str]) -> list[dict[str, Any]]:
    """Coverage-aware ordering.

    Lead with the primary candidate, then by denial risk. This orders and labels
    codes that are already evidence-backed; it never adds one to reach coverage
    and never drops a documented one to avoid a non-covered label.
    """
    status_rank = {"suggested": 0, "held": 1, "removed": 2}

    def key(item: tuple[int, dict[str, Any]]) -> tuple:
        idx, cand = item
        return (
            0 if primary and cand["code"] == primary else 1,
            # Codes actually being suggested lead. A held code above a suggested
            # one reads as the engine's recommendation, which it isn't.
            status_rank.get(cand["status"], 1),
            _RISK_RANK.get(cand.get("denial_risk"), 1),
            idx,                      # stable within a tier
        )

    return [c for _, c in sorted(enumerate(candidates), key=key)]


def build_billing_tab_view(
    bundle: VisitBundle,
    result: BillingResult,
    *,
    engine_version: str,
    generated_at: str = "",
) -> dict[str, Any]:
    """Reshape one pipeline result into the provider billing tab contract.

    ``generated_at`` is passed in rather than read from the clock so the output
    is deterministic and can be golden-tested.
    """
    card, ws = result.provider_card, result.biller_worksheet
    plan_items = _plan_items(bundle)

    # Every code the pipeline considered, filtered by what may be shown.
    considered = list(card.captured_codes) + list(card.declined_codes)
    renderable = [s for s in considered if _renderable(s, bundle)]
    withheld_without_evidence = sum(
        1 for suggestion in considered if not _spans(suggestion, bundle)
    )
    withheld_without_reason = sum(
        1 for suggestion in considered
        if _spans(suggestion, bundle)
        and suggestion.disposition != Disposition.CAPTURED
        and not suggestion.rationale
    )

    def bucket(
        kind_set: set[CodeKind], *, preferred_code: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        cands = [_candidate(s, bundle, plan_items)
                 for s in renderable if s.kind in kind_set]
        return _dedupe(cands, preferred_code=preferred_code)

    primary = ws.primary_diagnosis.code if ws.primary_diagnosis else None
    icd = _order(bucket({CodeKind.ICD10}, preferred_code=primary), primary)
    for candidate in icd:
        candidate["primary"] = bool(primary and candidate["code"] == primary)
    cpt = _order(bucket({CodeKind.EM, CodeKind.PROCEDURE, CodeKind.MODIFIER}), None)

    rulepack = applicable_rulepack(bundle.place_of_service, bundle.encounter_type)
    completeness = (
        check_note(bundle.signed_note, rulepack=rulepack).to_dict(
            pos=bundle.place_of_service)
        if rulepack else
        {"rulepack": None, "complete": None, "results": [], "attestation_block": None}
    )

    return {
        "visit_id": bundle.encounter_id,
        "generated_at": generated_at,
        "engine_version": engine_version,
        "em": _em_block(card, ws, bundle),
        "icd_candidates": icd,
        "cpt_candidates": cpt,
        "completeness": completeness,
        # Codes the pipeline considered but that cannot be rendered because their
        # evidence did not verify. Surfaced as a count so the view is honest
        # about what it is not showing, without showing unsupported codes.
        "withheld_without_evidence": withheld_without_evidence,
        # Evidence-backed held/removed codes still require an auditable reason.
        # Keep this distinct so the UI never misstates why a row was suppressed.
        "withheld_without_reason": withheld_without_reason,
        "copy_all": " ".join(
            c["copy_text"] for c in cpt + icd if c["status"] == "suggested"),
        **result.inference,
        "degraded": ({"reason": result.degraded.reason, "detail": result.degraded.detail}
                     if result.degraded else None),
        "clinical_note_modified": False,
    }


def _em_block(card, ws, bundle: VisitBundle) -> Optional[dict[str, Any]]:
    considered = list(card.captured_codes) + list(card.declined_codes)
    s = next((candidate for candidate in considered
              if candidate.kind == CodeKind.EM
              and _renderable(candidate, bundle)), None)
    if s is None:
        return None

    def note(prefix: str) -> Optional[str]:
        return next((n.split(":", 1)[1] for n in s.notes if n.startswith(prefix)), None)

    minutes = note("em_total_time_minutes:")
    return {
        "suggested_level": s.code,
        "provider_selected_level": bundle.submitted_em_code,
        "status": _STATUS.get(s.disposition, "held"),
        "status_reason": s.rationale if s.disposition != Disposition.CAPTURED else None,
        "pathway": note("em_pathway:") or "mdm_2of3",
        "mdm_code": note("em_mdm_code:"),
        "total_time_minutes": int(minutes) if minutes and minutes.isdigit() else None,
        "mdm_facts": [
            {"element": element, "finding": finding,
             "evidence_span": _element_span(s, bundle, element)}
            for element, finding in _em_elements(s).items()
        ],
        "why": s.rationale,
        "evidence_spans": _spans(s, bundle),
        # What the compliance gate did to this code, verbatim from the audit
        # trail — this is the line a payer conversation starts from.
        "guardrail_actions": [line for line in ws.audit_trail
                              if s.code in line and line.split()[0] in
                              ("DOWNGRADE", "CAPTURE", "HOLD", "REJECT")],
        "copy_text": s.code,
    }


def _element_span(
    s: CodeSuggestion, bundle: VisitBundle, element: str,
) -> Optional[dict[str, Any]]:
    """The span supporting one MDM element, or None.

    The coding agent tags each E/M evidence span with what it supports, so the
    right quote can be shown against the right element. Returning None where
    there is no span — the data element currently has none — is the point: a
    reviewer must not see the risk quote presented as evidence for problems.
    """
    wanted = {"risk": "risk", "problems": "problem"}.get(element)
    if not wanted:
        return None
    for ev, span in _anchored_evidence(s, bundle):
        if wanted in (ev.rationale or "").lower():
            return span
    return None


_ELEMENTS_RE = re.compile(r"Elements -> (\{.*\})")


def _em_elements(s: CodeSuggestion) -> dict[str, str]:
    """The three MDM elements and the level each reached, from the rationale."""
    match = _ELEMENTS_RE.search(s.rationale or "")
    if not match:
        return {}
    out = {}
    for element, level in re.findall(r"'(\w+)':\s*'(\w+)'", match.group(1)):
        out[element] = level
    return out
