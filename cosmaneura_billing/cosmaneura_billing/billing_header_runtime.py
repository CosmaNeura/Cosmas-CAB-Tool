"""Presentation adapter over a completed billing pipeline, never a code generator."""
from __future__ import annotations

import re
from uuid import uuid4

from .billing_header import BillingHeaderError, source_documents
from .billing_tab import build_billing_tab_view
from .completeness import RULE_PACKS, applicable_rulepack, check_note


_FACT_LABELS = {
    "visit_type": "Visit type", "patient_type": "Patient type",
    "modality": "Visit modality", "patient_location": "Patient location",
    "place_of_service": "Place of service", "provider_location": "Provider location",
    "consent": "Telehealth consent", "visit_duration": "Visit duration",
    "em_time": "Documented clinician time on the service date",
}
_SOURCE_KIND = {"signed_note": "note", "transcript": "transcript", "intake_text": "intake"}


def generated_header(stored, result, *, engine_version: str) -> dict:
    """Convert verified pipeline output to the approved evidence-linked header.

    Missing structured source provenance is left unspecified. Encounter metadata
    and a code's wording cannot establish note facts, POS, consent, or time.
    Exact spans use Unicode code points, matching Python string coordinates.
    """
    if result.degraded:
        raise BillingHeaderError("generation_unavailable")
    view = build_billing_tab_view(stored.bundle, result, engine_version=engine_version)
    sources = {row["kind"]: row for row in source_documents(stored)}
    evidence, evidence_keys = [], {}

    def add_span(kind, start, end, quote):
        doc = sources.get(kind)
        if (doc is None or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(doc["text"])
                or doc["text"][start:end] != quote):
            return None
        key = (kind, start, end)
        if key not in evidence_keys:
            eid = f"evidence-{len(evidence) + 1}"
            evidence_keys[key] = eid
            evidence.append({
                "id": eid, "source": kind, "sourceId": doc["id"],
                "sourceVersion": doc["version"], "start": start, "end": end,
                "text": quote, "coordinateSystem": "unicode_code_point",
            })
        return evidence_keys[key]

    def quote_evidence(quote):
        text = sources["note"]["text"]
        start = text.find(quote) if quote else -1
        return add_span("note", start, start + len(quote or ""), quote)

    facts = {key: {"key": key, "label": label, "status": "not_documented",
                   "value": None, "evidenceIds": []} for key, label in _FACT_LABELS.items()}

    negative = re.compile(r"\b(?:no|not|denies|denied|declined|without|refused)\b", re.I)
    hypothetical = re.compile(r"\b(?:example|planned|may|might|if|e\.g\.)\b", re.I)
    clauses = [m.group().strip() for m in re.finditer(r"[^.!?;\n]+[.!?;]?", stored.bundle.signed_note)]

    def fact(key, observations):
        # Multiple differing or negated observations are never silently resolved
        # by the first regex hit. Preserve both quotations as a review conflict.
        observations = list(dict.fromkeys(observations))
        if not observations or len(observations) > 10:
            return
        if len(observations) == 1 and negative.search(observations[0]):
            return
        value = " | ".join(observations)
        if len(value) > 600:
            return
        refs = [quote_evidence(quote) for quote in observations]
        if all(refs):
            facts[key].update(status="conflicting" if len(refs) > 1 else "documented",
                              value=value, evidenceIds=list(dict.fromkeys(refs)))

    # Keep complete matched statements rather than extracting a plausible value
    # from an unrelated number, payer rule, visit code, or supporting transcript.
    patterns = {
        "visit_type": r"\b(?:in-person(?: office)?|office|telehealth) visit\b",
        "patient_type": r"\b(?:new|established) patient\b",
        "place_of_service": r"\b(?:place of service|POS)\s*[:=]\s*(?:02|10|11)\b",
        "visit_duration": r"\b(?:visit|session) duration\s*[:=]\s*\d{1,3}\s*minutes\b",
        "em_time": r"\btotal (?:physician|QHP|qualified health care professional) time on (?:the )?(?:date of service|service date)\s*[:=]\s*\d{1,3}\s*minutes\b",
    }
    note = stored.bundle.signed_note
    for key, pattern in patterns.items():
        observations = []
        for clause in clauses:
            if hypothetical.search(clause):
                continue
            matches = list(re.finditer(pattern, clause, re.I))
            if len({m.group().casefold() for m in matches}) > 1:
                observations.extend(m.group() for m in matches)
            elif matches:
                observations.append(clause)
        fact(key, observations)

    pack = applicable_rulepack(place_of_service=stored.bundle.place_of_service,
                               encounter_type=stored.bundle.encounter_type)
    flags = []
    if pack:
        report = check_note(note, rulepack=pack)
        definitions = {rule.rule_id: rule for rule in RULE_PACKS[pack]}
        negative_topics = {"consent": r"\bconsent\b", "modality": r"\b(?:telehealth|video|audio|telephone)\b",
                           "patient_location": r"\bpatient\b.*\b(?:location|located|at|in|from)\b",
                           "provider_location": r"\b(?:provider|clinician)\b.*\b(?:location|located|at|in|from)\b"}
        for rule in report.results:
            key = rule.rule_id.removeprefix("telehealth.")
            # The old duration check finds generic minutes. It cannot establish
            # either actual visit duration or eligible QHP E/M time here.
            if key in {"modality", "patient_location", "provider_location", "consent"}:
                definition = definitions[rule.rule_id]
                observations = [clause for clause in clauses if not hypothetical.search(clause)
                    and (any(pattern.search(clause) for pattern in definition.patterns)
                         or negative.search(clause) and re.search(negative_topics[key], clause, re.I))]
                # Multiple locations in one sentence cannot safely be reduced
                # to one fact by the older completeness regexes.
                if key.endswith("location") and any(len(set(re.findall(
                        r"\b(?:home|residence|workplace|work|office|facility|hospital|school)\b",
                        clause.casefold()))) > 1 for clause in observations):
                    observations = []
                fact(key, observations)
            if rule.status != "present" or (key in facts and facts[key]["status"] != "documented"):
                flags.append({"id": rule.rule_id, "message": rule.fix_hint})
    else:
        flags.append({"id": "completeness_not_covered",
                      "message": "No completeness rule pack covers this visit type; manual review is required."})

    suggestions = []
    candidates = [("ICD10", row) for row in view["icd_candidates"]]
    if view.get("em"):
        em = view["em"]
        candidates.insert(0, ("EM", {**em, "code": em["suggested_level"],
                                     "description": "Office/outpatient E/M"}))
    for kind, row in candidates:
        if row["status"] != "suggested":
            flags.append({"id": f"withheld-{len(flags)}",
                          "message": row.get("status_reason") or "A code requires manual review."})
            continue
        refs = []
        for span in row.get("evidence_spans", []):
            ref = add_span(_SOURCE_KIND.get(span["source"]), span["loc"]["start"],
                           span["loc"]["end"], span["text"])
            if ref and ref not in refs:
                refs.append(ref)
        if not refs:
            continue
        if (kind, row["code"]) in {(s["kind"], s["code"]) for s in suggestions}:
            continue
        suggestions.append({
            "id": f"suggestion-{len(suggestions) + 1}", "kind": kind,
            "code": row["code"], "label": row["description"],
            "codeSet": "CPT" if kind == "EM" else "ICD-10-CM",
            # A configured engine label is not a verified terminology manifest.
            "codeSetVersion": "unverified",
            "reviewStatus": "unreviewed", "evidenceIds": refs,
            "basis": row.get("why") or "Verified billing pipeline output; human review required.",
        })
    if view.get("cpt_candidates"):
        flags.append({"id": "other_services", "message": "Review additional services in the full billing view."})
    return {
        "facts": list(facts.values()), "evidence": evidence, "suggestions": suggestions,
        "flags": flags, "policy": {"status": "not_verified",
            "message": "Payer applicability and billing rules require coder review."},
        "generation": {"engineVersion": engine_version, "runId": str(uuid4()),
                       "inferenceMode": result.inference.get("inferenceMode", "unknown")},
    }
