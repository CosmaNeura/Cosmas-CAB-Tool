"""Authenticated, source-pinned generation and unreviewed header revisions."""
from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .billing_header import BillingHeaderError, HeaderActor, check_source
from .billing_header_runtime import generated_header
from .billing_header_store import MemoryBillingHeaderStore, PostgresBillingHeaderStore
from .persistence import PostgresContext, ReviewerNotAuthorized, StaleEncounter
from .coder_feedback import FeedbackConflict

router = APIRouter()


class HeaderRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    encounterId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    expectedSource: dict[str, Any]


class HeaderGenerate(HeaderRead):
    operationId: str = Field(min_length=36, max_length=36)


class HeaderSave(HeaderGenerate):
    headerId: str = Field(min_length=36, max_length=36)
    expectedRevision: int = Field(ge=0, le=100)
    draft: dict[str, Any]
    reason: str = Field(min_length=1, max_length=600)


class HeaderCatalog(HeaderRead):
    headerId: str = Field(min_length=36, max_length=36)
    expectedRevision: int = Field(ge=0, le=100)
    candidateId: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=2, max_length=80)
    offset: int = Field(default=0, ge=0, le=1_000_000)


class HeaderSignoff(HeaderRead):
    headerId: str = Field(min_length=36, max_length=36)
    expectedRevision: int = Field(ge=0, le=100)
    feedback: dict[str, Any]
    acknowledged: bool
    humanProposalDecisions: list[dict[str, Any]] = Field(max_length=41)
    baselineDecisions: list[dict[str, Any]] = Field(default_factory=list, max_length=20)


def configure_header(application, config, encounter_store):
    """No schema dependency or memory fallback when the feature is disabled."""
    application.state.billing_header_store = None
    if not config.billing_header_enabled:
        return
    if config.state_store == "postgres":
        application.state.billing_header_store = PostgresBillingHeaderStore(
            PostgresContext(config.database_url, practice_id=config.practice_id))
    else:
        application.state.billing_header_store = MemoryBillingHeaderStore(
            encounter_store, practice_id=encounter_store.practice_id,
            authorize=lambda actor: actor.role in config.allowed_roles,
        )


def _context(request, encounter_id):
    from . import api
    principal = api._require_roles(request, "Provider", "Coder", "Admin")
    config = api._request_settings(request)
    store = getattr(request.app.state, "billing_header_store", None)
    if not config.billing_header_enabled or store is None:
        raise HTTPException(404, "header_unavailable")
    if principal.practice_id != (config.practice_id or "development"):
        raise HTTPException(403, "not_authorized")
    stored = api._get_stored_encounter(request, encounter_id)
    role = next(role for role in ("Admin", "Coder", "Provider")
                if principal.has_any_role((role,)))
    tenant, user = principal.tenant_id, principal.user_id
    if config.auth_mode == "disabled" and config.state_store == "local":
        # Local display identities remain explicitly synthetic and never enter
        # the database/Entra path. Actor authority still comes from middleware.
        tenant = str(uuid5(NAMESPACE_URL, f"cab-local:{tenant}"))
        user = str(uuid5(NAMESPACE_URL, f"cab-local:{principal.user_id}"))
    actor = HeaderActor(tenant, user, f"{role} {user[:8]}", role)
    return stored, store, actor


def _safe(exc):
    return HTTPException(exc.status_code, exc.code)


@router.post("/api/encounters/header")
def read_header(request: Request, req: HeaderRead):
    try:
        stored, store, _actor = _context(request, req.encounterId)
        check_source(stored, req.expectedSource)
        return {"view": store.latest(req.encounterId, expected_source=req.expectedSource)}
    except BillingHeaderError as exc:
        raise _safe(exc) from None


@router.post("/api/encounters/header/generate")
def generate_header(request: Request, req: HeaderGenerate):
    from . import api
    try:
        stored, store, actor = _context(request, req.encounterId)
        check_source(stored, req.expectedSource)
        # A retry or a second Generate must not replace another user's edits.
        existing = store.generation_replay(
            req.encounterId, expected_source=req.expectedSource,
            operation_id=req.operationId, actor=actor,
            audit_completion=(api._completion_event(request)
                              if isinstance(store, PostgresBillingHeaderStore) else None),
        )
        if existing:
            if isinstance(store, PostgresBillingHeaderStore):
                request.state.audit_completion_committed = True
            return {"view": existing, "created": False}
        api._require_inference_input_allowed(request, stored=stored)
        result = api._request_orchestrator(request).process(
            stored.bundle, user_id=api._request_principal(request).user_id,
            input_provenance=stored.source,
        )
        generated = generated_header(stored, result, engine_version=api._ENGINE_VERSION)
        # Store reloads and compares the source under the write transaction.
        view, created = store.create(
            req.encounterId, expected_source=req.expectedSource, generated=generated,
            actor=actor, operation_id=req.operationId,
            audit_completion=(api._completion_event(request)
                              if isinstance(store, PostgresBillingHeaderStore) else None),
        )
        if isinstance(store, PostgresBillingHeaderStore):
            request.state.audit_completion_committed = True
        return {"view": view, "created": created}
    except BillingHeaderError as exc:
        raise _safe(exc) from None


@router.post("/api/encounters/header/save")
def save_header(request: Request, req: HeaderSave):
    from . import api
    try:
        _stored, store, actor = _context(request, req.encounterId)
        view, created = store.save(
            req.encounterId, header_id=req.headerId, expected_source=req.expectedSource,
            expected_revision=req.expectedRevision, draft=req.draft, reason=req.reason,
            actor=actor, operation_id=req.operationId,
            audit_completion=(api._completion_event(request)
                              if isinstance(store, PostgresBillingHeaderStore) else None),
        )
        if isinstance(store, PostgresBillingHeaderStore):
            request.state.audit_completion_committed = True
        return {"view": view, "created": created}
    except BillingHeaderError as exc:
        raise _safe(exc) from None


@router.post("/api/encounters/header/diagnosis-catalog")
def diagnosis_catalog(request: Request, req: HeaderCatalog):
    from .billing_header import require
    from .billing_header_mapping import search_catalog
    try:
        stored, store, _actor = _context(request, req.encounterId)
        check_source(stored, req.expectedSource)
        current = store.latest(req.encounterId, expected_source=req.expectedSource)
        require(current is not None and current["header"]["headerId"] == req.headerId, "header_not_found")
        require(current["revisionNumber"] == req.expectedRevision, "revision_conflict")
        result = search_catalog(current["header"], req.candidateId, req.query, req.offset)
        return {"headerId": req.headerId, "revision": req.expectedRevision,
                "expectedSource": req.expectedSource, **result}
    except BillingHeaderError as exc:
        raise _safe(exc) from None


@router.post("/api/encounters/header/signoff")
def signoff_header(request: Request, req: HeaderSignoff):
    from . import api
    try:
        _stored, store, actor = _context(request, req.encounterId)
        try:
            payload = api.FeedbackModel.model_validate(req.feedback)
        except ValidationError:
            raise BillingHeaderError("invalid_signoff") from None
        if payload.encounter_id != req.encounterId:
            raise BillingHeaderError("invalid_signoff")
        _stored, feedback, context = api._validated_feedback(request, payload)
        view, created = store.signoff(req.encounterId, header_id=req.headerId,
            expected_source=req.expectedSource, expected_revision=req.expectedRevision,
            feedback=feedback, verified_context=context, actor=actor,
            human_proposal_decisions=req.humanProposalDecisions, acknowledged=req.acknowledged,
            baseline_decisions=req.baselineDecisions,
            feedback_store=api._request_feedback_store(request),
            audit_completion=(api._completion_event(request)
                              if isinstance(store, PostgresBillingHeaderStore) else None))
        if isinstance(store, PostgresBillingHeaderStore):
            request.state.audit_completion_committed = True
        return {"ok": True, "reviewId": feedback.review_id, "replayed": not created,
                "headerSignoff": view["signoff"], "view": view}
    except BillingHeaderError as exc:
        raise _safe(exc) from None
    except FeedbackConflict:
        raise HTTPException(409, "review_conflict") from None
    except ReviewerNotAuthorized:
        raise HTTPException(403, "not_authorized") from None
    except StaleEncounter:
        raise HTTPException(409, "source_changed") from None
