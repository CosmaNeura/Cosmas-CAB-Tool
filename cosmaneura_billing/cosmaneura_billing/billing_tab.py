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

from .schemas import BillingResult, CodeKind, CodeSuggestion, Disposition, VisitBundle

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
    text = getattr(bundle, source, "") or ""
    idx = text.find(quote)
    if idx < 0:
        return None
    return f"{_SOURCE_LABEL.get(source, source)}:{idx}-{idx + len(quote)}"


def _spans_from_evidence(bundle: VisitBundle, s: CodeSuggestion) -> list[dict]:
    spans = []
    for ev in s.evidence:
        quote = (ev.quote or "").strip()
        if not quote:
            continue
        spans.append({
            "source": _SOURCE_LABEL.get(ev.source, ev.source),
            "text": quote,
            "loc": _locate(bundle, ev.source, quote),
        })
    return spans


def _spans_from_rationale(bundle: VisitBundle, rationale: str) -> list[dict]:
    """A held/removed candidate's defense is often a verbatim quote embedded in
    its rationale (e.g. the patient's explicit denial). Extract quoted
    fragments and keep only those that verify against the documentation."""
    spans = []
    for quoted in re.findall(r'"([^"]{8,})"', rationale or ""):
        for source in ("signed_note", "intake_text", "transcript"):
            loc = _locate(bundle, source, quoted)
            if loc is not None:
                spans.append({"source": _SOURCE_LABEL[source], "text": quoted,
                              "loc": loc})
                break
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
    (one finding, multiple legitimate codes) → best row keeps, rest become
    ``alternates`` (Sherin's progesterone example: never duplicate rows)."""
    by_code: dict[str, tuple[dict, CodeSuggestion]] = {}
    for row, s in rows:
        if row["code"] in by_code:
            kept = by_code[row["code"]][0]
            seen = {sp["text"] for sp in kept["evidence_spans"]}
            kept["evidence_spans"] += [sp for sp in row["evidence_spans"]
                                       if sp["text"] not in seen]
            if _STATUS_RANK[row["status"]] < _STATUS_RANK[kept["status"]]:
                kept.update(status=row["status"],
                            status_reason=row["status_reason"],
                            confidence=row["confidence"])
        else:
            by_code[row["code"]] = (row, s)

    unique = list(by_code.values())
    by_signature: dict[frozenset, tuple[dict, CodeSuggestion]] = {}
    out: list[tuple[dict, CodeSuggestion]] = []
    for row, s in unique:
        sig = frozenset(sp["text"] for sp in row["evidence_spans"])
        prior = by_signature.get(sig)
        if prior is not None and row["status"] == prior[0]["status"]:
            prior[0]["alternates"].append(row["code"])
        else:
            by_signature[sig] = (row, s)
            out.append((row, s))
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
                "evidence_span": ({"source": "intake", "text": quote,
                                   "loc": _locate(bundle, "intake_text", quote)}
                                  if quote else None),
            })
    for d in facts.get("data_reviewed", []):
        if isinstance(d, dict):
            mdm_facts.append({"element": "data",
                              "finding": str(d.get("item", d.get("category", ""))),
                              "evidence_span": None})
    risk = facts.get("risk") if isinstance(facts.get("risk"), dict) else {}
    if risk.get("level"):
        quote = str(risk.get("quote", "")).strip()
        mdm_facts.append({
            "element": "risk", "finding": f"management risk: {risk['level']}",
            "evidence_span": ({"source": "note", "text": quote,
                               "loc": _locate(bundle, "signed_note", quote)}
                              if quote else None),
        })

    em_block = {
        "suggested_level": em_s.code if em_s else None,
        "provider_selected_level": bundle.submitted_em_code,
        "status": _STATUS[em_s.disposition] if em_s else None,
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
