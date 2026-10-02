"""Display-only visit context from the receipt for this exact stored source.

This is deliberately outside VisitBundle and billing_header.source_snapshot.
Missing historic metadata is not reconstructed from timestamps or note text.
"""
from copy import deepcopy
from datetime import date, datetime
import re

from .billing_header import digest, source_snapshot
from .persistence import PostgresEncounterStore

SCHEMA = "cab.source-visit-context.v1"
ATHENA_FIELDS = ("encounterdate", "status", "stage", "closeddate", "reviewedandsigneddate")
ATHENA_SOURCES = {"athena_staff_review_preview_synthetic", "athena_staff_review_mock", "athena_staff_review_operational"}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def calendar_date(value):
    """Exact ISO calendar day; no timezone/completion-time inference."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("serviceDate must be an ISO calendar date")
    date.fromisoformat(value)
    return value


def athena_visit_metadata(note):
    """Retain decoded observations and their exact response digest at import."""
    return {"metadataSha256": note["metadataSha256"],
            "fields": {key: deepcopy(note["metadata"][key]) for key in ATHENA_FIELDS}}


def _base(stored):
    return {"schemaVersion": SCHEMA, "expectedSource": source_snapshot(stored),
            "origin": "unavailable", "sourceSha256": None, "serviceDate": None,
            "noteStatus": None, "statusMeaning": "unavailable"}


def _same(left, right):
    return digest(left) == digest(right)


def _provider(stored, head):
    result = _base(stored)
    if not head or str(head.get("encounter_uid")) != stored.resource_id or not _same(head.get("header_source"), result["expectedSource"]):
        return result
    manifest = head.get("manifest")
    if not isinstance(manifest, dict) or digest(manifest) != head.get("manifest_sha256"):
        return result
    completion, visit = manifest.get("completion"), manifest.get("visit")
    if not isinstance(completion, dict) or not isinstance(visit, dict):
        return result
    status = completion.get("status")
    if status not in {"completed", "signed"} or completion.get("confirmedByProvider") is not True:
        return result
    service_date = visit.get("serviceDate")
    if service_date is not None:
        try:
            calendar_date(service_date)
        except ValueError:
            return result
    return {**result, "origin": "provider_manifest", "sourceSha256": head["manifest_sha256"],
            "serviceDate": service_date, "noteStatus": status, "statusMeaning": "provider_attested"}


def _observation(fields, key):
    field = fields.get(key)
    if not isinstance(field, dict) or set(field) != {"state", "value"}:
        return None
    value = field["value"]
    if (field["state"] != "present" or not isinstance(value, str) or not 1 <= len(value) <= 128
            or re.search(r"[\x00-\x1f\x7f\ud800-\udfff]", value)):
        return None
    return value


def _athena(stored, receipt):
    result = _base(stored)
    if not isinstance(receipt, dict) or receipt.get("action") != "athena.billing_apply" or stored.version != 1:
        return result
    imported = receipt.get("payload", {}).get("billingImport")
    if not isinstance(imported, dict):
        return result
    b = stored.bundle
    clinical = {"patientAge": b.patient_age, "patientSex": b.patient_sex,
                "encounterType": b.encounter_type, "placeOfService": b.place_of_service,
                "providerSpecialty": b.provider_specialty}
    if (imported.get("encounterId") != b.encounter_id or imported.get("source") != stored.source
            or imported.get("plainTextSha256") != result["expectedSource"]["noteSha256"]
            or not _same(imported.get("clinicalContext"), clinical)):
        return result
    metadata = imported.get("sourceVisitContext")
    if (not isinstance(metadata, dict) or set(metadata) != {"metadataSha256", "fields"}
            or not isinstance(metadata["metadataSha256"], str) or not _HASH.fullmatch(metadata["metadataSha256"])
            or metadata["metadataSha256"] != imported.get("sourceMetadataSha256")
            or not isinstance(metadata["fields"], dict) or set(metadata["fields"]) != set(ATHENA_FIELDS)):
        return result
    fields = metadata["fields"]
    raw_date = _observation(fields, "encounterdate")
    service_date = None
    if raw_date:
        try:
            service_date = calendar_date(raw_date) if "-" in raw_date else (
                datetime.strptime(raw_date, "%m/%d/%Y").date().isoformat()
                if re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", raw_date) else None)
        except ValueError:
            pass
    return {**result, "origin": "athena_import", "sourceSha256": digest(imported),
            "serviceDate": service_date, "noteStatus": _observation(fields, "status"),
            "statusMeaning": "source_reported"}


def read_source_visit_context(stored, *, encounter_store, provider_store=None, athena_store=None):
    """Read under the encounter's existing API authorization and practice RLS.

    Intake flags/admission expiry do not hide previously accepted source data.
    A concurrent new source revision yields unavailable context for the old pin.
    """
    result = _base(stored)
    if stored.source not in ATHENA_SOURCES | {"provider_completed_note"}:
        return result
    context = getattr(encounter_store, "context", None)
    if context is not None:
        with context.connection() as conn:
            row = conn.execute(PostgresEncounterStore._select() + " WHERE practice_id=%s AND encounter_uid=%s::uuid",
                               (context.practice_id, stored.resource_id)).fetchone()
            if not row or not _same(source_snapshot(PostgresEncounterStore._bundle(row)), result["expectedSource"]):
                return result
            if stored.source == "provider_completed_note":
                head = conn.execute("""SELECT h.* FROM provider_handoff_heads h
                    WHERE h.practice_id=%s AND h.encounter_uid=%s::uuid AND EXISTS (
                      SELECT 1 FROM provider_handoff_operations o WHERE o.practice_id=h.practice_id
                      AND o.record_key=h.record_key AND o.receipt->'headerSource'=h.header_source
                      AND o.receipt->>'sourceManifestSha256'=h.manifest_sha256)""",
                    (context.practice_id, stored.resource_id)).fetchone()
                return _provider(stored, head)
            operation = stored.bundle.encounter_id.removeprefix("athena-review-")
            if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", operation):
                return result
            receipt = conn.execute("SELECT action,payload FROM athena_case_review_events WHERE practice_id=%s AND operation_id=%s::uuid",
                                   (context.practice_id, operation)).fetchone()
            return _athena(stored, receipt)
    current = encounter_store.get(stored.bundle.encounter_id)
    if current is None or current.resource_id != stored.resource_id or not _same(source_snapshot(current), result["expectedSource"]):
        return result
    if stored.source == "provider_completed_note" and provider_store is not None:
        return _provider(stored, provider_store.heads.get(stored.bundle.encounter_id))
    if stored.source in ATHENA_SOURCES and athena_store is not None:
        operation = stored.bundle.encounter_id.removeprefix("athena-review-")
        return _athena(stored, athena_store._data["receipts"].get(operation))
    return result
