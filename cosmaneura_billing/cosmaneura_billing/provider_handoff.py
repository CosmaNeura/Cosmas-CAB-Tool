"""Closed completed-note contract; no transport or inferred input provenance."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .billing_header import digest, source_text
from .persistence import SYNTHETIC_DEMO_SOURCE
from .schemas import VisitBundle
from .seed_demo_release_candidate import prepare_synthetic_demo_seed
from .source_import import source_import_external_id, source_import_idempotency_key
from .source_visit_context import calendar_date

SCHEMA = "cab.completed-note-handoff.v1"
SOURCE = "provider_completed_note"
NAMESPACE = "provider_platform"
MAPPING = "provider-handoff.v1"
COMPLETION_MEANING = "Completion is a provider confirmation or synthetic fixture convention, not independent verification of an upstream signature."


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HandoffError(RuntimeError):
    STATUSES = {"handoff_conflict": 409, "source_changed": 409, "not_authorized": 403,
                "fixture_unavailable": 409, "invalid_handoff": 422,
                "handoff_unavailable": 503, "operational_handoff_not_approved": 403}

    def __init__(self, code):
        self.code, self.status_code = code, self.STATUSES[code]
        super().__init__(code)


def require(condition, code="invalid_handoff"):
    if not condition:
        raise HandoffError(code)


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)



class Document(Closed):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    version: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(min_length=1, max_length=131072)

    @model_validator(mode="after")
    def exact_hash(self):
        try:
            source_text(self.text, 131072)
            valid = sha256(self.text) == self.sha256
        except (ValueError, UnicodeEncodeError):
            valid = False
        if not valid:
            raise ValueError("source text hash mismatch")
        return self


class SupportingDocument(Document):
    kind: Literal["transcript", "intake"]


class Completion(Closed):
    status: Literal["completed", "signed"]
    confirmedByProvider: Literal[True]
    completedAt: str = Field(min_length=20, max_length=40,
        pattern=r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$")

    @field_validator("confirmedByProvider", mode="before")
    @classmethod
    def literal_confirmation(cls, value):
        if value is not True:
            raise ValueError("provider confirmation must be true")
        return value

    @model_validator(mode="after")
    def timestamp(self):
        value = datetime.fromisoformat(self.completedAt.replace("Z", "+00:00"))
        if value.tzinfo is None or "T" not in self.completedAt:
            raise ValueError("completion timestamp requires timezone")
        return self


class VisitContext(Closed):
    patientAge: int = Field(ge=0, le=130)
    patientSex: Literal["M", "F", "other", "unknown"]
    encounterType: Literal["office_new", "office_established", "telehealth_new", "telehealth_established"]
    placeOfService: Literal["11", "02", "10"]
    providerSpecialty: str = Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    serviceDate: str | None = None

    @field_validator("serviceDate", mode="before")
    @classmethod
    def service_day(cls, value):
        return calendar_date(value)  # Omission is supported; explicit null is not a date.

    @model_validator(mode="after")
    def pos(self):
        if self.encounterType.startswith("office_") != (self.placeOfService == "11"):
            raise ValueError("visit setting and place of service disagree")
        return self


class HandoffRequest(Closed):
    schemaVersion: Literal[SCHEMA]
    operationId: str = Field(min_length=36, max_length=36)
    sourceRecordId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    sourceRevision: int = Field(ge=1, le=9007199254740991)
    expectedSource: dict | None
    completion: Completion
    note: Document
    supportingDocuments: list[SupportingDocument] = Field(default_factory=list, max_length=2)
    visit: VisitContext

    @model_validator(mode="after")
    def bounded(self):
        if str(UUID(self.operationId)) != self.operationId:
            raise ValueError("invalid operation UUID")
        docs = [self.note, *self.supportingDocuments]
        if (len({doc.id for doc in docs}) != len(docs)
                or len({doc.kind for doc in self.supportingDocuments}) != len(self.supportingDocuments)
                or any(doc.kind == "intake" and len(doc.text) > 65536 for doc in self.supportingDocuments)
                or sum(len(doc.text.encode("utf-8")) for doc in docs) > 524288):
            raise ValueError("source document bounds exceeded")
        return self


class FixtureRequest(Closed):
    operationId: str = Field(min_length=36, max_length=36)
    fixtureKey: str = Field(pattern=r"^SYN-RC-[0-9]{4}$")

    @model_validator(mode="after")
    def operation(self):
        if str(UUID(self.operationId)) != self.operationId:
            raise ValueError("invalid operation UUID")
        return self


@dataclass(frozen=True)
class PreparedHandoff:
    operation_id: str
    record_key: str
    source_revision: int
    manifest: dict
    manifest_sha256: str
    intent: dict
    expected_source: dict | None
    bundle: VisitBundle
    fixture: bool = False


def prepare_handoff(req: HandoffRequest, practice: str):
    identity = dict(practice_id=practice, source_system=NAMESPACE, feed="encounters", source_record_id=req.sourceRecordId)
    external = source_import_external_id(**identity)
    version_key = source_import_idempotency_key(**identity, source_version=str(req.sourceRevision), mapping_version=MAPPING)
    request = req.model_dump()
    if req.visit.serviceDate is None:
        # Preserve the exact request/manifest identity of all pre-context calls.
        request["visit"].pop("serviceDate")
    manifest = deepcopy(request)
    for key in ("operationId", "expectedSource"):
        del manifest[key]
    for doc in [manifest["note"], *manifest["supportingDocuments"]]:
        del doc["text"]
    manifest["versionKey"] = version_key
    supporting = {doc.kind: doc.text for doc in req.supportingDocuments}
    v = req.visit
    bundle = VisitBundle(encounter_id=external, patient_age=v.patientAge,
        patient_sex={"M": "male", "F": "female"}.get(v.patientSex, v.patientSex),
        encounter_type=v.encounterType, place_of_service=v.placeOfService,
        provider_specialty=v.providerSpecialty, signed_note=req.note.text,
        transcript=supporting.get("transcript", ""), intake_text=supporting.get("intake", ""))
    return PreparedHandoff(req.operationId, external, req.sourceRevision, manifest,
        digest(manifest), request, req.expectedSource, bundle)


def fixture_choices():
    return [{"key": case.case_id, "label": "Synthetic visit " + case.case_id,
             "encounterId": case.case_id} for case in prepare_synthetic_demo_seed().cases]


def prepare_fixture(req: FixtureRequest):
    # This immutable server manifest is revalidated each time. No caller text.
    case = next((case for case in prepare_synthetic_demo_seed().cases if case.case_id == req.fixtureKey), None)
    require(case is not None, "fixture_unavailable")
    bundle = VisitBundle(**case.visit)
    manifest = {"fixtureKey": case.case_id, "bundleSha256": digest(asdict(bundle)),
                "completion": "completed synthetic fixture convention", "source": SYNTHETIC_DEMO_SOURCE}
    return PreparedHandoff(req.operationId, "fixture:" + case.case_id, 1, manifest,
        digest(manifest), req.model_dump(), None, bundle, True)


def verified_fixture(stored, prepared):
    require(stored is not None and stored.source == SYNTHETIC_DEMO_SOURCE
            and asdict(stored.bundle) == asdict(prepared.bundle), "fixture_unavailable")
