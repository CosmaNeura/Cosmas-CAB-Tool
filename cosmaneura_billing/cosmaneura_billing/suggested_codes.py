"""Dormant provider-dashboard adapter for post-pipeline billing suggestions.

The adapter does not run or bypass coding. It accepts a ``BillingResult`` from
``BillingOrchestrator`` and is intentionally not attached to ``api.py``.
"""
from __future__ import annotations

from typing import Any

from .schemas import BillingResult, CodeKind, CodeSuggestion, Disposition, VisitBundle

_PIPELINE_STAGES = (
    "CodingAgent",
    "DocumentationIntegrityAgent",
    "ComplianceGuardrailAgent",
    "DenialRiskAgent",
)
_REVIEW_STATUS = {
    Disposition.REVIEW: "held",
    Disposition.REJECTED: "removed",
}


def _exact_spans(
    suggestion: CodeSuggestion, bundle: VisitBundle,
) -> list[dict[str, Any]]:
    """Return exact declared-source spans; malformed evidence fails closed."""

    sources = {
        "signed_note": bundle.signed_note,
        "transcript": bundle.transcript,
        "intake_text": bundle.intake_text,
    }
    evidence_items = getattr(suggestion, "evidence", ())
    if not isinstance(evidence_items, (list, tuple)):
        return []
    spans: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for evidence in evidence_items:
        source = getattr(evidence, "source", None)
        quote = getattr(evidence, "quote", None)
        text = sources.get(source) if isinstance(source, str) else None
        if not isinstance(text, str) or not isinstance(quote, str) or not quote.strip():
            continue
        start = text.find(quote)
        key = (source, start, start + len(quote))
        if start < 0 or key in seen:
            continue
        seen.add(key)
        spans.append({
            "source": source,
            "text": quote,
            "loc": {"start": start, "end": start + len(quote)},
        })
    return spans


def _valid(suggestion: object) -> bool:
    return (
        isinstance(suggestion, CodeSuggestion)
        and isinstance(suggestion.kind, CodeKind)
        and isinstance(suggestion.code, str)
        and bool(suggestion.code.strip())
        and isinstance(suggestion.label, str)
        and bool(suggestion.label.strip())
    )


def _code_key(suggestion: object) -> tuple[CodeKind, str] | None:
    if not isinstance(suggestion, CodeSuggestion):
        return None
    if not isinstance(suggestion.kind, CodeKind) or not isinstance(suggestion.code, str):
        return None
    return suggestion.kind, suggestion.code


def _worksheet_codes(result: BillingResult) -> list[CodeSuggestion]:
    """Use only the pipeline's post-guardrail claim-ready collections."""

    worksheet = result.biller_worksheet
    return (
        ([worksheet.em_code] if worksheet.em_code is not None else [])
        + list(worksheet.procedures)
        + list(worksheet.modifiers)
        + list(worksheet.diagnoses)
    )


def _base_row(
    suggestion: CodeSuggestion, status: str, spans: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "code": suggestion.code,
        "kind": suggestion.kind.value,
        "description": suggestion.label,
        "status": status,
        "evidence_spans": spans,
    }


def build_suggested_codes_payload(
    bundle: VisitBundle,
    result: BillingResult,
    *,
    engine_version: str,
    generated_at: str = "",
) -> dict[str, Any]:
    """Adapt one completed pipeline run without adding or promoting a code."""

    if len({
        bundle.encounter_id,
        result.provider_card.encounter_id,
        result.biller_worksheet.encounter_id,
    }) != 1:
        raise ValueError("bundle and pipeline result must describe the same encounter")

    counts = dict.fromkeys((
        "suggested_without_exact_evidence",
        "review_without_exact_evidence",
        "review_without_reason",
        "cross_surface_conflict",
        "invalid_pipeline_state",
        "degraded_result",
    ), 0)
    primary = result.biller_worksheet.primary_diagnosis
    primary_code = primary.code if isinstance(primary, CodeSuggestion) else None
    captured = list(result.provider_card.captured_codes)
    declined_keys = {
        key for suggestion in result.provider_card.declined_codes
        if (key := _code_key(suggestion)) is not None
    }

    codes = []
    for suggestion in _worksheet_codes(result):
        if not _valid(suggestion) or suggestion.disposition != Disposition.CAPTURED:
            counts["invalid_pipeline_state"] += 1
            continue
        if suggestion not in captured or _code_key(suggestion) in declined_keys:
            counts["cross_surface_conflict"] += 1
            continue
        spans = _exact_spans(suggestion, bundle)
        if not spans:
            counts["suggested_without_exact_evidence"] += 1
            continue
        row = _base_row(suggestion, "suggested", spans)
        row.update({
            "primary": bool(
                suggestion.kind == CodeKind.ICD10
                and primary_code
                and suggestion.code == primary_code
            ),
            "why": suggestion.rationale or None,
            "denial_risk": suggestion.denial_risk,
            "copy_text": suggestion.code,
            "selectable": True,
        })
        codes.append(row)

    review_items = []
    for suggestion in result.provider_card.declined_codes:
        if not _valid(suggestion) or suggestion.disposition not in _REVIEW_STATUS:
            counts["invalid_pipeline_state"] += 1
            continue
        spans = _exact_spans(suggestion, bundle)
        if not spans:
            counts["review_without_exact_evidence"] += 1
            continue
        reason = (
            suggestion.rationale.strip()
            if isinstance(suggestion.rationale, str)
            else ""
        )
        if not reason:
            counts["review_without_reason"] += 1
            continue
        status = _REVIEW_STATUS[suggestion.disposition]
        row = _base_row(suggestion, status, spans)
        row.update({
            "status_reason": reason,
            "copy_text": None,
            "selectable": False,
            "action": "provider_review_required" if status == "held" else "do_not_use",
        })
        review_items.append(row)

    degraded = (
        {"reason": result.degraded.reason, "detail": result.degraded.detail}
        if result.degraded is not None
        else None
    )
    if degraded is not None:
        counts["degraded_result"] = len(codes) + len(review_items)
        codes, review_items = [], []

    return {
        "visit_id": bundle.encounter_id,
        "generated_at": generated_at,
        "engine_version": engine_version,
        "enabled": False,
        "status": "degraded" if degraded else "ready",
        "pipeline": {
            "source": "BillingResult",
            "stages": list(_PIPELINE_STAGES),
            "advisory_only": True,
        },
        "provider_selected_em_code": bundle.submitted_em_code,
        "codes": codes,
        "review_items": review_items,
        "copy_all": " ".join(row["copy_text"] for row in codes),
        "withheld_counts": counts,
        "human_verification_required": True,
        "degraded": degraded,
        "inference": dict(result.inference or {}),
    }
