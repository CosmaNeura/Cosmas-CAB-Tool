"""Closed Core operation contract; no browser assertion creates authority."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from .provider_handoff import Closed, Completion, VisitContext, Document
from .billing_header import timestamp

PURPOSES = frozenset(("completed-note.accept", "header.read", "header.generate", "header.save"))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class CoreActor(Closed):
    issuer: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    subject: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    role: Literal["Provider"]
    sessionSha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CoreSource(Closed):
    noteId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    appointmentId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    noteSchemaId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    revision: int = Field(ge=1, le=9007199254740991)
    contentSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    textSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    projectionVersion: Literal["ordered-note-markdown.v1"]
    excludedRecordingTime: bool


class CoreAdmission(Closed):
    admissionId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    dataClass: Literal["synthetic", "deidentified"]
    reviewRef: str = Field(min_length=1, max_length=160, pattern=r"^[^\x00-\x1f\x7f]+$")
    contentSha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CoreOperation(Closed):
    operationId: str
    practiceId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    actor: CoreActor
    source: CoreSource
    noteText: str
    visit: VisitContext
    completion: Completion
    admission: CoreAdmission

    @model_validator(mode="after")
    def exact_source(self):
        if str(UUID(self.operationId)) != self.operationId or self.completion.status != "completed":
            raise ValueError("invalid Core operation")
        Document(id=self.source.noteId, version=str(self.source.revision),
                 sha256=self.source.textSha256, text=self.noteText)
        if self.admission.contentSha256 != self.source.contentSha256:
            raise ValueError("Core source admission mismatch")
        return self

    def document(self):
        result = self.model_dump()
        if "serviceDate" not in self.visit.model_fields_set:
            result["visit"].pop("serviceDate", None)
        return result


class CoreBridgeError(PermissionError):
    def __init__(self, code="core_authority_unavailable", status=403):
        self.code, self.status_code = code, status
        super().__init__(code)


def require(value, code="core_authority_unavailable", status=403):
    if not value:
        raise CoreBridgeError(code, status)


def authorization_request(operation, purpose, request_sha):
    require(purpose in PURPOSES)
    return {"schemaVersion": "core-cab-authorization-request.v1", "purpose": purpose,
        "actor": {k: operation["actor"][k] for k in ("issuer", "subject")},
        "requestSha256": request_sha, "sourceContentSha256": operation["source"]["contentSha256"],
        "sourceTextSha256": operation["source"]["textSha256"]}


def verify_authorization(value, operation, purpose, request_sha, *, now=None):
    """Verify the authenticated Core response; its body is not a bearer token."""
    try:
        keys = {"schemaVersion", "allowed", "operationId", "purpose", "requestSha256", "operationSha256",
                "practiceId", "actor", "source", "admission", "authorizedAt", "expiresAt"}
        require(type(value) is dict and set(value) == keys and value["allowed"] is True
                and value["schemaVersion"] == "core-cab-authorization.v1")
        require(value["operationId"] == operation["operationId"] and value["purpose"] == purpose
                and value["requestSha256"] == request_sha and value["operationSha256"] == digest(operation)
                and value["practiceId"] == operation["practiceId"]
                and value["actor"] == {k: operation["actor"][k] for k in ("issuer", "subject", "role")}
                and canonical(value["source"]) == canonical(operation["source"])
                and canonical(value["admission"]) == canonical(operation["admission"]))
        issued, expires = timestamp(value["authorizedAt"]), timestamp(value["expiresAt"])
        current = now or datetime.now(timezone.utc)
        require(issued <= current + timedelta(seconds=2) and current < expires and 0 < (expires-issued).total_seconds() <= 30)
        return value
    except CoreBridgeError:
        raise
    except Exception:
        raise CoreBridgeError() from None
