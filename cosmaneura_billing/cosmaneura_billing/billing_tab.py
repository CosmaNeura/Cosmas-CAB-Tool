"""F1 — Provider Billing Tab adapter (synthesis §F1).

A deterministic transform from one engine run (``VisitBundle`` +
``BillingResult``) to the ``BillingTabView`` JSON the provider-facing tab
renders. Pure function of its inputs: no LLM calls, no engine re-entry, and —
by construction and by golden test — **zero mutation of the clinical note**.

Contract highlights (per the requirements synthesis and groundwork brief):
  * every rendered candidate carries ≥1 evidence span (no span → no render);
    held/removed candidates always carry a ``status_reason``;
  * a finding that maps to multiple codes renders once with ``alternates``;
  * coverage-aware ordering: documented, covered candidates lead — a
    non-covered label never leads. Ordering and labeling only; the engine
    upstream is what decides which codes exist;
  * ``em.pathway`` is ``"mdm_2of3"`` today; the field is the reserved seam for
    ``"total_time"`` (Aug 13 question — do not build the time pathway yet);
  * ``confidence`` is a deterministic render-layer mapping of engine outputs
    (disposition + denial risk), not a model probability — documented here so
    nobody mistakes it for one.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from .evidence import verify_quote
from .schemas import BillingResult, CodeKind, CodeSuggestion, Disposition, VisitBundle

_SOURCE_FIELDS = ("signed_note", "transcript", "intake_text")

_SOURCE_LABEL = {"signed_note": "note", "transcript": "transcript",
                 "intake_text": "intake", "vitals": "vitals"}
_STATUS = {Disposition.CAPTURED: "suggested", Disposition.REVIEW: "held",
           Disposition.REJECTED: "removed"}
_STATUS_RANK = {"suggested": 0, "held": 1, "removed": 2}
_CONFIDENCE = {  # deterministic: (status, denial_risk) -> confidence
    ("suggested", "low"): 0.9, ("suggested", "medium"): 0.8,
    ("suggested", "high"): 0.7, ("suggested", "unknown"): 0.85,
    ("held", None): 0.5, ("removed", None): 0.1,
}


def _confidence(status: str, denial_risk: str) -> float:
    if status == "suggested":
        return _CONFIDENCE.get(("suggested", denial_risk), 0.85)
    return _CONFIDENCE[(status, None)]


def _locate(bundle: VisitBundle, source: str, quote: str) -> Optional[str]:
    """Character offsets of a quote inside one source, or None. Handles
    '...'-elided quotes by locating the first part — the render layer must
    never be stricter than the evidence verifier that already accepted the
    quote, or a legitimate row would silently disappear."""
    text = getattr(bundle, source, "") or ""
    if not quote:
        return None
    label = _SOURCE_LABEL.get(source, source)
    idx = text.find(quote)
    if idx >= 0:
        return f"{label}:{idx}-{idx + len(quote)}"
    head = next((p.strip() for p in re.split(r"\.\.\.|…", quote) if p.strip()), "")
    if head:
        idx = text.find(head)
        if idx >= 0:
            return f"{label}:{idx}-{idx + len(head)}"
        idx = text.lower().find(head.lower())
        if idx >= 0:
            return f"{label}:{idx}-{idx + len(head)}"
    return None


def _find_source(bundle: VisitBundle, quote: str) -> Optional[str]:
    """Which document actually contains this quote, using the same tolerant
    matcher the compliance pipeline uses (case/whitespace/ellipsis aware).
    Never trust a hardcoded source label."""
    if not quote:
        return None
    for source in _SOURCE_FIELDS:                    # exact wins first
        if quote in (getattr(bundle, source, "") or ""):
            return source
    ok, best = verify_quote(quote, {s: getattr(bundle, s, "") or ""
                                    for s in _SOURCE_FIELDS})
    return best if ok else None


def _span(bundle: VisitBundle, quote: str) -> Optional[dict]:
    """A rendered evidence span located in whichever source holds the quote."""
    source = _find_source(bundle, quote)
    if source is None:
        return None
    return {"source": _SOURCE_LABEL.get(source, source), "text": quote,
            "loc": _locate(bundle, source, quote)}


def _spans_from_evidence(bundle: VisitBundle, s: CodeSuggestion) -> list[dict]:
    """Render the suggestion's verified evidence. The source label is resolved
    against the actual documents rather than trusted from the agent, so a
    transcript quote is never mislabeled as coming from the note."""
    spans = []
    for ev in s.evidence:
        quote = (ev.quote or "").strip()
        if not quote:
            continue
        span = _span(bundle, quote)
        if span is None:                       # keep the claim, drop the false
            span = {"source": _SOURCE_LABEL.get(ev.source, ev.source),
                    "text": quote, "loc": None}     # precision of a bad label
        spans.append(span)
    return spans


def _spans_from_rationale(bundle: VisitBundle, rationale: str) -> list[dict]:
    """A held/removed candidate's defense is often a verbatim quote embedded in
    its rationale (e.g. the patient's explicit denial). Extract quoted
    fragments and keep those that verify against the documentation — using the
    tolerant verifier, so an elided or case-shifted quote still renders its
    row instead of dropping the transparency the provider needs."""
    spans = []
    for quoted in re.findall(r'["“]([^"”]{8,})["”]', rationale or ""):
        span = _span(bundle, quoted.strip())
        if span is not None:
            spans.append(span)
    return spans


def _status_reason(s: CodeSuggestion, status: str) -> Optional[str]:
    if status == "suggested":
        return None
    if s.rationale and ("not" in s.rationale.lower()
                        or "denied" in s.rationale.lower()
                        or "unresolved" in s.rationale.lower()
                        or "confirm" in s.rationale.lower()):
        return s.rationale
    for note in s.notes:
        if note == "not_addressed_in_plan":
            return "Documented but not addressed in the plan — reportable only if assessed/managed."
        if note.startswith("no documentation span"):
            return note
        if note.startswith("preference_hold"):
            return note.split(": ", 1)[-1]
    return s.rationale or "Held for provider confirmation."


def _coverage_note(s: CodeSuggestion) -> Optional[str]:
    for note in s.notes:
        if note.startswith("coverage:"):
            return note.split("coverage:", 1)[1].strip()
    return None


# --------------------------------------------------------------------------- #
# A/P decomposition: link plan tasks to the code that justifies them
# --------------------------------------------------------------------------- #
_AP_BLOCK = re.compile(r"Assessment/Plan:\s*(.+?)(?=Return if|Electronically signed|$)",
                       re.S | re.I)
_AP_ITEM = re.compile(r"\(\d+\)\s*([^;]+)")


def _parse_ap_items(signed_note: str) -> list[tuple[str, list[str]]]:
    """[(head, [task, …]), …] from '(1) Head - task, task;' style A/P text."""
    m = _AP_BLOCK.search(signed_note)
    if not m:
        return []
    items = []
    for chunk in _AP_ITEM.findall(m.group(1)):
        head, _, tail = chunk.partition(" - ")
        tasks = [t.strip(" .;") for t in tail.split(",") if t.strip(" .;")]
        items.append((head.strip(), tasks))
    return items


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{5,}", text.lower())}


def _link_plan_items(bundle: VisitBundle, facts: dict,
                     candidate: dict, suggestion: CodeSuggestion) -> list[str]:
    ap = _parse_ap_items(bundle.signed_note)
    if not ap:
        return []
    label_tokens = _tokens(suggestion.label)
    ev_texts = [sp["text"].lower() for sp in candidate["evidence_spans"]]
    for head, tasks in ap:
        head_tokens = _tokens(head)
        # direct label overlap ("Obesity" ↔ "Obesity, unspecified")
        if head_tokens & label_tokens:
            return tasks
        # via the extracted problem: A/P head names the problem; the problem's
        # verbatim quote overlaps the candidate's evidence span
        for p in facts.get("problems", []):
            if not isinstance(p, dict):
                continue
            if head_tokens & _tokens(str(p.get("name", ""))):
                pq = str(p.get("quote", "")).lower()
                for frag in re.split(r"\s*\.\.\.\s*", pq):
                    frag = frag.strip()
                    if frag and any(frag in ev or ev in frag for ev in ev_texts):
                        return tasks
    return []


# --------------------------------------------------------------------------- #
# candidate assembly: dedupe (same finding → one row + alternates)
# --------------------------------------------------------------------------- #
def _candidate(bundle: VisitBundle, s: CodeSuggestion) -> Optional[dict]:
    status = _STATUS[s.disposition]
    spans = _spans_from_evidence(bundle, s)
    if not spans and status != "suggested":
        spans = _spans_from_rationale(bundle, s.rationale)
    if not spans:
        return None                      # no span, no render — hard rule
    return {
        "code": s.code,
        "description": s.label,
        "status": status,
        "status_reason": _status_reason(s, status),
        "confidence": _confidence(status, s.denial_risk),
        "evidence_spans": spans,
        "coverage_note": _coverage_note(s),
        "alternates": [],
        "linked_plan_items": [],
        "copy_text": s.code,
    }


def _dedupe(rows: list[tuple[dict, CodeSuggestion]]) -> list[tuple[dict, CodeSuggestion]]:
    """Same code twice → merge. Different codes over the identical evidence
    (one finding, multiple legitimate codes) → the best row keeps and the rest
    become ``alternates`` (Sherin's progesterone example: never duplicate
    rows).

    Order-independent by construction: the keeper of an evidence signature is
    chosen by status then primary-candidate flag then code — never by which
    code the model happened to emit first, since emission order is arbitrary
    in production.
    """
    by_code: dict[str, tuple[dict, CodeSuggestion]] = {}
    for row, s in rows:
        if row["code"] in by_code:
            kept_row, kept_s = by_code[row["code"]]
            seen = {sp["text"] for sp in kept_row["evidence_spans"]}
            kept_row["evidence_spans"] += [sp for sp in row["evidence_spans"]
                                           if sp["text"] not in seen]
            if _STATUS_RANK[row["status"]] < _STATUS_RANK[kept_row["status"]]:
                kept_row.update(status=row["status"],
                                status_reason=row["status_reason"],
                                confidence=row["confidence"])
            # union the notes so a primary_candidate flag on either copy
            # survives for ordering and plan-item linking
            merged_notes = list(dict.fromkeys(kept_s.notes + s.notes))
            kept_s.notes[:] = merged_notes
        else:
            by_code[row["code"]] = (row, s)

    # group by evidence signature, then pick the keeper deterministically
    groups: dict[tuple, list[tuple[dict, CodeSuggestion]]] = {}
    order: list[tuple] = []
    for row, s in by_code.values():
        key = (frozenset(sp["text"] for sp in row["evidence_spans"]),
               row["status"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((row, s))

    out: list[tuple[dict, CodeSuggestion]] = []
    for key in order:
        members = sorted(
            groups[key],
            key=lambda pair: (_STATUS_RANK[pair[0]["status"]],
                              "primary_candidate" not in pair[1].notes,
                              pair[0]["code"]),
        )
        keeper_row, keeper_s = members[0]
        for other_row, _ in members[1:]:
            if other_row["code"] not in keeper_row["alternates"]:
                keeper_row["alternates"].append(other_row["code"])
        out.append((keeper_row, keeper_s))
    return out


def _order_key(row: dict, s: CodeSuggestion) -> tuple:
    note = (row["coverage_note"] or "").lower()
    non_covered = "not_covered" in note or "not_supported" in note
    primary = "primary_candidate" in s.notes
    # covered before non-covered (a non-covered label never leads), primary
    # first within covered, then stable by status and code
    return (_STATUS_RANK[row["status"]], non_covered, not primary, row["code"])


# --------------------------------------------------------------------------- #
# the view
# --------------------------------------------------------------------------- #
def build_billing_tab_view(
    bundle: VisitBundle,
    result: BillingResult,
    *,
    generated_at: Optional[str] = None,
    engine_version: str = "cosmaneura-billing/0.1.0",
    completeness: Optional[dict] = None,
) -> dict[str, Any]:
    """BillingTabView JSON for one processed encounter. Deterministic; reads
    the bundle, never writes it."""
    facts = result.facts or {}
    all_suggestions = (
        list(result.provider_card.captured_codes)
        + list(result.provider_card.declined_codes)
    )

    # ---- E/M block ----------------------------------------------------- #
    em_s = next((s for s in all_suggestions if s.kind == CodeKind.EM), None)
    mdm_facts = []
    for p in facts.get("problems", []):
        if isinstance(p, dict):
            quote = str(p.get("quote", "")).strip()
            mdm_facts.append({
                "element": "problems",
                "finding": f"{p.get('name', '')} ({p.get('status', '')})".strip(),
                "evidence_span": _span(bundle, quote) if quote else None,
            })
    for d in facts.get("data_reviewed", []):
        if isinstance(d, dict):
            quote = str(d.get("quote", "")).strip()
            mdm_facts.append({
                "element": "data",
                "finding": str(d.get("item", d.get("category", ""))),
                "evidence_span": _span(bundle, quote) if quote else None,
            })
    risk = facts.get("risk") if isinstance(facts.get("risk"), dict) else {}
    if risk.get("level"):
        quote = str(risk.get("quote", "")).strip()
        # locate in whichever document holds it; fall back to the E/M's own
        # already-verified span rather than asserting a source we didn't check
        span = _span(bundle, quote) if quote else None
        if span is None and em_s is not None:
            span = next(iter(_spans_from_evidence(bundle, em_s)), None)
        mdm_facts.append({
            "element": "risk", "finding": f"management risk: {risk['level']}",
            "evidence_span": span,
        })

    em_status = _STATUS[em_s.disposition] if em_s else None
    em_block = {
        "suggested_level": em_s.code if em_s else None,
        "provider_selected_level": bundle.submitted_em_code,
        "status": em_status,
        # a held/removed E/M carries its reason too — same contract as the
        # ICD/CPT candidates, so the UI never asserts an unearned level
        "status_reason": (_status_reason(em_s, em_status)
                          if em_s is not None and em_status != "suggested"
                          else None),
        "pathway": "mdm_2of3",           # reserved seam: "total_time" (Aug 13)
        "mdm_facts": mdm_facts,
        "guardrail_actions": [line for line in result.biller_worksheet.audit_trail
                              if line.startswith("DOWNGRADE")],
    }

    # ---- ICD / CPT candidates ------------------------------------------ #
    icd_rows, cpt_rows = [], []
    for s in all_suggestions:
        if s.kind == CodeKind.EM:
            continue                     # E/M renders in its own block
        row = _candidate(bundle, s)
        if row is None:
            continue
        (icd_rows if s.kind == CodeKind.ICD10 else cpt_rows).append((row, s))

    icd_rows = _dedupe(icd_rows)
    cpt_rows = _dedupe(cpt_rows)
    icd_rows.sort(key=lambda pair: _order_key(*pair))
    cpt_rows.sort(key=lambda pair: _order_key(*pair))
    for row, s in icd_rows + cpt_rows:
        row["linked_plan_items"] = _link_plan_items(bundle, facts, row, s)

    icd_candidates = [row for row, _ in icd_rows]
    cpt_candidates = [row for row, _ in cpt_rows]

    copy_all = [f"{c['code']} — {c['description']}"
                for c in icd_candidates if c["status"] == "suggested"]
    if em_block["suggested_level"] and em_block["status"] == "suggested":
        copy_all.append(f"{em_block['suggested_level']} — E/M office visit")

    return {
        "visit_id": bundle.encounter_id,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(),
        "engine_version": engine_version,
        "em": em_block,
        "icd_candidates": icd_candidates,
        "cpt_candidates": cpt_candidates,
        "copy_all_text": "\n".join(copy_all),
        "completeness": completeness
        or {"rulepack": "telehealth.v1", "results": [],
            "attestation_block": None},
    }
