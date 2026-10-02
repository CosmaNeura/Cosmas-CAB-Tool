"""Dormant Athena-to-Card-Chase staff review candidates, never operational state.

Only supplied, bounded bytes with an exact server-owned source scope enter this
pure mapper. Legacy Preview remains synthetic; Production is operational and
requires separate read/storage admission in the calling workflow.
The trusted caller owns practice/request binding; context is not authorization.
No CoverageSnapshot, contact route, import receipt, case, consent grant, fresh
eligibility result or human acceptance is created. The presence-only operator
probe remains separate and does not supply values to this module automatically.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from .athena_staff_contract import AthenaStaffScope

from .athena_client import (
    ATHENA_PREVIEW_PRACTICE_ID,
    AthenaCardField,
    AthenaCardRead,
    AthenaClientError,
    decode_athena_card_read,
    build_athena_staff_read_url,
)

SCHEMA_VERSION = "athena-card-chase-review.v1"
MAPPING_VERSION = "athena-card-chase-map.2026-09-30.2"
_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z", re.ASCII,
)
_DATE_FIELDS = frozenset({"birth_date", "issue_date", "policy_expiration_date",
                          "package_expiration_date"})
_ID_FIELDS = frozenset({"registration_department_id", "current_department_id",
                        "policy_record_id", "package_id", "package_payer_id",
                        "category_id", "member_id", "insured_id", "group_number",
                        "relationship_id"})
# Preview returns these documented identifiers as JSON integers. Card/member
# identifiers and group numbers remain exact strings; never infer their zeros.
_NUMERIC_ID_FIELDS = frozenset({"package_id", "category_id", "relationship_id"})
_CONSENT_FIELDS = ("text_consent", "call_consent", "billing_sms_preference",
                   "billing_phone_preference", "billing_email_preference")


class AthenaCardChaseError(ValueError):
    """Static errors never include source values, identifiers or vendor errors."""

    def __init__(self, code: str = "invalid_source") -> None:
        self.code = code if isinstance(code, str) and code in {
            "invalid_context", "invalid_source", "binding_failed",
        } else "invalid_source"
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class CardChaseReadContext:
    """Caller-owned binding, not a patient lookup or a grant of read permission.

    Both supplied responses must come from reads for this exact source patient
    and vendor practice. observed_at is receipt time, not a source version or
    eligibility check time. Reusing a batch ID does not apply/deduplicate data.
    """

    server_practice_id: str
    source_patient_id: str
    batch_id: str
    observed_at: str
    vendor_practice_id: str = ATHENA_PREVIEW_PRACTICE_ID
    synthetic_only: bool = True
    scope: AthenaStaffScope | None = None


@dataclass(frozen=True, repr=False)
class CardChaseReviewCandidate:
    """Immutable generated snapshot; explicit projection returns a fresh copy.

    The review dictionary contains patient/contact/insurance source values and
    belongs only on an authorized staff surface, never logs or a public route.
    This class is an output type, not a validator for caller-created snapshots.
    """

    candidate_id: str
    _json: str

    def to_review_dict(self) -> dict:
        return json.loads(self._json)


def _text(value, maximum: int) -> bool:
    return (isinstance(value, str) and len(value) <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _context(context: CardChaseReadContext) -> dict:
    if type(context) is not CardChaseReadContext:
        raise AthenaCardChaseError("invalid_context")
    if context.scope is not None and type(context.scope) is not AthenaStaffScope:
        raise AthenaCardChaseError("invalid_context")
    production = False
    if context.scope is not None:
        try:
            build_athena_staff_read_url(context.scope, "patient")
        except AthenaClientError:
            raise AthenaCardChaseError("invalid_context") from None
        if (context.scope.profile.server_practice_id != context.server_practice_id
                or context.scope.profile.vendor_practice_id != context.vendor_practice_id
                or context.scope.source_patient_id != context.source_patient_id):
            raise AthenaCardChaseError("invalid_context")
        production = context.scope.profile.environment == "production"
    if production:
        if context.synthetic_only is not False:
            raise AthenaCardChaseError("invalid_context")
    elif (context.synthetic_only is not True
            or context.vendor_practice_id != ATHENA_PREVIEW_PRACTICE_ID
            or not isinstance(context.source_patient_id, str)
            or context.source_patient_id not in {str(i) for i in range(60178, 60185)}):
        raise AthenaCardChaseError("invalid_context")
    for value in (context.server_practice_id, context.batch_id):
        if not _text(value, 128) or not value or value != value.strip():
            raise AthenaCardChaseError("invalid_context")
    value = context.observed_at
    try:
        if (not isinstance(value, str) or not _INSTANT.fullmatch(value)
                or value.endswith("-00:00")):
            raise ValueError()
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        normalized = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise AthenaCardChaseError("invalid_context") from None
    return {"system": "athena_production" if production else "athena_preview", "vendorPracticeId": context.vendor_practice_id,
            "patientId": context.source_patient_id, "serverPracticeId": context.server_practice_id,
            "batchId": context.batch_id, "observedAt": normalized, "syntheticOnly": not production,
            **({"dataClass":"operational"} if production else {})}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def _field(field: AthenaCardField, read: AthenaCardRead) -> dict:
    value, parsed = field.value, None
    if not field.present:
        state = "absent"
    elif field.wire_type == "null":
        state = "null"
    elif isinstance(value, str) and not value.strip():
        state = "empty"
    elif field.name == "sequence":
        if type(value) is int and 1 <= value <= 99:
            state, parsed = "present", value
        elif (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]?", value, re.ASCII)):
            state, parsed = "present", int(value)
        else:
            state = "invalid"
    elif field.name == "cancelled" and type(value) is bool:
        state = "present"
    elif field.name in _NUMERIC_ID_FIELDS and type(value) is int:
        # Keep the raw observation exact on the JavaScript staff surface and
        # expose decimal normalization separately from source value/type.
        if 0 <= value <= 2**53 - 1:
            state, parsed = "present", str(value)
        else:
            state = "invalid"
    elif field.name in _CONSENT_FIELDS and type(value) is bool:
        state, parsed = "present", value
    elif field.wire_type != "string" or not _text(value, 2048):
        state = "invalid"
    elif field.name in _ID_FIELDS and (len(value) > 128 or value != value.strip()):
        state = "invalid"
    elif field.name in _DATE_FIELDS:
        try:
            if not re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", value):
                raise ValueError()
            parsed = datetime.strptime(value, "%m/%d/%Y").date().isoformat()
            state = "present"
        except ValueError:
            state = "invalid"
    else:
        state = "present"
    # Invalid shapes never reflect nested source content. A bounded original
    # scalar remains reviewable, without being substituted into parsedValue.
    safe_value = value if type(value) in {str, int, bool} and (
        not isinstance(value, str) or _text(value, 2048)
    ) else None
    return {"name": field.name, "state": state, "value": safe_value,
            "parsedValue": parsed, "sourceType": field.wire_type,
            "source": {"dataset": read.dataset, "pointer": field.pointer,
                       "sha256": read.sha256}}


def _read_summary(read: AthenaCardRead | None) -> dict:
    if read is None:
        return {"state": "unavailable", "sha256": None, "recordCount": None,
                "totalCount": None, "truncated": False}
    state = "partial" if read.truncated else "complete" if read.records else "empty"
    return {"state": state, "sha256": read.sha256, "recordCount": len(read.records),
            "totalCount": read.total_count, "truncated": read.truncated}


def national_phone_digits(value: str) -> str | None:
    """Recognize bounded national syntax without assigning a country or number."""
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[2-9][0-9]{2}|\([2-9][0-9]{2}\))[-. ]?[2-9][0-9]{2}[-. ]?[0-9]{4}", value,
    ):
        return None
    return re.sub(r"[(). -]", "", value)


def _contact(fields: dict[str, dict]) -> dict:
    candidates = []
    for name, kind in (("mobile_phone", "mobile"), ("home_phone", "home"), ("email", "email")):
        field = fields.get(name)
        status = "unavailable" if field is None else field["state"]
        if status == "present":
            value = field["value"]
            if value.casefold() in {"declined", "none", "unknown", "n/a"}:
                status = "unrecognized_or_declined"
            elif kind == "email":
                status = "plausible" if re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value) else "unrecognized"
            else:
                # Do not assign a country, strip extensions, choose between
                # numbers, or establish ownership from syntactic plausibility.
                status = ("plausible" if re.fullmatch(r"\+[1-9][0-9]{7,14}", value) else
                          "country_confirmation_required" if national_phone_digits(value) else "unrecognized")
        candidates.append({"kind": kind, "field": field, "formatStatus": status,
                           "ownership": "unverified", "selected": False,
                           "usableForOutreach": False})
    phone_values = [c for c in candidates[:2] if c["formatStatus"] in {"plausible", "country_confirmation_required"}]
    return {"candidates": candidates,
            "phoneSelection": "ambiguous" if len(phone_values) > 1 else "unselected",
            "consentObservations": [fields[name] for name in _CONSENT_FIELDS if name in fields],
            # Even a positive Athena Boolean/string does not prove recipient,
            # channel/purpose consent, shared-number safety or current suppression.
            "effectiveConsent": {"sms": "unknown", "phone": "unknown", "email": "unknown"},
            "outreachAuthorized": False,
            "requiredReview": ["recipient_identity", "channel_purpose_consent",
                               "suppression", "direct_contract", "staff_approval"]}


def _value(fields: dict[str, dict], name: str):
    field = fields[name]
    return field["value"] if field["state"] == "present" else None


def _policy(read: AthenaCardRead, index: int, candidate_id: str) -> dict:
    fields = {field.name: _field(field, read) for field in read.records[index]}
    issues = []
    for name, field in fields.items():
        if field["state"] == "invalid":
            issues.append("invalid_field:" + name)
    member, insured = _value(fields, "member_id"), _value(fields, "insured_id")
    if member is not None and insured is not None and member != insured:
        member_state = "conflicting"
        issues.append("member_identifiers_conflict")
    else:
        # insured_id is never a fallback for absent card/member ID.
        member_state = "review_required" if member is not None else "unknown"
    display = _value(fields, "subscriber_display_name")
    first, last = _value(fields, "subscriber_first_name"), _value(fields, "subscriber_last_name")
    if display and first and last and display != first + " " + last:
        subscriber_state = "ambiguous"
        issues.append("subscriber_names_conflict")
    elif bool(first) != bool(last):
        subscriber_state = "ambiguous"
        issues.append("subscriber_name_incomplete")
    else:
        subscriber_state = "review_required" if display or (first and last) else "unknown"
    for name in ("policy_record_id", "package_id", "sequence"):
        if fields[name]["state"] != "present":
            issues.append(name + "_unknown")
    return {"candidateId": _digest([candidate_id, "insurance", index]), "sourceIndex": index,
            "fields": list(fields.values()), "sequence": fields["sequence"]["parsedValue"],
            "memberIdState": member_state, "subscriberState": subscriber_state,
            "subscriberPatientMatch": "unverified", "issues": issues,
            "cardImageAvailability": "unknown", "coverageEffectiveDate": None,
            "currentCoverage": "unknown", "eligibilityVerifiedByCab": False,
            "eligibilityInterpretation": "source_snapshot_only",
            "sourceVersion": None, "accepted": False}


def map_athena_card_chase(
    *, context: CardChaseReadContext, patient_response: bytes | None,
    insurance_response: bytes | None,
) -> CardChaseReviewCandidate:
    """Generate a candidate from two independently bound, optional read bodies.

    None means that read is unavailable, never absent insurance/card/contact.
    Malformed/mismatched bytes reject the entire exchange with a static error.
    Empty patient + nonempty insurance rejects contradictory patient binding.
    No API/client is invoked, no pagination is followed and no data is applied.
    """
    source = _context(context)
    reads = {}
    try:
        for dataset, raw in (("patient", patient_response), ("insurance", insurance_response)):
            reads[dataset] = None if raw is None else decode_athena_card_read(
                raw, dataset=dataset, expected_patient_id=context.source_patient_id,
                scope=context.scope,
            )
    except AthenaClientError:
        raise AthenaCardChaseError("invalid_source") from None
    patient, insurance = reads["patient"], reads["insurance"]
    if patient is not None and not patient.records and insurance is not None and insurance.records:
        raise AthenaCardChaseError("binding_failed")
    summaries = {name: _read_summary(read) for name, read in reads.items()}
    candidate_id = _digest([MAPPING_VERSION, source, summaries])
    fields = {} if patient is None or not patient.records else {
        f.name: _field(f, patient) for f in patient.records[0]
    }
    policies = [] if insurance is None else [
        _policy(insurance, index, candidate_id) for index in range(len(insurance.records))
    ]
    # Retain collisions as separate source records. Neither source order nor
    # apparent recency resolves policy/subscriber identity or priority conflicts.
    for key, issue in (("policy_record_id", "duplicate_policy_id"), ("sequence", "duplicate_sequence")):
        values = [_value({f["name"]: f for f in p["fields"]}, key) for p in policies]
        normalized = [str(value) if value is not None else None for value in values]
        for policy, value in zip(policies, normalized):
            if value is not None and normalized.count(value) > 1:
                policy["issues"].append(issue)
    issues = [name + "_" + summary["state"] for name, summary in summaries.items()
              if summary["state"] != "complete"]
    issues.extend("invalid_patient_field:" + name for name, field in fields.items()
                  if field["state"] == "invalid")
    operational = source["system"] == "athena_production"
    payload = {"schemaVersion": "athena-card-chase-review.v2" if operational else SCHEMA_VERSION,
               "mappingVersion": "athena-card-chase-map.2026-09-30.3" if operational else MAPPING_VERSION,
               "candidateId": candidate_id, "status": "review_required", "source": source,
               "reads": summaries, "patient": {"fields": list(fields.values())},
               "contact": _contact(fields), "insurance": {"policies": policies,
               "state": summaries["insurance"]["state"], "cardImageAvailability": "unknown"},
               "reviewIssues": issues, "applied": False}
    return CardChaseReviewCandidate(candidate_id, json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ))
