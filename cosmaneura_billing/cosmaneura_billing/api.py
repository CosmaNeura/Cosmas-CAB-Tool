"""FastAPI service wrapping the billing engine — the demoable backend.

Endpoints (shapes match the documented contracts in frontend/*.jsx):

  GET  /api/health                        engine + knowledge-base status
  GET  /api/session                       server-owned view capabilities
  GET  /api/encounters                    encounters available to demo
  POST /api/encounters/detail             one encounter's documentation
  POST /api/encounters/billing            run the pipeline on a stored encounter
  POST /api/billing                       run the pipeline on posted documentation
  POST /api/encounters/coder-view         evidence-linked draft for coder review
  POST /api/feedback                      persist a coder's reviewed decisions
  GET  /api/feedback/summary              accuracy metrics + tuning backlog
  POST /api/chase/worklist                minimum-necessary insurance worklist
  POST /api/chase/task                    audited manual contact task

Run:  uvicorn cosmaneura_billing.api:app --reload
The orchestrator and its regulatory knowledge bases are warmed during process
startup. Readiness remains unavailable until that warmup and every configured
durable persistence boundary succeed.
"""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import secrets
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import coder_feedback
from .access_audit import (
    AccessAuditMiddleware, AccessAuditStore, MemoryAccessAuditStore,
    DeniedAccessAuditor, build_access_event, classify_access,
)
from .auth import AuthPrincipal, EntraAuthMiddleware
from .billing_tab import build_billing_tab_view
from .chase.worklist import ChaseActionConflict
from .chase.store import PostgresChaseStore
from .coder_feedback import EncounterFeedback, FeedbackConflict, FeedbackStore
from .completeness import OverrideRequiresReason, applicable_rulepack, check_note
from .corpus.acibench import load_acibench
from .corpus.demo import SAMPLE_VISIT
from .llm import describe_client
from .orchestrator import BillingOrchestrator, InferenceInputPolicyError
from .primary_diagnosis import result_primary
from .persistence import (
    MemoryEncounterStore, PersistenceUnavailable, PostgresEncounterStore, PostgresFeedbackStore,
    PostgresMembershipStore, ReviewerNotAuthorized, StaleEncounter,
    ATHENA_REVIEW_OPERATIONAL_SOURCE, DEPLOYABLE_SYNTHETIC_INFERENCE_SOURCES,
    READ_ONLY_SYNTHETIC_ENCOUNTER_SOURCES, UnknownEncounter, postgres_services,
)
from .preferences import PreferenceStore
from .runtime import PHISafeHTTPMiddleware, configure_json_logging
from .schemas import BillingResult, CodeKind, CodeSuggestion, Disposition, VisitBundle
from .security import (
    FixedWindowLimiter, PrincipalRateLimitMiddleware,
    RequestBodyLimitMiddleware, SecurityHeadersMiddleware, StrictOriginMiddleware,
)
from .settings import Settings, get_settings

router = APIRouter()

_KIND = {CodeKind.EM: "EM", CodeKind.ICD10: "ICD10",
         CodeKind.PROCEDURE: "PROCEDURE", CodeKind.MODIFIER: "MODIFIER"}
_ENGINE_VERSION = "cpt2026u4;icd10-FY2026;ncci-2026Q3-v32.2;mcd-NGS-2026-07-20"
_REQUEST_RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SYNTHETIC_INFERENCE_ONLY_DETAIL = (
    "model inference is restricted to reserved synthetic demo encounters"
)


def _review_context_binding(
    encoded_payload: str,
    principal: AuthPrincipal,
    encounter_version: int,
    resource_id: str,
) -> bytes:
    return json.dumps(
        [
            "cab-coder-review.v3",
            encoded_payload,
            resource_id,
            principal.practice_id,
            principal.tenant_id,
            principal.user_id,
            encounter_version,
        ],
        separators=(",", ":"),
    ).encode("utf-8")


def _issue_review_context(
    stored,
    principal: AuthPrincipal,
    *,
    signing_key: bytes,
    engine_version: str = _ENGINE_VERSION,
    recommendation_manifest: tuple[tuple[str, str], ...],
) -> str:
    """Bind the view with a server secret independent of exposed source IDs."""
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise ValueError("review signing key unavailable")
    payload = json.dumps(
        {
            "v": 3,
            "engineVersion": engine_version,
            "submittedEm": stored.bundle.submitted_em_code,
            "recommendationCount": len(recommendation_manifest),
            "recommendationDigest": _recommendation_digest(
                recommendation_manifest,
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        signing_key,
        _review_context_binding(encoded, principal, stored.version, stored.resource_id),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _verify_review_context(
    token: str,
    stored,
    principal: AuthPrincipal,
    encounter_version: int,
    *,
    signing_key: bytes,
) -> dict[str, Any]:
    """Return the signed review contract or reject a forged/stale binding."""
    if (not isinstance(signing_key, bytes) or len(signing_key) < 32
            or not isinstance(token, str) or len(token) > 1_024):
        raise ValueError("invalid review context")
    try:
        encoded, supplied = token.split(".", 1)
        if not encoded or len(supplied) != 64:
            raise ValueError("invalid review context")
        expected = hmac.new(
            signing_key,
            _review_context_binding(encoded, principal, encounter_version, stored.resource_id),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ValueError("invalid review context")
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if set(payload) != {
            "v", "engineVersion", "submittedEm", "recommendationCount",
            "recommendationDigest",
        } or type(payload["v"]) is not int or payload["v"] != 3:
            raise ValueError("invalid review context")
        engine_version = payload["engineVersion"]
        if not isinstance(engine_version, str) or not 1 <= len(engine_version) <= 256:
            raise ValueError("invalid review context")
        submitted_em = payload["submittedEm"]
        if submitted_em is not None and (
            not isinstance(submitted_em, str) or not 1 <= len(submitted_em) <= 32
        ):
            raise ValueError("invalid review context")
        if (
            type(payload["recommendationCount"]) is not int
            or not 0 <= payload["recommendationCount"] <= 500
            or not isinstance(payload["recommendationDigest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["recommendationDigest"])
        ):
            raise ValueError("invalid review context")
        return payload
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid review context") from exc


def _recommendation_digest(
    recommendation_manifest: tuple[tuple[str, str], ...],
) -> str:
    canonical = json.dumps(
        sorted([list(item) for item in recommendation_manifest]),
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _coder_recommendation_manifest(view: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    rows = ([view["em"]] if view.get("em") else []) + list(view.get("dx") or [])
    return tuple((row["kind"], row["code"]) for row in rows)


def _feedback_matches_review_context(
    decisions: list["DecisionModel"], context: dict[str, Any],
) -> bool:
    submitted = tuple(
        (decision.kind, decision.engine_code)
        for decision in decisions
        if decision.action != "add"
    )
    return (
        len(submitted) == context["recommendationCount"]
        and hmac.compare_digest(
            _recommendation_digest(submitted),
            context["recommendationDigest"],
        )
    )


# --------------------------------------------------------------------------- #
# Engine + encounter store (lazy singletons)
# --------------------------------------------------------------------------- #
def get_orchestrator(
    settings: Settings | None = None, *, feedback_store=None,
) -> BillingOrchestrator:
    """Build an engine using one resolved application configuration."""
    config = settings or get_settings()
    preferences = (
        PreferenceStore(settings=config, feedback_store=feedback_store)
        if feedback_store is not None else None
    )
    return BillingOrchestrator(settings=config, preferences=preferences)


@lru_cache(maxsize=1)
def get_encounters() -> dict[str, VisitBundle]:
    store: dict[str, VisitBundle] = {SAMPLE_VISIT.encounter_id: SAMPLE_VISIT}
    try:
        for b in load_acibench("train")[:10]:
            store[b.encounter_id] = b
    except FileNotFoundError:
        pass
    return store


def get_feedback_store(settings: Settings | None = None):
    config = settings or get_settings()
    if config.state_store == "postgres":
        return postgres_services(config).feedback
    return FeedbackStore(config.feedback_path)


def _request_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def _request_orchestrator(request: Request) -> BillingOrchestrator:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        # TestClient calls outside a lifespan still use stable, app-local state.
        orchestrator = get_orchestrator(
            _request_settings(request),
            feedback_store=request.app.state.feedback_store,
        )
        request.app.state.orchestrator = orchestrator
    return orchestrator


def _request_principal(request: Request) -> AuthPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, AuthPrincipal):
        raise HTTPException(status_code=401, detail="authentication required")
    return principal


def _request_feedback_store(request: Request):
    return request.app.state.feedback_store


def _request_review_signing_key(request: Request) -> bytes:
    key = request.app.state.review_context_signing_key
    if not isinstance(key, bytes) or len(key) < 32:
        raise HTTPException(503, "review_signing_unavailable")
    return key


def _request_encounter_store(request: Request):
    return request.app.state.encounter_store


def _set_audit_resource(request: Request, resource_id: str) -> None:
    # The store generated and tenant-validated this opaque ID. Raw request IDs
    # and EHR/MRN values must never enter the access-audit stream.
    request.state.audit_resource_id = resource_id


def _completion_event(request: Request, *, status: int = 200, outcome: str = "allowed"):
    classified = classify_access(request.method, request.url.path)
    event_id = getattr(request.state, "audit_event_id", "")
    if classified is None or not event_id:
        raise RuntimeError("access audit reservation is unavailable")
    return build_access_event(
        settings=_request_settings(request), scope=request.scope,
        classified=classified, event_id=event_id, status=status, outcome=outcome,
    )


def _require_roles(request: Request, *roles: str) -> AuthPrincipal:
    principal = _request_principal(request)
    if not principal.has_any_role(roles):
        raise HTTPException(status_code=403, detail="access is not permitted")
    return principal


def _permitted_views(principal: AuthPrincipal) -> list[str]:
    """Return server-owned UI capabilities in stable navigation order.

    The frontend consumes capabilities rather than interpreting role names.
    Route authorization below remains the enforcement boundary.
    """
    if principal.has_any_role(("Admin", "Coder")):
        return ["provider", "coder", "chase"]
    if principal.has_any_role(("Provider",)):
        # Provider is deliberately the restrictive persona when a mistaken
        # extra BillingStaff assignment is present. Admin/Coder above are the
        # only roles that intentionally expand this login beyond one tab.
        return ["provider"]
    if principal.has_any_role(("BillingStaff",)):
        return ["chase"]
    return []


def _require_view(request: Request, view: str) -> AuthPrincipal:
    principal = _request_principal(request)
    if view not in _permitted_views(principal):
        raise HTTPException(status_code=403, detail="access is not permitted")
    return principal


def _require_chase_staff(request: Request) -> AuthPrincipal:
    principal = _require_view(request, "chase")
    if not principal.has_any_role(("BillingStaff", "Admin")):
        raise HTTPException(status_code=403, detail="access is not permitted")
    return principal


# --------------------------------------------------------------------------- #
# Serializers -> the frontend data contracts
# --------------------------------------------------------------------------- #
def _level(code: str | None) -> int:
    return int(code[-1]) if code and code[-1:].isdigit() else 0


def _code_json(s: CodeSuggestion, *, submitted_em: str | None = None,
               primary: CodeSuggestion | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": _KIND.get(s.kind, str(s.kind)),
        "code": s.code,
        "label": s.label,
        "disposition": s.disposition.value,
        "denialRisk": s.denial_risk,
        "evidence": [{"source": e.source, "quote": e.quote} for e in s.evidence],
    }
    if s is primary and s.disposition == Disposition.CAPTURED:
        out["primary"] = True
    if s.disposition != Disposition.CAPTURED and s.rationale:
        out["reason"] = s.rationale
    if (s.kind == CodeKind.EM and s.disposition == Disposition.CAPTURED
            and submitted_em and _level(s.code) > _level(submitted_em)):
        out["correctedFrom"] = submitted_em
    prior = next((n for n in s.notes if n.startswith("denial_prior:")), None)
    if prior:
        out["denialPrior"] = float(prior.split(":", 1)[1])
    sprior = next((n for n in s.notes if n.startswith("denial_prior_state:")), None)
    if sprior:
        _, state, rate = sprior.split(":")
        out["denialPriorState"] = {"state": state, "rate": float(rate)}
    return out


_AUDIT_RE = re.compile(r"^(?P<action>[A-Z-]+)\s+(?P<code>\S+(?:\s->\s\S+)?)\s*[:(]?\s*(?P<detail>.*)$")


def _audit_json(lines: list[str]) -> list[dict[str, str]]:
    out = []
    for line in lines:
        head, _, detail = line.partition(": ")
        m = _AUDIT_RE.match(head.strip())
        if m and detail:
            out.append({"action": m.group("action"), "code": m.group("code").strip(),
                        "detail": detail.strip()})
        else:
            out.append({"action": line.split()[0] if line.split() else "",
                        "code": "", "detail": line})
    return out


def _billing_json(bundle: VisitBundle, result: BillingResult) -> dict[str, Any]:
    ws, card = result.biller_worksheet, result.provider_card
    sequencing = result_primary(result)
    primary = sequencing.primary
    sub = bundle.submitted_em_code
    return {
        "encounterId": bundle.encounter_id,
        "summary": card.summary,
        "captured": [_code_json(s, submitted_em=sub, primary=primary) for s in card.captured_codes],
        "declined": [_code_json(s) for s in card.declined_codes],
        "em": ({"code": ws.em_code.code, "label": ws.em_code.label} if ws.em_code else None),
        "primary": ({"code": primary.code, "label": primary.label} if primary else None),
        "diagnosisSequencing": sequencing.public,
        "diagnoses": [{"code": d.code, "label": d.label, "denialRisk": d.denial_risk}
                      for d in ws.diagnoses],
        "denialFlags": ws.denial_flags,
        "requiresSignoff": ws.requires_provider_signoff,
        "audit": _audit_json(ws.audit_trail),
        "feedback": [{"kind": e.kind.value, "target": e.target.value, "message": e.message,
                      "tone": e.tone, "patientBenefit": e.patient_benefit}
                     for e in result.feedback_events],
        "revenueIntegrityNote": result.revenue_integrity_note,
        **result.inference,
        "degraded": _degraded_json(result),
    }


def _degraded_json(result: BillingResult) -> Optional[dict[str, str]]:
    d = result.degraded
    return {"reason": d.reason, "detail": d.detail} if d else None


def _coder_view_json(
    bundle: VisitBundle, result: BillingResult, coder_id: str,
) -> dict[str, Any]:
    card = result.provider_card
    sequencing = result_primary(result)

    sources = {
        "signed_note": bundle.signed_note,
        "transcript": bundle.transcript,
        "intake_text": bundle.intake_text,
    }

    def row(s: CodeSuggestion) -> dict[str, Any]:
        spans = []
        for evidence in s.evidence:
            source_text = sources.get(evidence.source, "")
            start = source_text.find(evidence.quote) if evidence.quote else -1
            spans.append({
                "source": evidence.source,
                "text": evidence.quote,
                "loc": (
                    {"start": start, "end": start + len(evidence.quote)}
                    if start >= 0 else None
                ),
            })
        return {
            "id": re.sub(r"[^a-z0-9]", "", s.code.lower()) or "x",
            "kind": _KIND.get(s.kind, str(s.kind)),
            "code": s.code, "label": s.label,
            "primary": (s is sequencing.primary and s.disposition == Disposition.CAPTURED) or None,
            "why": s.rationale,
            "evidence": s.evidence[0].quote if s.evidence else "",
            "evidenceSpans": spans,
        }

    em = next((s for s in card.captured_codes if s.kind == CodeKind.EM), None)
    dx = [s for s in card.captured_codes if s.kind == CodeKind.ICD10]
    withheld = [{**row(s), "status": "rejected" if s.disposition == Disposition.REJECTED
                 else "held"} for s in card.declined_codes]
    rulepack = applicable_rulepack(bundle.place_of_service, bundle.encounter_type)
    completeness = (
        check_note(bundle.signed_note, rulepack=rulepack).to_dict(
            pos=bundle.place_of_service,
        )
        if rulepack is not None else {
            "rulepack": None,
            "complete": None,
            "results": [],
            "attestationBlock": None,
            "detail": "No rule pack covers this encounter type yet.",
        }
    )
    return {
        "encounterId": bundle.encounter_id,
        "engineVersion": _ENGINE_VERSION,
        "submittedEm": bundle.submitted_em_code,
        "coderId": coder_id,
        "diagnosisSequencing": sequencing.public,
        "em": row(em) if em else None,
        "dx": [row(s) for s in dx],
        "withheld": withheld,
        "completeness": completeness,
        **result.inference,
        "degraded": _degraded_json(result),
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EncounterRequest(StrictRequest):
    encounterId: str = Field(min_length=1, max_length=128)

    @field_validator("encounterId")
    @classmethod
    def validate_encounter_id(cls, value: str) -> str:
        if _REQUEST_RESOURCE_ID.fullmatch(value) is None:
            raise ValueError("encounterId has an invalid format")
        return value


class BillingTabRequest(EncounterRequest):
    generatedAt: str = Field(default="", max_length=64)


class EMBenchmarkRequest(StrictRequest):
    specialty: Optional[str] = Field(default=None, max_length=128)
    state: str = Field(default="NH", min_length=2, max_length=2)


class PayerDenialRequest(StrictRequest):
    state: str = Field(default="NH", min_length=2, max_length=2)


class ProcessRequest(StrictRequest):
    encounterId: str = Field(default="ADHOC-1", min_length=1, max_length=128)
    intake: str = Field(default="", max_length=250_000)
    transcript: str = Field(default="", max_length=500_000)
    signedNote: str = Field(default="", max_length=250_000)
    submittedEm: Optional[str] = Field(default=None, max_length=32)
    encounterType: str = Field(default="office_established", max_length=64)
    placeOfService: str = Field(default="11", max_length=8)
    patientAge: int = Field(default=0, ge=0, le=130)
    patientSex: str = Field(default="unknown", max_length=32)


class CompletenessRequest(StrictRequest):
    note: str = Field(default="", max_length=750_000)
    placeOfService: str = Field(default="10", max_length=8)
    encounterType: str = Field(default="telehealth_established", max_length=64)
    rulepack: Optional[str] = Field(default=None, max_length=128)
    # rule_id -> reason. A blank reason is rejected (422): the reason is the
    # record of why the rule did not apply to this note.
    overrides: Optional[dict[str, str]] = None


class ChaseDetectRequest(StrictRequest):
    today: str = Field(min_length=1, max_length=32)
    # Raw CoverageSnapshot fields — the source-neutral insurance adapter boundary.
    snapshots: list[dict[str, Any]] = Field(default_factory=list, max_length=5_000)


class ChaseWorklistRequest(StrictRequest):
    bucket: str = Field(default="", max_length=32)
    assignee: str = Field(default="", max_length=128)
    needsPerson: bool = False


class ChaseMetricsRequest(StrictRequest):
    baselineMinutesPerCard: Optional[float] = Field(default=None, ge=0)
    baselineClaimsHeldMonthly: Optional[int] = Field(default=None, ge=0)


class ChaseTargetRequest(StrictRequest):
    chaseId: str = Field(min_length=1, max_length=128)

    @field_validator("chaseId")
    @classmethod
    def validate_chase_id(cls, value: str) -> str:
        if _REQUEST_RESOURCE_ID.fullmatch(value) is None:
            raise ValueError("chaseId has an invalid format")
        return value


class ChaseDemoSeedRequest(StrictRequest):
    seedDigest: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")


class ChaseActionRequest(ChaseTargetRequest):
    action_id: UUID
    action: str = Field(min_length=1, max_length=32)
    assignee: str = Field(default="", max_length=128)
    reason: str = Field(default="", max_length=4_000)
    channel: Optional[str] = Field(default=None, max_length=32)
    template_id: Optional[str] = Field(default=None, max_length=128)
    outcome: Optional[str] = Field(default=None, max_length=128)
    minutes_spent: float = Field(default=0.0, ge=0, le=1_440)
    note: str = Field(default="", max_length=4_000)
    source_write_id: str = Field(default="", max_length=256)
    eligibility_status: str = Field(default="", max_length=128)


class ChasePolicyCreateRequest(StrictRequest):
    policy_revision_id: UUID


class ChasePolicyApprovalRequest(StrictRequest):
    approval_event_id: UUID
    policy_revision_id: UUID
    reason: str = Field(default="", max_length=2_000)


class ChasePolicyActivationRequest(StrictRequest):
    approval_event_id: UUID
    policy_revision_id: UUID


class ChasePolicyRevocationRequest(StrictRequest):
    approval_event_id: UUID
    policy_revision_id: UUID
    reason: str = Field(min_length=1, max_length=2_000)


class ChaseMessageReleaseRequest(ChaseTargetRequest):
    release_id: UUID
    reason: str = Field(default="", max_length=2_000)


class DecisionModel(StrictRequest):
    kind: Literal["EM", "ICD10", "PROCEDURE", "MODIFIER"]
    engine_code: Optional[str] = Field(default=None, max_length=32)
    action: Literal["accept", "modify", "reject", "add"]
    final_code: Optional[str] = Field(default=None, max_length=32)
    reason_code: Optional[Literal[
        "em_doc_supports_higher_level", "em_doc_supports_lower_level",
        "em_mdm_element_disagreement", "em_time_based_leveling",
        "dx_not_supported_by_doc", "dx_contradicted_by_doc",
        "dx_needs_more_specific_code", "dx_primary_secondary_swap",
        "dx_missed_by_engine", "modifier_required",
        "procedure_bundling_ncci", "medical_necessity_coverage", "other",
    ]] = None
    note: str = Field(default="", max_length=4_000)

    @field_validator("engine_code", "final_code")
    @classmethod
    def normalize_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("code values cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_action_shape(self):
        valid = (
            self.action == "add" and self.engine_code is None and self.final_code
        ) or (
            self.action == "reject" and self.engine_code and self.final_code is None
        ) or (
            self.action in {"accept", "modify"}
            and self.engine_code and self.final_code
        )
        if not valid:
            raise ValueError("decision fields do not match the selected action")
        if self.action == "accept" and self.final_code != self.engine_code:
            raise ValueError("accepted decisions must preserve the engine code")
        if self.action == "modify" and self.final_code == self.engine_code:
            raise ValueError("modified decisions must change the engine code")
        if self.action in {"modify", "reject", "add"}:
            if self.reason_code is None:
                raise ValueError("changed decisions require a controlled reason")
            if not self.note.strip():
                raise ValueError("changed decisions require a rationale")
        if self.action == "add" and re.search(
            r"(?:^|\n)Evidence:\s*\S", self.note, flags=re.IGNORECASE,
        ) is None:
            raise ValueError("human-added codes require supporting evidence")
        return self


class FeedbackModel(StrictRequest):
    review_id: UUID
    review_context: str = Field(min_length=1, max_length=1_024)
    encounter_id: str = Field(min_length=1, max_length=128)
    encounter_version: int = Field(ge=1)
    submitted_em: Optional[str] = Field(default=None, min_length=1, max_length=32)
    engine_em: Optional[str] = Field(default=None, min_length=1, max_length=32)
    final_em: Optional[str] = Field(default=None, min_length=1, max_length=32)
    decisions: list[DecisionModel] = Field(default_factory=list, max_length=500)
    time_spent_seconds: Optional[float] = Field(default=None, ge=0, le=86_400)
    overall_note: str = Field(default="", max_length=4_000)

    @field_validator("submitted_em", "engine_em", "final_em")
    @classmethod
    def normalize_em_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("E/M code values cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_final_em(self):
        em_decisions = [decision for decision in self.decisions if decision.kind == "EM"]
        if len(em_decisions) > 1:
            raise ValueError("a review may contain only one E/M decision")
        if em_decisions:
            decision = em_decisions[0]
            expected_engine = None if decision.action == "add" else decision.engine_code
            expected = None if decision.action == "reject" else decision.final_code
            if self.engine_em is not None and self.engine_em != expected_engine:
                raise ValueError("engine E/M must match the issued E/M decision")
            if self.final_em != expected:
                raise ValueError("final E/M must match the E/M decision")
        elif self.engine_em is not None or self.final_em is not None:
            raise ValueError("E/M summary fields require an E/M decision")
        return self


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def _cpt_status(cpt, settings: Settings | None = None) -> str:
    """One human-readable line the UI can show for the CPT pill tooltip."""
    if cpt.loaded:
        return f"{cpt.size} official descriptors loaded from {cpt.source}"
    if cpt.fetch_error:
        # ``fetch_error`` is deliberately not interpolated. Keep this response
        # safe even if a legacy or injected descriptor object carries raw
        # provider exception text.
        return "CPT auto-fetch failed; generic labels in use"
    if (settings or get_settings()).ama_configured:
        return "AMA credentials present but no descriptors resolved"
    return ("no CPT license configured — set the four AMA_* variables "
            "(auto-fetch) or CPT_DATA_DIR (local Standard Data File); "
            "generic labels in use")


@router.get("/api/health")
def health(request: Request) -> dict[str, Any]:
    orch = _request_orchestrator(request)
    settings = _request_settings(request)
    return {
        "status": "ok",
        "engineVersion": _ENGINE_VERSION,
        **describe_client(orch.llm),
        "inferenceInputPolicy": (
            "reserved_synthetic_only"
            if settings.azure_openai_synthetic_only
            else "standard"
        ),
        "multiEngineEnabled": orch.multi_engine,
        "icd10Codes": orch.icd10.size,
        "currentFyLoaded": orch.icd10.has_current_fy,
        "ncciLoaded": bool(orch.ncci and orch.ncci.loaded),
        "coverageLoaded": bool(orch.coverage and orch.coverage.loaded),
        "cptDescriptors": orch.cpt.size if orch.cpt.loaded else 0,
        "cptStatus": _cpt_status(orch.cpt, settings),
        "denialPriors": orch.denial_priors.size,
        "encounters": len(_request_encounter_store(request).all()),
        "ta4h": orch.ta4h_observability(),
    }


@router.get("/api/session")
def session_capabilities(request: Request) -> dict[str, Any]:
    """Return server-derived views and narrowly scoped UI action capabilities."""
    principal = _request_principal(request)
    views = _permitted_views(principal)
    if not views:
        # Authentication already rejects principals without an allowed app
        # role. Keep this defensive branch fail-closed if that contract drifts.
        raise HTTPException(status_code=403, detail="access is not permitted")
    capabilities: dict[str, Any] = {
        "views": views,
        "defaultView": views[0],
        "capabilities": {
            # This is deliberately independent from the ability to see the
            # worklist. A Coder can inspect all three pilot tabs, but only a
            # BillingStaff or Admin membership may mutate Card Chase state.
            "manageChase": bool(
                "chase" in views
                and principal.has_any_role(("BillingStaff", "Admin"))
            ),
        },
    }
    if principal.has_any_role(("Admin",)):
        # Presence of this object is the browser's capability check. Validate
        # the bundled pack before advertising it and never make the UI infer
        # authorization from role names or duplicate the digest constant.
        from .chase.synthetic_demo import prepare_synthetic_chase_seed

        seed = prepare_synthetic_chase_seed()
        capabilities["actions"] = {
            "seedSyntheticChase": {
                "seedDigest": seed.digest,
                "caseCount": len(seed.cases),
            },
        }
    if _request_settings(request).billing_header_enabled and "provider" in views:
        capabilities["capabilities"]["billingHeader"] = True
        if _request_settings(request).provider_handoff_enabled and principal.has_any_role(("Provider", "Admin")):
            capabilities["capabilities"]["providerHandoff"] = True
    if _request_settings(request).athena_review_mode != "disabled" and principal.has_any_role(("Admin", "BillingStaff")):
        capabilities["capabilities"]["athenaReview"] = True
    return capabilities


@router.get("/api/ready")
def readiness(request: Request):
    orchestrator = getattr(request.app.state, "orchestrator", None)
    chase_context = getattr(request.app.state, "chase_context", None)
    datasets_loaded = bool(
        orchestrator
        and orchestrator.ncci
        and orchestrator.ncci.loaded
        and orchestrator.coverage
        and orchestrator.coverage.loaded
    )
    if (
        not getattr(request.app.state, "ready", False)
        or chase_context is None
        or not datasets_loaded
        or not getattr(request.app.state, "review_context_signing_key", None)
    ):
        return JSONResponse(status_code=503, content={"status": "warming"})
    try:
        chase_context["store"].healthcheck()
        scheduler = chase_context.get("scheduler")
        if scheduler is not None:
            scheduler.healthcheck()
        request.app.state.access_audit_store.healthcheck()
        request.app.state.encounter_store.healthcheck()
        request.app.state.feedback_store.healthcheck()
        model_policy = request.app.state.model_data_policy
        if model_policy is not None and "provider_completed_note" in model_policy.document.get("sources", {}):
            with request.app.state.encounter_store.context.connection() as conn:
                row = conn.execute("""SELECT has_function_privilege(current_user,
                    to_regprocedure('public.cosmaneura_provider_model_source(uuid,uuid,text,text)'),
                    'EXECUTE') AS allowed""").fetchone()
                if not row or row["allowed"] is not True:
                    raise RuntimeError("provider model source reader unavailable")
        if request.app.state.billing_header_store is not None:
            request.app.state.billing_header_store.healthcheck()
        if request.app.state.provider_handoff_store is not None:
            request.app.state.provider_handoff_store.healthcheck()
        if request.app.state.athena_review_service is not None:
            request.app.state.athena_review_service.store.healthcheck()
        membership_store = request.app.state.membership_store
        if membership_store is not None:
            membership_store.healthcheck()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {
        "status": "ready",
        "ncciLoaded": True,
        "coverageLoaded": True,
    }


@router.get("/api/live")
def liveness() -> dict[str, str]:
    """Shallow process liveness; it intentionally initializes no engine state."""
    return {"status": "alive"}


@router.get("/api/encounters")
def list_encounters(request: Request) -> list[dict[str, Any]]:
    _require_view(request, "provider")
    _request_orchestrator(request)
    return [{
        "id": b.encounter_id,
        "encounterType": b.encounter_type,
        "submittedEm": b.submitted_em_code,
        **_encounter_data_policy(row, request),
    } for row in _request_encounter_store(request).all() for b in [row.bundle]]


def _encounter_data_policy(stored, request=None) -> dict[str, Any]:
    from .model_admission import MODEL_SCOPED_SOURCES
    is_synthetic_demo = stored.source in READ_ONLY_SYNTHETIC_ENCOUNTER_SOURCES
    policy = {"allowed": False, "reason": "operational_source_model_not_approved"}
    if stored.source in MODEL_SCOPED_SOURCES and request is not None:
        try:
            with _inference_scope(request, stored):
                policy = {"allowed": True, "reason": None}
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
    return {
        "dataClass": (
            stored.source if is_synthetic_demo else "operational"
        ),
        "readOnly": is_synthetic_demo,
        **({"inferencePolicy": policy}
           if stored.source in MODEL_SCOPED_SOURCES else {}),
    }


def _get_stored_encounter(request: Request, encounter_id: str):
    stored = _request_encounter_store(request).get(encounter_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="unknown encounter")
    _set_audit_resource(request, stored.resource_id)
    return stored


def _require_inference_input_allowed(request: Request, *, stored=None) -> None:
    """Enforce the deployment's model-input policy before pipeline execution.

    The public-demo latch trusts only the server-owned provenance persisted with
    a stored encounter. A caller cannot opt an ad-hoc payload into the synthetic
    class and an encounter ID prefix has no authority over this decision.
    """
    from .model_admission import requires_model_scope
    settings = _request_settings(request)
    scoped = stored is not None and requires_model_scope(settings, stored.source)
    # Source read/storage approval never grants model-service disclosure.
    if (scoped
            and (getattr(request.app.state, "model_data_policy", None) is None
                 or (stored.source == ATHENA_REVIEW_OPERATIONAL_SOURCE
                     and settings.azure_openai_synthetic_only))):
        raise HTTPException(status_code=403, detail="operational_source_model_not_approved")
    if (
        settings.azure_openai_synthetic_only and not scoped
        and (stored is None or stored.source not in DEPLOYABLE_SYNTHETIC_INFERENCE_SOURCES)
    ):
        raise HTTPException(status_code=403, detail=_SYNTHETIC_INFERENCE_ONLY_DETAIL)
    if (getattr(request.app.state, "model_data_policy", None) is not None and not scoped
            and (stored is None or stored.source not in DEPLOYABLE_SYNTHETIC_INFERENCE_SOURCES)):
        raise HTTPException(403, "model_data_policy_requires_reviewed_source")


@contextmanager
def _inference_scope(request, stored):
    """Resolve source/model authority from the actual stored encounter and actor."""
    from .billing_header import HeaderActor
    from .model_admission import ModelAdmissionError, operational_scope, requires_model_scope
    _require_inference_input_allowed(request, stored=stored)
    settings = _request_settings(request)
    if not requires_model_scope(settings, stored.source):
        yield None
        return
    principal = _request_principal(request)
    encounter_store = _request_encounter_store(request)
    role = next((role for role in ("Admin", "Coder", "Provider")
                 if principal.has_any_role((role,))), None)
    if (not isinstance(encounter_store, PostgresEncounterStore) or role is None
            or principal.practice_id != settings.practice_id):
        raise HTTPException(403, "operational_source_model_not_approved")
    actor = HeaderActor(principal.tenant_id, principal.user_id,
                        f"{role} {principal.user_id[:8]}", role)
    try:
        with operational_scope(request.app.state.model_data_policy, settings=settings,
                stored=stored, actor=actor, context=encounter_store.context) as permit:
            yield permit
    except (ModelAdmissionError, InferenceInputPolicyError):
        raise HTTPException(403, "operational_source_model_not_approved") from None


def _process_stored(request, stored, *, orchestrator=None):
    with _inference_scope(request, stored) as permit:
        return (orchestrator or _request_orchestrator(request)).process(
            stored.bundle, user_id=_request_principal(request).user_id,
            input_provenance=stored.source, **({"input_scope": permit} if permit else {}),
        )


@router.post("/api/encounters/detail")
def get_encounter(request: Request, req: EncounterRequest) -> dict[str, Any]:
    _require_view(request, "provider")
    stored = _get_stored_encounter(request, req.encounterId)
    b = stored.bundle
    header_source = {}
    if _request_settings(request).billing_header_enabled:
        from .billing_header import source_snapshot
        header_source = {"headerSource": source_snapshot(stored)}
    return {"id": b.encounter_id, "encounterType": b.encounter_type,
            "encounterVersion": stored.version,
            "submittedEm": b.submitted_em_code, "intake": b.intake_text,
            "transcript": b.transcript, "signedNote": b.signed_note,
            "placeOfService": b.place_of_service,
            **_encounter_data_policy(stored, request), **header_source}


@router.post("/api/encounters/billing")
def bill_stored(request: Request, req: EncounterRequest) -> dict[str, Any]:
    _require_view(request, "provider")
    stored = _get_stored_encounter(request, req.encounterId)
    bundle = stored.bundle
    result = _process_stored(request, stored)
    return {**_billing_json(bundle, result), **_encounter_data_policy(stored, request)}


@router.post("/api/billing")
def bill_adhoc(request: Request, req: ProcessRequest) -> dict[str, Any]:
    _require_view(request, "provider")
    _require_inference_input_allowed(request)
    principal = _request_principal(request)
    bundle = VisitBundle(
        encounter_id=req.encounterId, patient_age=req.patientAge,
        patient_sex=req.patientSex, encounter_type=req.encounterType,
        place_of_service=req.placeOfService, intake_text=req.intake,
        transcript=req.transcript, signed_note=req.signedNote,
        submitted_em_code=req.submittedEm,
    )
    result = _request_orchestrator(request).process(
        bundle, user_id=principal.user_id
    )
    return _billing_json(bundle, result)


@router.post("/api/encounters/coder-view")
def coder_view(request: Request, req: EncounterRequest) -> dict[str, Any]:
    _require_view(request, "coder")
    signing_key = _request_review_signing_key(request)
    stored = _get_stored_encounter(request, req.encounterId)
    bundle = stored.bundle
    orch = _request_orchestrator(request)
    uid = _request_principal(request).user_id
    result = _process_stored(request, stored, orchestrator=orch)
    view = _coder_view_json(bundle, result, uid)
    view["encounterVersion"] = stored.version
    view["reviewContext"] = _issue_review_context(
        stored,
        _request_principal(request),
        signing_key=signing_key,
        recommendation_manifest=_coder_recommendation_manifest(view),
    )
    view.update(_encounter_data_policy(stored, request))
    # attach learned-history hints so the reviewer sees why a code was
    # refined or held ("you've declined this 3x")
    prefs = orch.preferences.for_user(uid)
    for row in ([view["em"]] if view["em"] else []) + view["dx"] + view["withheld"]:
        key = row["code"].replace(".", "").upper()
        if key in prefs.suppress:
            row["history"] = prefs.suppress[key].hint()
        elif key in prefs.refine:
            row["history"] = prefs.refine[key].hint()
    return view


@router.post("/api/encounters/billing-tab")
def billing_tab(request: Request, req: BillingTabRequest) -> dict[str, Any]:
    """The provider billing tab (F1): the same visit reorganised as a billing
    reference. The clinical note is never altered — this is a view over it."""
    _require_view(request, "provider")
    stored = _get_stored_encounter(request, req.encounterId)
    bundle = stored.bundle
    result = _process_stored(request, stored)
    return {
        **build_billing_tab_view(
            bundle, result, engine_version=_ENGINE_VERSION,
            generated_at=req.generatedAt,
        ),
        "encounterVersion": stored.version,
        **_encounter_data_policy(stored, request),
    }


@router.post("/api/completeness")
def completeness(request: Request, req: CompletenessRequest) -> dict[str, Any]:
    """Run a note-completeness rule pack before the claim reaches a coder.

    This is the pre-submission check: fixing a gap here means it never becomes a
    denial, and never costs a coder a review minute.
    """
    _require_view(request, "provider")
    pack = req.rulepack or applicable_rulepack(req.placeOfService, req.encounterType)
    if pack is None:
        return {"rulepack": None, "complete": None, "results": [],
                "attestationBlock": None,
                "detail": "No rule pack covers this encounter type yet."}
    try:
        report = check_note(req.note, rulepack=pack, overrides=req.overrides or None)
    except OverrideRequiresReason as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"unknown rule pack '{pack}'") from e
    return report.to_dict(pos=req.placeOfService)


def _validated_feedback(request: Request, fb: FeedbackModel):
    """Build an authenticated recommendation-bound review without persisting it."""
    principal = _require_roles(request, "Coder", "Admin")
    stored = _get_stored_encounter(request, fb.encounter_id)
    signing_key = _request_review_signing_key(request)
    try:
        review_context = _verify_review_context(
            fb.review_context, stored, principal, fb.encounter_version,
            signing_key=signing_key,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="review context is invalid; reload the review",
        ) from exc
    if not _feedback_matches_review_context(fb.decisions, review_context):
        raise HTTPException(
            status_code=422,
            detail="review decisions do not match the issued recommendations",
        )
    if (
        fb.submitted_em is not None
        and fb.submitted_em != review_context["submittedEm"]
    ):
        raise HTTPException(
            status_code=422,
            detail="submitted E/M does not match the issued review context",
        )
    em_decision = next(
        (decision for decision in fb.decisions if decision.kind == "EM"), None,
    )
    engine_em = (
        None if em_decision is None or em_decision.action == "add"
        else em_decision.engine_code
    )
    final_em = (
        None if em_decision is None or em_decision.action == "reject"
        else em_decision.final_code
    )
    record = EncounterFeedback(
        encounter_id=fb.encounter_id, coder_id=principal.user_id,
        coder_tenant_id=principal.tenant_id, review_id=str(fb.review_id),
        encounter_version=fb.encounter_version,
        engine_version=review_context["engineVersion"],
        submitted_em=review_context["submittedEm"],
        engine_em=engine_em, final_em=final_em,
        decisions=[coder_feedback.CodeDecision(**d.model_dump()) for d in fb.decisions],
        time_spent_seconds=fb.time_spent_seconds, overall_note=fb.overall_note,
    )
    return stored, record, review_context


@router.post("/api/feedback")
def submit_feedback(request: Request, fb: FeedbackModel) -> dict[str, Any]:
    stored, record, _review_context = _validated_feedback(request, fb)
    store = _request_feedback_store(request)
    try:
        if (
            isinstance(store, PostgresFeedbackStore)
            and request.app.state.atomic_feedback_audit
        ):
            created = store.append_with_audit(record, _completion_event(request))
            request.state.audit_completion_committed = True
        else:
            if store.exact_replay(record):
                created = False
            else:
                if stored.version != fb.encounter_version:
                    raise StaleEncounter(fb.encounter_id)
                created = store.append(record)
    except FeedbackConflict as exc:
        raise HTTPException(
            status_code=409, detail="review ID conflicts with an existing review",
        ) from exc
    except UnknownEncounter as exc:
        raise HTTPException(status_code=404, detail="unknown encounter") from exc
    except StaleEncounter as exc:
        raise HTTPException(
            status_code=409, detail="encounter changed; reload the review",
        ) from exc
    except ReviewerNotAuthorized as exc:
        raise HTTPException(status_code=403, detail="access is not permitted") from exc
    except coder_feedback.InvalidCurrentICD10 as exc:
        raise HTTPException(
            status_code=422, detail="final ICD-10 code is not verified as currently billable",
        ) from exc
    return {"ok": True, "reviewId": str(fb.review_id), "replayed": not created}


@router.post("/api/benchmarks/em")
def em_benchmarks(request: Request, req: EMBenchmarkRequest) -> dict[str, Any]:
    """State + national E/M mix and allowed amounts — population benchmarks for
    opportunity-sizing only, never an input to coding an encounter."""
    _require_view(request, "provider")
    from .benchmarks.em_benchmark import EMBenchmark, StateEMBenchmark
    nh = StateEMBenchmark()
    if req.state.upper() != nh.state:
        raise HTTPException(status_code=404, detail="no bundled benchmark for state")
    nat = EMBenchmark()
    specialty = req.specialty
    spec = specialty if specialty in (nh.specialties() if specialty else []) or specialty else None
    return {
        "state": nh.state,
        "specialty": specialty,
        "stateMix": nh.mix(specialty),
        "nationalMix": (nat.mix(specialty) if specialty in nat.specialties() else None),
        "stateAllowed": {c: nh.allowed(c, specialty)
                         for c in ("99212", "99213", "99214", "99215")},
        "specialtiesAvailable": nh.specialties(),
    }


@router.post("/api/benchmarks/payer-denials")
def payer_denials(request: Request, req: PayerDenialRequest) -> dict[str, Any]:
    """Issuer-level Marketplace denial rates (CMS TC-PUF PY2026) — the
    'how tough is this payer' prior."""
    _require_view(request, "provider")
    from .knowledge.denial_stats import PayerDenialRates
    rates = PayerDenialRates()
    rows = rates.for_state(req.state)
    return {"state": req.state.upper(), "source": "CMS Marketplace TC-PUF PY2026 (2024 claims)",
            "issuers": [{
                "name": r.issuer_name, "claimsReceived": r.claims_received,
                "denialRate": r.denial_rate, "topReason": r.top_reason or None,
                "topReasonShare": r.top_reason_share,
            } for r in rows]}


# --------------------------------------------------------------------------- #
# Insurance-data chase (F9). The state machine remains in-process, but every row
# mutation goes through a configured store. Production rejects the in-memory
# store, preventing replica divergence and restart data loss before messaging is
# enabled. The default transport remains inert until P1-P4 and the BAA land.
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_chase_context() -> dict[str, Any]:
    """Legacy no-argument context used by CLI callers and older tests."""
    return _build_chase_context(get_settings())


def _build_chase_context(settings: Settings) -> dict[str, Any]:
    from .chase import (
        ApprovalMode, ChasePolicy, SequenceRunner, chase_store_from_settings,
    )
    from .direct_contract import DirectContractList

    policy = ChasePolicy(
        practice_name=settings.practice_name,
        upload_url=settings.upload_url,
        approval_mode=ApprovalMode(settings.chase_approval),
    )
    context = {
        # This object is safe to cache: PostgresChaseStore carries only immutable
        # connection configuration. Chase rows themselves live in PostgreSQL.
        "store": chase_store_from_settings(settings),
        "policy": policy,
        # The default transport records without contacting anyone; real messaging
        # is gated on the deployment layer and the BAA.
        "runner": SequenceRunner(policy=policy),
        "roster": DirectContractList(settings=settings),
    }
    # The P4 repository is a second, deliberately narrower authority over the
    # shared chase schema. Construct it only for the fully durable application
    # topology so local/demo callers retain the legacy in-memory behavior.
    if settings.state_store == "postgres" and settings.chase_store == "postgres":
        from .chase.policy_fingerprint import (
            DeliveryConfiguration, policy_fingerprint,
        )
        from .chase.scheduler_store import PostgresChaseSchedulerRepository

        delivery = DeliveryConfiguration.from_settings(settings)
        context.update({
            "scheduler": PostgresChaseSchedulerRepository(
                settings.chase_dsn, practice_id=settings.practice_id,
            ),
            "delivery": delivery,
            "practice_timezone": settings.practice_timezone,
            "policy_fingerprint": policy_fingerprint(
                policy,
                practice_timezone=settings.practice_timezone,
                delivery=delivery,
            ),
        })
    return context


def _request_chase_context(request: Request) -> dict[str, Any]:
    context = getattr(request.app.state, "chase_context", None)
    if context is None:
        context = _build_chase_context(_request_settings(request))
        request.app.state.chase_context = context
    return context


def _scheduler_context(request: Request) -> tuple[dict[str, Any], Any]:
    context = _request_chase_context(request)
    scheduler = context.get("scheduler")
    if scheduler is None:
        raise HTTPException(
            status_code=503, detail="durable chase scheduling is unavailable",
        )
    return context, scheduler


def _scheduler_audit_event(request: Request):
    # P4 mutations and their allowed access completion are one PostgreSQL
    # transaction. Never silently fall back to a later best-effort append.
    if not request.app.state.atomic_chase_audit:
        raise HTTPException(
            status_code=503, detail="durable chase audit is unavailable",
        )
    return _completion_event(request)


def _raise_scheduler_http(exc: Exception) -> None:
    from .chase.scheduler_store import (
        SchedulerAuthorizationError, SchedulerConflict, SchedulerNotFound,
    )

    if isinstance(exc, SchedulerAuthorizationError):
        raise HTTPException(status_code=403, detail="access is not permitted") from exc
    if isinstance(exc, SchedulerNotFound):
        raise HTTPException(
            status_code=404, detail="scheduler resource was not found",
        ) from exc
    if isinstance(exc, SchedulerConflict):
        raise HTTPException(
            status_code=409,
            detail="scheduler action conflicts with the current state",
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def _chase_outbound_status(settings: Settings) -> dict[str, Any]:
    automatic_contact = bool(
        settings.chase_scheduler_enabled
        and settings.patient_messaging_enabled
        and settings.patient_messaging_operationally_activated
        and settings.chase_transport != "record_only"
    )
    return {
        "schedulerEnabled": settings.chase_scheduler_enabled,
        "patientMessagingEnabled": settings.patient_messaging_enabled,
        "operationallyActivated": settings.patient_messaging_operationally_activated,
        "transport": settings.chase_transport,
        "automaticContact": automatic_contact,
    }


def _chase_row_json(item) -> dict[str, Any]:
    """Minimum-necessary worklist row; contact and internal IDs stay separate."""
    from .chase.synthetic_demo import synthetic_case_for

    synthetic = synthetic_case_for(item)
    return {
        "chaseId": item.chase_id,
        "patientLabel": (
            synthetic.display_label if synthetic is not None else "Patient record"
        ),
        "dataClass": "synthetic_demo" if synthetic is not None else "operational",
        "trigger": item.trigger.value,
        "bucket": item.bucket,
        "detectedAt": item.detected_at,
        "appointmentAt": item.appointment_at,
        "state": item.state.value,
        "priority": item.priority,
        "reviewReason": item.review_reason,
        "assigned": bool(item.assignee),
        "touches": item.touches,
        "staffMinutes": item.staff_minutes,
        "hasPendingTask": item.pending_task is not None,
        "open": item.is_open,
    }


def _chase_task_json(
    item, settings: Settings, *, principal: AuthPrincipal,
) -> dict[str, Any]:
    """One audited contact task without copying operational destinations."""
    from .chase import State
    from .chase.synthetic_demo import synthetic_case_for

    synthetic = synthetic_case_for(item)
    needs_review = item.state is State.NEEDS_REVIEW
    awaiting_verification = item.state is State.SUBMITTED
    closed = not item.is_open
    synthetic_manual_task = bool(
        synthetic is not None
        and not settings.chase_scheduler_enabled
        and not settings.patient_messaging_enabled
        and not settings.patient_messaging_operationally_activated
        and settings.chase_transport == "record_only"
    )
    may_manage = principal.has_any_role(("BillingStaff", "Admin"))
    allowed_actions: list[str] = []
    if may_manage and not closed:
        allowed_actions.append("assign")
        if item.state is State.SUBMITTED:
            allowed_actions.append("verify")
        elif item.state is not State.NEEDS_REVIEW:
            allowed_actions.append("submitted")
            if (
                (
                    item.pending_task is not None
                    and item.pending_task.get("channel") == "phone"
                )
                or synthetic_manual_task
            ):
                allowed_actions.append("log_attempt")
    contact = None
    if synthetic is not None and not (needs_review or awaiting_verification or closed):
        contact = {
            "mode": "synthetic_demo",
            "channel": synthetic.contact.channel,
            "destination": synthetic.contact.destination,
        }
    if needs_review:
        instruction = item.review_reason
    elif awaiting_verification:
        instruction = "A submission is awaiting staff verification; do not contact again."
    elif closed:
        instruction = "This insurance follow-up task is closed."
    elif synthetic is not None:
        instruction = synthetic.contact.instruction
    else:
        instruction = (
            "Open the approved patient record in the practice source system. The contact "
            "destination is not copied into this application."
        )
    return {
        "chaseId": item.chase_id,
        "patientLabel": (
            synthetic.display_label if synthetic is not None else "Patient record"
        ),
        "dataClass": "synthetic_demo" if synthetic is not None else "operational",
        "state": item.state.value,
        "assigned": bool(item.assignee),
        "touches": item.touches,
        "staffMinutes": item.staff_minutes,
        "open": item.is_open,
        "hasPendingTask": item.pending_task is not None,
        "allowedActions": allowed_actions,
        "instruction": instruction,
        "contact": contact,
        "systemOfRecord": {
            "name": "Practice source system",
            "deepLinkConfigured": False,
            "instruction": (
                "Use the approved source-system patient-search workflow; no patient "
                "identifier is placed in a URL or application log."
            ),
        },
        "outbound": _chase_outbound_status(settings),
    }


def _chase_action_json(item) -> dict[str, Any]:
    """Minimum-necessary acknowledgement for a staff mutation."""
    return {
        "chaseId": item.chase_id,
        "state": item.state.value,
        "assigned": bool(item.assignee),
        "touches": item.touches,
        "staffMinutes": item.staff_minutes,
        "open": item.is_open,
    }


def _require_dormant_synthetic_chase(settings: Settings) -> None:
    if (
        settings.chase_scheduler_enabled
        or settings.patient_messaging_enabled
        or settings.patient_messaging_operationally_activated
        or settings.chase_transport != "record_only"
    ):
        raise HTTPException(
            status_code=409,
            detail="synthetic chase seed requires dormant record-only outreach",
        )


def _policy_record_json(record) -> dict[str, Any]:
    return {
        "policyRevisionId": record.policy_revision_id,
        "policyName": record.policy_name,
        "revisionNo": record.revision_no,
        "policyFingerprint": record.policy_fingerprint,
        "approvalMode": record.approval_mode,
    }


def _approval_record_json(record) -> dict[str, Any]:
    return {
        "approvalEventId": record.approval_event_id,
        "policyRevisionId": record.policy_revision_id,
        "policyFingerprint": record.policy_fingerprint,
        "eventType": record.event_type,
        "occurredAt": record.occurred_at.isoformat(),
    }


@router.post("/api/chase/worklist")
def chase_worklist(
    request: Request, req: ChaseWorklistRequest,
) -> dict[str, Any]:
    """The master list: one integrated view over missing, expired and eligibility."""
    _require_view(request, "chase")
    ctx = _request_chase_context(request)
    policy_view = ctx["policy"].to_dict()
    scheduler = ctx.get("scheduler")
    if scheduler is not None:
        try:
            durable = scheduler.policy_status(
                expected_policy_fingerprint=ctx["policy_fingerprint"],
            )
        except Exception as exc:
            _raise_scheduler_http(exc)
        # Minimal backend seam for the existing UI contract: durable approval
        # replaces the legacy process-local flag, while the PHI-free lifecycle
        # object remains available to future UI work without redesign here.
        policy_view.update({
            "approved": bool(durable.get("active")),
            "approved_by": None,
            "approved_at": durable.get("activatedAt") or None,
            "durable_status": durable,
        })
    rows = ctx["store"].rows(
        bucket=req.bucket,
        assignee=req.assignee,
        needs_person=req.needsPerson,
    )
    return {
        "counts": ctx["store"].counts(),
        "rows": [_chase_row_json(item) for item in rows],
        "policy": policy_view,
        "outbound": _chase_outbound_status(_request_settings(request)),
    }


@router.post("/api/chase/task")
def chase_task(request: Request, req: ChaseTargetRequest) -> dict[str, Any]:
    """Return one audited, minimum-necessary manual contact task."""
    principal = _require_view(request, "chase")
    store = _request_chase_context(request)["store"]
    item = store.get(req.chaseId)
    if item is None:
        raise HTTPException(status_code=404, detail="chase task was not found")
    if hasattr(store, "audit_resource_id"):
        _set_audit_resource(request, store.audit_resource_id(req.chaseId))
    result = _chase_task_json(
        item, _request_settings(request), principal=principal,
    )
    if _request_settings(request).athena_review_mode != "disabled" and principal.has_any_role(("Admin", "BillingStaff")):
        result["athenaReview"] = {"available": True, "mode": _request_settings(request).athena_review_mode,
                                 "billingAvailable": True}
    return result


@router.post("/api/chase/demo-seed")
def chase_demo_seed(
    request: Request, req: ChaseDemoSeedRequest,
) -> dict[str, Any]:
    """Install only the fixed four-case synthetic pack while outreach is dormant."""
    principal = _require_roles(request, "Admin")
    settings = _request_settings(request)
    _require_dormant_synthetic_chase(settings)
    from .chase import Channel
    from .chase.synthetic_demo import prepare_synthetic_chase_seed

    seed = prepare_synthetic_chase_seed()
    if not hmac.compare_digest(req.seedDigest, seed.digest):
        raise HTTPException(status_code=409, detail="synthetic chase seed digest mismatch")
    context = _request_chase_context(request)
    from .chase.synthetic_demo import synthetic_case_for

    existing = [context["store"].get(item.chase_id) for item in seed.items]
    if any(item is not None for item in existing):
        if not all(
            item is not None and synthetic_case_for(item) is not None
            for item in existing
        ):
            raise HTTPException(
                status_code=409,
                detail="synthetic chase seed conflicts with existing chase state",
            )
        return {
            "seedId": seed.seed_id,
            "seedDigest": seed.digest,
            "installedCaseCount": len(existing),
            "openTaskCount": sum(
                item.is_open for item in existing if item is not None
            ),
            "replayed": True,
            "outbound": _chase_outbound_status(settings),
        }
    scheduler = context.get("scheduler")
    items = seed.items
    if scheduler is not None:
        from .chase.scheduler_store import ContactRouteInput, ContactSnapshotInput

        observed_at = datetime.fromisoformat(seed.detected_at)
        entries = []
        for case, item in zip(seed.cases, items):
            snapshot_id = str(uuid5(
                NAMESPACE_URL,
                ":".join((seed.digest, case.case_id)),
            ))
            entries.append((
                item,
                ContactSnapshotInput(
                    contact_snapshot_id=snapshot_id,
                    source_system="synthetic.digest_pinned_demo",
                    source_version=seed.digest,
                    observed_at=observed_at,
                    verified_at=observed_at,
                    verification_method="staff",
                    routes=(ContactRouteInput(
                        Channel.PHONE,
                        case.contact.destination,
                        "not_required",
                    ),),
                    verified_by_tenant_id=principal.tenant_id,
                    verified_by_user_id=principal.user_id,
                ),
            ))
        try:
            scheduler.persist_detection_batch(
                entries,
                audit_event=_scheduler_audit_event(request),
            )
            request.state.audit_completion_committed = True
        except Exception as exc:
            _raise_scheduler_http(exc)
        installed = [context["store"].get(item.chase_id) for item in items]
        if not all(
            item is not None and synthetic_case_for(item) is not None
            for item in installed
        ):
            raise HTTPException(
                status_code=503,
                detail="synthetic chase seed confirmation is unavailable",
            )
    else:
        installed = context["store"].extend(items)
    return {
        "seedId": seed.seed_id,
        "seedDigest": seed.digest,
        "installedCaseCount": len(installed),
        "openTaskCount": sum(
            item.is_open for item in installed if item is not None
        ),
        "replayed": False,
        "outbound": _chase_outbound_status(settings),
    }


@router.get("/api/chase/policy/status")
def chase_policy_status(request: Request) -> dict[str, Any]:
    """Return only the configured policy's PHI-free lifecycle metadata."""
    _require_roles(request, "Admin")
    context, scheduler = _scheduler_context(request)
    try:
        return scheduler.policy_status(
            expected_policy_fingerprint=context["policy_fingerprint"],
        )
    except Exception as exc:
        _raise_scheduler_http(exc)


@router.post("/api/chase/policy/create")
def chase_policy_create(
    request: Request, req: ChasePolicyCreateRequest,
) -> dict[str, Any]:
    principal = _require_roles(request, "Admin")
    context, scheduler = _scheduler_context(request)
    try:
        record = scheduler.create_policy(
            context["policy"], context["delivery"],
            practice_timezone=context["practice_timezone"],
            policy_revision_id=str(req.policy_revision_id),
            actor_tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            audit_event=_scheduler_audit_event(request),
        )
        request.state.audit_completion_committed = True
        return _policy_record_json(record)
    except Exception as exc:
        _raise_scheduler_http(exc)


@router.post("/api/chase/policy/approve")
def chase_policy_approve(
    request: Request, req: ChasePolicyApprovalRequest,
) -> dict[str, Any]:
    principal = _require_roles(request, "Admin")
    context, scheduler = _scheduler_context(request)
    try:
        record = scheduler.approve_policy(
            context["policy"], context["delivery"],
            practice_timezone=context["practice_timezone"],
            approval_event_id=str(req.approval_event_id),
            policy_revision_id=str(req.policy_revision_id),
            expected_policy_fingerprint=context["policy_fingerprint"],
            actor_tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            reason=req.reason,
            audit_event=_scheduler_audit_event(request),
        )
        request.state.audit_completion_committed = True
        return _approval_record_json(record)
    except Exception as exc:
        _raise_scheduler_http(exc)


@router.post("/api/chase/policy/activate")
def chase_policy_activate(
    request: Request, req: ChasePolicyActivationRequest,
) -> dict[str, Any]:
    principal = _require_roles(request, "Admin")
    context, scheduler = _scheduler_context(request)
    try:
        scheduler.activate_policy(
            context["policy"], context["delivery"],
            practice_timezone=context["practice_timezone"],
            policy_revision_id=str(req.policy_revision_id),
            expected_policy_fingerprint=context["policy_fingerprint"],
            approval_event_id=str(req.approval_event_id),
            actor_tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            audit_event=_scheduler_audit_event(request),
        )
        request.state.audit_completion_committed = True
        return {"status": "active"}
    except Exception as exc:
        _raise_scheduler_http(exc)


@router.post("/api/chase/policy/revoke")
def chase_policy_revoke(
    request: Request, req: ChasePolicyRevocationRequest,
) -> dict[str, Any]:
    principal = _require_roles(request, "Admin")
    context, scheduler = _scheduler_context(request)
    try:
        record = scheduler.revoke_policy(
            approval_event_id=str(req.approval_event_id),
            policy_revision_id=str(req.policy_revision_id),
            expected_policy_fingerprint=context["policy_fingerprint"],
            actor_tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            reason=req.reason,
            audit_event=_scheduler_audit_event(request),
        )
        request.state.audit_completion_committed = True
        return _approval_record_json(record)
    except Exception as exc:
        _raise_scheduler_http(exc)


@router.post("/api/chase/detect")
def chase_detect(request: Request, req: ChaseDetectRequest) -> dict[str, Any]:
    """Run detection over posted coverage snapshots and merge into the worklist.

    Re-running is safe: an open row for the same patient and trigger is refreshed
    rather than duplicated, so daily detection does not inflate the backlog.
    """
    from .chase import Channel, CoverageSnapshot, detect, detect_all

    _require_chase_staff(request)
    ctx = _request_chase_context(request)
    if (
        ctx.get("scheduler") is not None
        and any(snapshot.get("sms_consent") is True for snapshot in req.snapshots)
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "posted detection cannot grant SMS consent; consent must come "
                "from the trusted opt-in feed"
            ),
        )
    snapshots = [CoverageSnapshot(**s) for s in req.snapshots]
    store = ctx["store"]
    scheduler = ctx.get("scheduler")
    if scheduler is not None:
        from .chase.scheduler_store import (
            ContactRouteInput, ContactSnapshotInput,
        )

        principal = _request_principal(request)
        detected_at = datetime.now(timezone.utc)
        entries = []
        for snapshot in snapshots:
            item = detect(
                snapshot,
                today=req.today,
                now=lambda: detected_at.isoformat(),
                roster=ctx["roster"],
            )
            if item is None:
                continue
            routes = []
            if snapshot.email:
                routes.append(ContactRouteInput(
                    Channel.EMAIL, snapshot.email, "not_required",
                ))
            if snapshot.portal_enrolled:
                routes.append(ContactRouteInput(
                    Channel.PORTAL, snapshot.patient_ref, "not_required",
                ))
            if snapshot.phone:
                # A posted coverage snapshot is not an opt-in authority. Keep
                # SMS fail-closed while preserving the manual staff-call route.
                routes.extend((
                    ContactRouteInput(Channel.SMS, snapshot.phone, "unknown"),
                    ContactRouteInput(
                        Channel.PHONE, snapshot.phone, "not_required",
                    ),
                ))
            contact = ContactSnapshotInput(
                contact_snapshot_id=str(uuid4()),
                source_system="api.coverage_snapshot",
                source_version=req.today,
                observed_at=detected_at,
                verified_at=detected_at,
                verification_method="staff",
                routes=tuple(routes),
                verified_by_tenant_id=principal.tenant_id,
                verified_by_user_id=principal.user_id,
            )
            entries.append((item, contact))
        entries.sort(key=lambda entry: (-entry[0].priority, entry[0].patient_ref))
        try:
            results = scheduler.persist_detection_batch(
                entries, audit_event=_scheduler_audit_event(request),
            )
            request.state.audit_completion_committed = True
        except Exception as exc:
            _raise_scheduler_http(exc)
        return {"checked": len(snapshots), "detected": len(results)}

    found = detect_all(snapshots, today=req.today, roster=ctx["roster"])
    if request.app.state.atomic_chase_audit:
        worklist = store.extend_and_snapshot(
            found, audit_event=_completion_event(request),
        )
        request.state.audit_completion_committed = True
    else:
        store.extend(found)
        worklist = store.to_dict()
    return {"checked": len(snapshots), "detected": len(found), **worklist}


@router.post("/api/chase/metrics")
def chase_metrics(
    request: Request, req: ChaseMetricsRequest,
) -> dict[str, Any]:
    from .chase.metrics import baseline_comparison

    _require_view(request, "chase")
    return baseline_comparison(
        _request_chase_context(request)["store"].all_items(),
        baseline_minutes_per_card=req.baselineMinutesPerCard,
        baseline_claims_held_monthly=req.baselineClaimsHeldMonthly)


@router.post("/api/chase/release-message")
def chase_release_message(
    request: Request,
    req: ChaseMessageReleaseRequest,
) -> dict[str, Any]:
    """Admin release of the current exact per-message cadence rung."""
    chase_id = req.chaseId
    principal = _require_roles(request, "Admin")
    context, scheduler = _scheduler_context(request)
    store = context["store"]
    if store.get(chase_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown chase '{chase_id}'")
    if hasattr(store, "audit_resource_id"):
        _set_audit_resource(request, store.audit_resource_id(chase_id))
    try:
        target = scheduler.message_release_target(
            release_id=str(req.release_id),
            chase_id=chase_id,
            expected_policy_fingerprint=context["policy_fingerprint"],
            actor_tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            reason=req.reason,
        )
        record = scheduler.release_message(
            release_id=str(req.release_id),
            chase_id=chase_id,
            policy_revision_id=target["policy_revision_id"],
            expected_policy_fingerprint=context["policy_fingerprint"],
            step_ordinal=target["step_ordinal"],
            contact_snapshot_id=target["contact_snapshot_id"],
            actor_tenant_id=principal.tenant_id,
            actor_user_id=principal.user_id,
            reason=req.reason,
            audit_event=_scheduler_audit_event(request),
        )
        request.state.audit_completion_committed = True
        return {"status": "released", "releaseId": record.release_id}
    except Exception as exc:
        _raise_scheduler_http(exc)


@router.post("/api/chase/action")
def chase_action(request: Request, req: ChaseActionRequest) -> dict[str, Any]:
    """Staff actions on one row.

    `release` and `suppress` are the human decisions; `log_attempt` is how a
    phone call enters the contact history; `submitted` and `verify` are the
    completion loop, and only `verify` closes a row.
    """
    from .chase import Channel

    chase_id = req.chaseId
    principal = _require_chase_staff(request)
    ctx = _request_chase_context(request)
    store, runner, action = ctx["store"], ctx["runner"], req.action
    if action == "release":
        # Direct-contract release is Admin-only in every topology. Keep this
        # check ahead of store access so the memory/demo path cannot be less
        # restrictive than the durable scheduler path.
        principal = _require_roles(request, "Admin")
    actor = principal.user_id
    existing = store.get(chase_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"unknown chase '{chase_id}'")
    if action == "log_attempt" and req.channel not in {None, "phone"}:
        raise HTTPException(
            status_code=422,
            detail="staff tasks can only log a manual phone call",
        )
    if hasattr(store, "audit_resource_id"):
        _set_audit_resource(request, store.audit_resource_id(chase_id))
    scheduler = ctx.get("scheduler")
    settings = _request_settings(request)
    from .chase.synthetic_demo import synthetic_case_for
    synthetic_manual_task = bool(
        synthetic_case_for(existing) is not None
        and not settings.chase_scheduler_enabled
        and not settings.patient_messaging_enabled
        and not settings.patient_messaging_operationally_activated
        and settings.chase_transport == "record_only"
    )
    if scheduler is not None and action == "release":
        gate_fingerprint = str(
            (existing.direct_contract or {}).get("gate_fingerprint") or ""
        )
        try:
            record = scheduler.release_direct_contract(
                review_release_id=str(req.action_id),
                chase_id=chase_id,
                gate_fingerprint=gate_fingerprint,
                actor_tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                reason=req.reason,
                audit_event=_scheduler_audit_event(request),
            )
            request.state.audit_completion_committed = True
            return {
                "chaseId": chase_id,
                "status": "released",
                "releaseId": record.release_id,
            }
        except HTTPException:
            raise
        except Exception as exc:
            _raise_scheduler_http(exc)
    if (
        scheduler is not None
        and action == "log_attempt"
        and not synthetic_manual_task
    ):
        try:
            result = scheduler.complete_staff_task(
                ctx["policy"], ctx["delivery"],
                practice_timezone=ctx["practice_timezone"],
                chase_id=chase_id,
                action_id=str(req.action_id),
                actor_tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                outcome=req.outcome or "reached",
                minutes_spent=req.minutes_spent,
                note=req.note,
                audit_event=_scheduler_audit_event(request),
            )
            request.state.audit_completion_committed = True
            return {
                "chaseId": result.chase_id,
                "status": "completed",
                "attemptRecorded": result.attempt_recorded,
                "cadenceAdvanced": result.cadence_advanced,
            }
        except HTTPException:
            raise
        except Exception as exc:
            _raise_scheduler_http(exc)
    assignee = req.assignee or actor
    membership_store = request.app.state.membership_store
    atomic_assignee_check = bool(
        action == "assign"
        and isinstance(store, PostgresChaseStore)
        and isinstance(membership_store, PostgresMembershipStore)
    )

    def transition(item):
        from .chase import State

        if not item.is_open:
            raise ValueError("the chase task is already closed")
        if action == "release":
            if item.state is not State.NEEDS_REVIEW:
                raise ValueError("only a task awaiting review can be released")
            runner.release(item, by=actor, reason=req.reason)
        elif action == "suppress":
            runner.suppress(item, by=actor, reason=req.reason)
        elif action == "assign":
            selected_assignee = assignee
            if membership_store is not None and not atomic_assignee_check:
                selected_assignee = membership_store.validate_assignee(assignee)
            item.assignee = selected_assignee
        elif action == "log_attempt":
            channel = Channel(req.channel or "phone")
            if (
                item.state is State.NEEDS_REVIEW
                or not (
                    (
                        item.pending_task is not None
                        and item.pending_task.get("channel") == channel.value
                    )
                    or synthetic_manual_task
                )
            ):
                raise ValueError(
                    "a staff attempt requires an approved manual contact task"
                )
            runner.log_staff_attempt(
                item, channel=channel,
                template_id=req.template_id or "phone.call", actor=actor,
                outcome=req.outcome or "reached",
                minutes_spent=req.minutes_spent, note=req.note)
        elif action == "submitted":
            if item.state is State.NEEDS_REVIEW:
                raise ValueError("a task awaiting review must be released first")
            if item.state is State.SUBMITTED:
                raise ValueError("the submission is already awaiting verification")
            runner.mark_submitted(item, note=req.note)
        elif action == "verify":
            if item.state is not State.SUBMITTED:
                raise ValueError("only a submitted task can be verified")
            runner.close_verified(item, verified_by=actor,
                                  source_write_id=req.source_write_id,
                                  eligibility_status=req.eligibility_status)
        else:
            raise HTTPException(status_code=400, detail=f"unknown action '{action}'")

    action_payload = {
        "actorTenantId": principal.tenant_id,
        "actorUserId": principal.user_id,
        "chaseId": chase_id,
        "request": req.model_dump(mode="json", exclude={"action_id"}),
    }
    action_hash = hashlib.sha256(json.dumps(
        action_payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    def validate_assignment_in_transaction(conn) -> None:
        if atomic_assignee_check:
            membership_store.validate_assignee(assignee, connection=conn)

    try:
        if request.app.state.atomic_chase_audit:
            item = store.mutate(
                chase_id, transition, audit_event=_completion_event(request),
                action_id=str(req.action_id), payload_hash=action_hash,
                transaction_check=(
                    validate_assignment_in_transaction
                    if atomic_assignee_check else None
                ),
            )
            if item is not None:
                request.state.audit_completion_committed = True
        else:
            item = store.mutate(
                chase_id, transition, action_id=str(req.action_id),
                payload_hash=action_hash,
                transaction_check=(
                    validate_assignment_in_transaction
                    if atomic_assignee_check else None
                ),
            )
    except ChaseActionConflict as exc:
        raise HTTPException(
            status_code=409, detail="action ID conflicts with an existing action",
        ) from exc
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"unknown chase '{chase_id}'")
    return _chase_action_json(item)


@router.get("/api/feedback/summary")
def feedback_summary(request: Request) -> dict[str, Any]:
    _require_roles(request, "Admin")
    feedbacks = _request_feedback_store(request).load()
    if not feedbacks:
        return {"encounters": 0, "summary": None, "tuningBacklog": [], "goldRows": 0}
    return {
        "encounters": len(feedbacks),
        "summary": coder_feedback.summarize(feedbacks),
        "tuningBacklog": coder_feedback.tuning_signals(feedbacks),
        "goldRows": len(coder_feedback.export_gold(feedbacks)),
    }


@asynccontextmanager
async def _lifespan(application: FastAPI):
    """Warm heavyweight data before the replica can report ready."""
    application.state.ready = False
    application.state.startup_error = None
    try:
        settings = application.state.settings
        # Athena and staff actions share the same composed worklist for the
        # entire app lifetime, including local lifespan re-entry.
        # Force one database round trip so readiness also proves the durable
        # chase schema exists; a configured-but-unreachable store is not ready.
        application.state.chase_context["store"].healthcheck()
        scheduler = application.state.chase_context.get("scheduler")
        if scheduler is not None:
            scheduler.healthcheck()
        application.state.access_audit_store.healthcheck()
        application.state.encounter_store.healthcheck()
        application.state.feedback_store.healthcheck()
        if application.state.billing_header_store is not None:
            application.state.billing_header_store.healthcheck()
        if application.state.provider_handoff_store is not None:
            application.state.provider_handoff_store.healthcheck()
        if application.state.athena_review_service is not None:
            application.state.athena_review_service.store.healthcheck()
        if application.state.membership_store is not None:
            application.state.membership_store.healthcheck()
        orchestrator = get_orchestrator(
            settings, feedback_store=application.state.feedback_store,
        )
        if not (
            orchestrator.ncci
            and orchestrator.ncci.loaded
            and orchestrator.coverage
            and orchestrator.coverage.loaded
        ):
            raise RuntimeError("required regulatory datasets are unavailable")
        application.state.orchestrator = orchestrator
    except Exception as exc:
        # Record only the class; configuration values and data never enter state
        # that a generic health surface might later expose.
        application.state.startup_error = type(exc).__name__
        raise
    application.state.ready = True
    try:
        yield
    finally:
        application.state.ready = False
        active_orchestrator = application.state.orchestrator
        shutdown_orchestrator = getattr(active_orchestrator, "shutdown", None)
        if callable(shutdown_orchestrator):
            try:
                shutdown_orchestrator()
            except Exception:
                # Shadow shutdown is bounded and best effort; no secondary
                # exception may delay or escape service termination.
                pass
        llm = getattr(active_orchestrator, "llm", None)
        shutdown_metrics = getattr(llm, "shutdown_metrics", None)
        if callable(shutdown_metrics):
            try:
                shutdown_metrics()
            except Exception:
                # Metric flushing is outside claim processing and shutdown must
                # never expose an SDK exception or deployment credential.
                pass
        application.state.orchestrator = None


def create_app(
    *,
    settings: Settings | None = None,
    frontend_dist: str | os.PathLike[str] | None = None,
    access_audit_store: AccessAuditStore | None = None,
    feedback_store=None,
    encounter_store=None,
    membership_store=None,
    rate_limiter: FixedWindowLimiter | None = None,
    auth_failure_limiter: FixedWindowLimiter | None = None,
) -> FastAPI:
    """Compose one API instance and its same-origin static frontend."""
    config = settings or get_settings()
    http_logger = configure_json_logging(config.log_level)
    if config.environment in {"staging", "production"} and any(
        override is not None for override in (
            access_audit_store, feedback_store, encounter_store, membership_store,
        )
    ):
        raise ValueError(
            "deployed applications cannot override required Postgres stores"
        )
    postgres = postgres_services(config) if config.state_store == "postgres" else None
    default_encounters = (
        postgres.encounters if postgres else MemoryEncounterStore(
            get_encounters().values(), practice_id=config.practice_id or "local",
        )
    )
    default_feedback = (
        postgres.feedback if postgres else FeedbackStore(config.feedback_path)
    )
    default_audit = postgres.audit if postgres else MemoryAccessAuditStore()
    default_memberships = postgres.memberships if postgres else None
    selected_encounters = encounter_store or default_encounters
    selected_feedback = feedback_store or default_feedback
    audit_store = access_audit_store or default_audit
    selected_memberships = (
        membership_store if membership_store is not None else default_memberships
    )
    principal_limiter = rate_limiter or FixedWindowLimiter()
    failed_auth_limiter = auth_failure_limiter or FixedWindowLimiter()
    denied_auditor = DeniedAccessAuditor(settings=config, store=audit_store)
    application = FastAPI(
        title="Cosmas Billing Engine",
        version="0.1.0",
        lifespan=_lifespan,
    )
    application.state.settings = config
    from .model_admission import parse_policy
    application.state.model_data_policy = parse_policy(config.model_data_policy_json)
    # Local-only ephemeral keys invalidate reviews after restart. Durable or
    # deployed runtimes require the same configured secret across all replicas.
    application.state.review_context_signing_key = (
        config.review_context_signing_key.encode("ascii")
        if config.review_context_signing_key else
        secrets.token_bytes(32) if config.state_store == "local"
        and config.environment in {"development", "test"} else None
    )
    application.state.ready = False
    application.state.startup_error = None
    application.state.orchestrator = None
    application.state.chase_context = _build_chase_context(config)
    application.state.access_audit_store = audit_store
    application.state.encounter_store = selected_encounters
    application.state.feedback_store = selected_feedback
    application.state.membership_store = selected_memberships
    from .billing_header_routes import configure_header, router as header_router
    configure_header(application, config, selected_encounters)
    from .provider_handoff_routes import configure_handoff, router as handoff_router
    configure_handoff(application, config, selected_encounters)
    from .athena_review_routes import configure_athena, router as athena_router
    configure_athena(application, config, application.state.chase_context, selected_encounters)
    application.state.atomic_feedback_audit = bool(
        postgres
        and selected_feedback is postgres.feedback
        and audit_store is postgres.audit
    )
    application.state.atomic_chase_audit = bool(
        postgres
        and audit_store is postgres.audit
        and config.chase_store == "postgres"
        and config.chase_dsn == config.database_url
    )
    application.state.rate_limiter = principal_limiter
    application.state.auth_failure_limiter = failed_auth_limiter

    # Starlette's last-added middleware is outermost. Request order is:
    # Security headers -> PHI-safe runtime -> exact CORS -> origin -> auth ->
    # audit -> principal rate -> body cap -> API.
    application.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=config.max_request_bytes,
    )
    application.add_middleware(
        PrincipalRateLimitMiddleware,
        settings=config,
        limiter=principal_limiter,
    )
    application.add_middleware(
        AccessAuditMiddleware,
        settings=config,
        store=audit_store,
    )
    application.add_middleware(
        EntraAuthMiddleware,
        settings=config,
        failure_limiter=failed_auth_limiter,
        denied_auditor=denied_auditor,
        principal_authorizer=(
            selected_memberships.authorize if selected_memberships is not None else None
        ),
    )
    application.add_middleware(
        StrictOriginMiddleware,
        allowed_origins=config.cors_origins,
        denied_auditor=denied_auditor,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.cors_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["Retry-After", "X-Request-ID"],
        allow_credentials=False,
    )
    application.add_middleware(PHISafeHTTPMiddleware, logger=http_logger)
    application.add_middleware(
        SecurityHeadersMiddleware,
        deployed_https=config.environment in {"staging", "production"},
    )
    application.include_router(router)
    application.include_router(header_router)
    application.include_router(handoff_router)
    application.include_router(athena_router)

    async def persistence_unavailable(_request: Request, _exc: PersistenceUnavailable):
        return JSONResponse(
            status_code=503, content={"detail": "persistent storage is unavailable"},
        )

    application.add_exception_handler(
        PersistenceUnavailable, persistence_unavailable,
    )

    configured_dist = (
        frontend_dist if frontend_dist is not None else config.frontend_dist
    )
    dist = (
        Path(configured_dist)
        if configured_dist is not None
        else Path(__file__).resolve().parents[1] / "frontend" / "dist"
    )
    if (dist / "index.html").is_file():
        # Mounted last so frontend routes cannot shadow /api/*.
        application.mount(
            "/", StaticFiles(directory=dist, html=True), name="frontend"
        )
    elif config.environment == "production":
        raise RuntimeError(
            "production requires a built same-origin frontend; configure "
            "COSMANEURA_FRONTEND_DIST"
        )
    return application


app = create_app()
