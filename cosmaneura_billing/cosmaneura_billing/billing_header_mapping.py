"""Human catalog browsing and evidence binding, never inferred code authority."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import re

from .icd10_validity import _current_catalogue

PREFIX = "diagnosis-review-"


def mapping_candidates(header):
    return [flag for flag in header["flags"]
            if re.fullmatch(r"diagnosis-review-[0-9]+", flag["id"])
            and flag.get("evidenceIds")]


@lru_cache(maxsize=1)
def catalog_snapshot():
    catalog = _current_catalogue()
    rows = tuple((entry.code, entry.description) for entry in catalog.billable_entries())
    if not rows or len(rows) > 1_000_000:
        raise ValueError("catalog_unavailable")
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return "icd10cm-sha256:" + digest, rows


def search_catalog(header, candidate_id, query, offset=0):
    from .billing_header import BillingHeaderError, require, text, integer
    require(any(c["id"] == candidate_id for c in mapping_candidates(header)), "invalid_edit")
    text(query, 80)
    terms = re.findall(r"[a-z0-9.]+", query.lower())
    require(2 <= len(query.strip()) <= 80 and 1 <= len(terms) <= 10, "invalid_edit")
    integer(offset, 0, 1_000_000)
    try:
        version, rows = catalog_snapshot()
    except (OSError, ValueError):
        raise BillingHeaderError("catalog_unavailable") from None
    require(offset <= len(rows), "invalid_edit")
    # Lexical catalog matches ordered by code, not clinical relevance or revenue.
    hits = [(code, label) for code, label in rows
            if all(term in (code + " " + code.replace(".", "") + " " + label).lower() for term in terms)]
    page = hits[offset:offset + 20]
    return {"candidateId": candidate_id, "catalogVersion": version,
            "matches": [{"code": code, "label": label[:150], "description": label} for code, label in page],
            "nextOffset": offset + 20 if offset + 20 < len(hits) else None}


def validate_mapping(header, line, *, current=True):
    """Metadata is historical provenance; only save/signoff require current catalog."""
    from .billing_header import closed, require
    mapping = closed(line["mapping"], ("candidateId", "catalogVersion"))
    require(line["kind"] == "ICD10" and line["id"] not in {s["id"] for s in header["suggestions"]})
    candidate = next((c for c in mapping_candidates(header) if c["id"] == mapping["candidateId"]), None)
    require(candidate is not None and sorted(line["evidenceIds"]) == sorted(candidate["evidenceIds"]))
    require(isinstance(mapping["catalogVersion"], str)
            and re.fullmatch(r"icd10cm-sha256:[0-9a-f]{64}", mapping["catalogVersion"]) is not None)
    if current:
        try:
            version, rows = catalog_snapshot()
        except (OSError, ValueError):
            from .billing_header import BillingHeaderError
            raise BillingHeaderError("catalog_unavailable") from None
        require(version == mapping["catalogVersion"], "catalog_changed")
        require(any(code == line["code"] and label[:150] == line["label"] for code, label in rows))


def validate_mappings(header, codes, *, current=True):
    from .billing_header import require
    seen = set()
    for line in codes:
        if "mapping" in line:
            validate_mapping(header, line, current=current)
            require(line["mapping"]["candidateId"] not in seen)
            seen.add(line["mapping"]["candidateId"])
