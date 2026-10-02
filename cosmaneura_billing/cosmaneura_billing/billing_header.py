"""Evidence-bound generated headers and separate, unreviewed human revisions.

This module never generates/adjudicates codes or authorizes an actor. The runtime
supplies full-pipeline output and a server-resolved actor; stores recheck scope.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import UUID, uuid4

SCHEMA = "provider-billing-header.v1"
FACT_KEYS = ("visit_type", "patient_type", "modality", "patient_location",
             "place_of_service", "provider_location", "consent", "visit_duration", "em_time")
OFFICE_EM = frozenset(("99202", "99203", "99204", "99205", "99212", "99213", "99214", "99215"))
MAX_REVISIONS = 100


class BillingHeaderError(ValueError):
    """Safe static error, without source content or actor details."""
    STATUSES = {"source_changed": 409, "revision_conflict": 409,
                "operation_conflict": 409, "header_not_found": 404,
                "not_authorized": 403, "invalid_header": 422, "invalid_edit": 422,
                "audit_required": 503, "history_limit": 409, "store_invalid": 503,
                "generation_unavailable": 503, "header_unavailable": 404,
                "invalid_signoff": 422, "signoff_unavailable": 503, "review_conflict": 409}

    def __init__(self, code="invalid_header"):
        self.code = code if code in self.STATUSES else "invalid_header"
        self.status_code = self.STATUSES[self.code]
        super().__init__(self.code)


def require(condition, code="invalid_header"):
    if not condition:
        raise BillingHeaderError(code)


def text(value, maximum=500, *, empty=False):
    require(isinstance(value, str) and len(value) <= maximum
            and (empty or bool(value.strip()))
            and not any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127
                        or 0xD800 <= ord(c) <= 0xDFFF for c in value))
    return value


def identifier(value):
    text(value, 160)
    require(re.fullmatch(r"[A-Za-z0-9._:~-]+", value) is not None)
    return value


def uuid(value):
    try:
        require(isinstance(value, str) and str(UUID(value)) == value)
    except (ValueError, AttributeError):
        raise BillingHeaderError() from None
    return value


def closed(value, keys):
    require(type(value) is dict and set(value) == set(keys))
    return value


def sequence(value, maximum):
    require(type(value) is list and len(value) <= maximum)
    return value


def integer(value, minimum=0, maximum=2_147_483_647):
    require(type(value) is int and minimum <= value <= maximum)
    return value


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise BillingHeaderError() from None


def detached(value):
    return json.loads(canonical(value))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256(value):
    text(value, 200_000, empty=True)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    text(value, 40)
    require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value) is not None
            and not value.endswith("-00:00"))
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None and result.utcoffset() is not None)
    except (ValueError, OverflowError):
        raise BillingHeaderError() from None
    return result


@dataclass(frozen=True, repr=False)
class HeaderActor:
    tenant_id: str
    user_id: str
    name: str
    role: str

    def __post_init__(self):
        uuid(self.tenant_id)
        uuid(self.user_id)
        text(self.name, 100)
        require(self.role in ("Provider", "Coder", "Admin"), "not_authorized")

    def to_api(self):
        return {"id": self.user_id, "name": self.name, "role": self.role.lower()}

    def __repr__(self):
        return "HeaderActor(<redacted>)"


def source_documents(stored):
    """Stable local document identities; no claim of a vendor-issued note ID."""
    resource = uuid(stored.resource_id)
    version = str(integer(stored.version, 1))
    documents = []
    for kind, content in (("note", stored.bundle.signed_note),
                          ("transcript", stored.bundle.transcript),
                          ("intake", stored.bundle.intake_text)):
        if content or kind == "note":
            documents.append({"id": f"{resource}:{kind}", "kind": kind,
                              "version": version, "sha256": sha256(content), "text": content})
    return tuple(documents)


def source_snapshot(stored):
    documents = source_documents(stored)
    note = documents[0]
    return {"encounterId": text(stored.bundle.encounter_id, 500),
            "encounterVersion": stored.version, "ingestionSource": stored.source,
            "noteId": note["id"],
            "noteVersion": note["version"], "noteSha256": note["sha256"],
            "supportingSourceDigests": [
                {key: doc[key] for key in ("id", "kind", "version", "sha256")}
                for doc in documents[1:]],
            "bundleSha256": digest(asdict(stored.bundle))}


def check_source(stored, expected_source):
    require(stored is not None, "header_not_found")
    require(type(expected_source) is dict and canonical(expected_source) == canonical(source_snapshot(stored)),
            "source_changed")


def _references(value, known, minimum=0):
    sequence(value, 80)
    require(len(value) >= minimum and all(isinstance(item, str) for item in value))
    require(len(set(value)) == len(value) and set(value) <= known)


def validate_generated(generated, stored):
    """Validate a runtime-only pipeline projection against exact source bytes.

    This is not a replacement for running the billing pipeline. No generated
    payload is accepted directly from a user-facing save request.
    """
    closed(generated, ("facts", "evidence", "suggestions", "flags", "policy", "generation"))
    provenance = closed(generated["generation"], ("engineVersion", "runId", "inferenceMode"))
    text(provenance["engineVersion"], 100)
    uuid(provenance["runId"])
    text(provenance["inferenceMode"], 80)
    documents = {doc["id"]: doc for doc in source_documents(stored)}
    evidence_ids = set()
    for item in sequence(generated["evidence"], 80):
        closed(item, ("id", "source", "sourceId", "sourceVersion", "start", "end", "text", "coordinateSystem"))
        identifier(item["id"])
        require(item["id"] not in evidence_ids)
        evidence_ids.add(item["id"])
        require(item["coordinateSystem"] == "unicode_code_point")
        identifier(item["sourceId"])
        doc = documents.get(item["sourceId"])
        require(doc is not None and item["source"] == doc["kind"]
                and item["sourceVersion"] == doc["version"])
        start, end = integer(item["start"]), integer(item["end"], 1)
        require(start < end <= len(doc["text"]))
        text(item["text"], 2000)
        require(doc["text"][start:end] == item["text"])
    facts = sequence(generated["facts"], len(FACT_KEYS))
    require(len(facts) == len(FACT_KEYS))
    seen = set()
    for fact in facts:
        closed(fact, ("key", "label", "status", "value", "evidenceIds"))
        require(isinstance(fact["key"], str) and fact["key"] in FACT_KEYS and fact["key"] not in seen)
        seen.add(fact["key"])
        text(fact["label"], 100)
        require(fact["status"] in ("documented", "not_documented", "conflicting"))
        if fact["status"] == "not_documented":
            require(fact["value"] is None and fact["evidenceIds"] == [])
        else:
            text(fact["value"], 600)
            _references(fact["evidenceIds"], evidence_ids, 2 if fact["status"] == "conflicting" else 1)
    seen, codes = set(), set()
    for line in sequence(generated["suggestions"], 20):
        closed(line, ("id", "kind", "code", "label", "codeSet", "codeSetVersion", "reviewStatus", "evidenceIds", "basis"))
        identifier(line["id"])
        require(line["id"] not in seen)
        seen.add(line["id"])
        require(line["kind"] in ("EM", "ICD10"))
        text(line["code"], 15)
        require(re.fullmatch(r"[A-Z0-9][A-Z0-9.]{0,14}", line["code"]) is not None)
        key = (line["kind"], line["code"])
        require(key not in codes)
        codes.add(key)
        text(line["label"], 150)
        text(line["basis"], 700)
        text(line["codeSetVersion"], 80)
        require(line["reviewStatus"] == "unreviewed"
                and line["codeSet"] == ("CPT" if line["kind"] == "EM" else "ICD-10-CM"))
        _references(line["evidenceIds"], evidence_ids, 1)
    seen = set()
    for flag in sequence(generated["flags"], 30):
        require(type(flag) is dict)
        closed(flag, ("id", "message", "evidenceIds") if "evidenceIds" in flag else ("id", "message"))
        identifier(flag["id"])
        require(flag["id"] not in seen)
        seen.add(flag["id"])
        text(flag["message"], 700)
        if "evidenceIds" in flag:
            _references(flag["evidenceIds"], evidence_ids, 1)
    policy = closed(generated["policy"], ("status", "message"))
    require(policy["status"] == "not_verified")
    text(policy["message"], 700)
    return detached(generated)


def make_header(generated, stored, actor, operation_id, at):
    timestamp(at)
    uuid(operation_id)
    body = validate_generated(generated, stored)
    return {"schemaVersion": SCHEMA, "headerId": str(uuid4()),
            "requestId": operation_id, "generatedAt": at, "createdBy": actor.to_api(),
            "expectedSource": source_snapshot(stored), "reviewStatus": "unreviewed", **body}


def code_fields(line):
    result = {key: detached(line[key]) for key in ("id", "kind", "code", "label", "evidenceIds")}
    result["evidenceIds"] = sorted(result["evidenceIds"])
    return result


def create_draft(header, revision=None):
    return detached(revision["draft"]) if revision else {
        "facts": {fact["key"]: fact["value"] or "" for fact in header["facts"]},
        "codes": [code_fields(line) for line in header["suggestions"]], "note": ""}


def normalize_draft(header, draft):
    try:
        closed(draft, ("facts", "codes", "note"))
        closed(draft["facts"], FACT_KEYS)
        facts = {key: text(draft["facts"][key], 600, empty=True) for key in FACT_KEYS}
        note = text(draft["note"], 2000, empty=True)
        known = {item["id"] for item in header["evidence"]}
        originals = {line["id"]: code_fields(line) for line in header["suggestions"]}
        ids, codes, result = set(), set(), []
        for line in sequence(draft["codes"], 20):
            closed(line, ("id", "kind", "code", "label", "evidenceIds"))
            identifier(line["id"])
            require(line["id"] not in ids and line["kind"] in ("EM", "ICD10"))
            ids.add(line["id"])
            text(line["code"], 15)
            text(line["label"], 150)
            _references(line["evidenceIds"], known, 1)
            normalized = code_fields(line)
            normalized["evidenceIds"] = sorted(normalized["evidenceIds"])
            original = originals.get(line["id"])
            unchanged = original is not None and {**original, "evidenceIds": sorted(original["evidenceIds"])} == normalized
            if not unchanged:
                normalized["code"] = normalized["code"].strip().upper()
                code = normalized["code"]
                require(code in OFFICE_EM if line["kind"] == "EM" else
                        re.fullmatch(r"[A-Z][0-9][A-Z0-9](?:\.[A-Z0-9]{1,4})?", code) is not None)
            key = (normalized["kind"], normalized["code"])
            require(key not in codes)
            codes.add(key)
            result.append(normalized)
        return {"facts": facts, "codes": result, "note": note}
    except (BillingHeaderError, TypeError, KeyError):
        raise BillingHeaderError("invalid_edit") from None


def changes_between(header, before, after):
    changes = []
    for fact in header["facts"]:
        key = fact["key"]
        if before["facts"][key] != after["facts"][key]:
            changes.append({"path": f"facts.{key}", "label": fact["label"],
                            "before": before["facts"][key], "after": after["facts"][key]})
    old, new = ({line["id"]: line for line in view["codes"]} for view in (before, after))
    for key in dict.fromkeys((*old, *new)):
        if old.get(key) != new.get(key):
            changes.append({"path": f"codes.{key}", "label": "Human code proposal",
                            "before": old.get(key), "after": new.get(key)})
    if before["note"] != after["note"]:
        changes.append({"path": "note", "label": "Note to reviewer",
                        "before": before["note"], "after": after["note"]})
    return detached(changes)


def make_revision(header, previous, draft, actor, reason, expected_revision, at):
    require(type(expected_revision) is int and expected_revision == (previous["number"] if previous else 0), "revision_conflict")
    require(expected_revision < MAX_REVISIONS, "history_limit")
    try:
        text(reason, 1000)
        timestamp(at)
    except BillingHeaderError:
        raise BillingHeaderError("invalid_edit") from None
    require(timestamp(at) >= timestamp(previous["createdAt"] if previous else header["generatedAt"]), "invalid_edit")
    normalized = normalize_draft(header, draft)
    changes = changes_between(header, create_draft(header, previous), normalized)
    require(bool(changes), "invalid_edit")
    return {"id": str(uuid4()), "number": expected_revision + 1,
            "headerId": header["headerId"], "expectedSource": detached(header["expectedSource"]),
            "actor": actor.to_api(), "createdAt": at, "reason": reason,
            "reviewStatus": "unreviewed", "draft": normalized, "changes": changes}


def view(header, revisions):
    revision = detached(revisions[-1]) if revisions else None
    if revision:
        revision["history"] = [{key: detached(entry[key]) for key in
                                ("id", "number", "actor", "createdAt", "reason", "changes")}
                               for entry in revisions]
    return {"header": detached(header), "revision": revision,
            "revisionNumber": len(revisions)}
