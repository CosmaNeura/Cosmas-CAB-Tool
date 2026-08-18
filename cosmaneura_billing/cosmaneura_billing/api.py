"""FastAPI service wrapping the billing engine — the demoable backend.

Endpoints (shapes match the documented contracts in frontend/*.jsx):

  GET  /api/health                        engine + knowledge-base status
  GET  /api/encounters                    encounters available to demo
  GET  /api/encounters/{id}               one encounter's documentation
  POST /api/encounters/{id}/billing       run the pipeline on a stored encounter
  POST /api/billing                       run the pipeline on posted documentation
  GET  /api/encounters/{id}/coder-view    pre-coded claim for the coder review UI
  POST /api/feedback                      persist a coder's review (JSONL store)
  GET  /api/feedback/summary              accuracy metrics + tuning backlog

Run:  uvicorn cosmaneura_billing.api:app --reload
The orchestrator and its regulatory knowledge bases are warmed during process
startup. Readiness remains unavailable until that warmup and the durable chase
schema check both succeed.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import coder_feedback
from .access_audit import (
    AccessAuditMiddleware, AccessAuditStore, FileAccessAuditStore,
    MemoryAccessAuditStore, DeniedAccessAuditor,
)
from .auth import AuthPrincipal, EntraAuthMiddleware
from .billing_tab import build_billing_tab_view
from .coder_feedback import EncounterFeedback, FeedbackStore
from .completeness import OverrideRequiresReason, applicable_rulepack, check_note
from .corpus.acibench import load_acibench
from .corpus.demo import SAMPLE_VISIT
from .llm import describe_client
from .orchestrator import BillingOrchestrator
from .schemas import BillingResult, CodeKind, CodeSuggestion, Disposition, VisitBundle
from .security import (
    FixedWindowLimiter, PrincipalRateLimitMiddleware,
    RequestBodyLimitMiddleware, StrictOriginMiddleware,
)
from .settings import Settings, get_settings

router = APIRouter()

_KIND = {CodeKind.EM: "EM", CodeKind.ICD10: "ICD10",
         CodeKind.PROCEDURE: "PROCEDURE", CodeKind.MODIFIER: "MODIFIER"}
_ENGINE_VERSION = "cpt2026u4;icd10-FY2026;ncci-2026Q3-v32.2;mcd-NGS-2026-07-20"


# --------------------------------------------------------------------------- #
# Engine + encounter store (lazy singletons)
# --------------------------------------------------------------------------- #
def get_orchestrator(settings: Settings | None = None) -> BillingOrchestrator:
    """Build an engine using one resolved application configuration."""
    return BillingOrchestrator(settings=settings or get_settings())


@lru_cache(maxsize=1)
def get_encounters() -> dict[str, VisitBundle]:
    store: dict[str, VisitBundle] = {SAMPLE_VISIT.encounter_id: SAMPLE_VISIT}
    try:
        for b in load_acibench("train")[:10]:
            store[b.encounter_id] = b
    except FileNotFoundError:
        pass
    return store


def get_feedback_store(settings: Settings | None = None) -> FeedbackStore:
    return FeedbackStore((settings or get_settings()).feedback_path)


def _request_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def _request_orchestrator(request: Request) -> BillingOrchestrator:
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        # TestClient calls outside a lifespan still use stable, app-local state.
        orchestrator = get_orchestrator(_request_settings(request))
        request.app.state.orchestrator = orchestrator
    return orchestrator


def _request_principal(request: Request) -> AuthPrincipal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, AuthPrincipal):
        raise HTTPException(status_code=401, detail="authentication required")
    return principal


def _require_roles(request: Request, *roles: str) -> AuthPrincipal:
    principal = _request_principal(request)
    if not principal.has_any_role(roles):
        raise HTTPException(status_code=403, detail="access is not permitted")
    return principal


# --------------------------------------------------------------------------- #
# Serializers -> the frontend data contracts
# --------------------------------------------------------------------------- #
def _level(code: str | None) -> int:
    return int(code[-1]) if code and code[-1:].isdigit() else 0


def _code_json(s: CodeSuggestion, *, submitted_em: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": _KIND.get(s.kind, str(s.kind)),
        "code": s.code,
        "label": s.label,
        "disposition": s.disposition.value,
        "denialRisk": s.denial_risk,
        "evidence": [{"source": e.source, "quote": e.quote} for e in s.evidence],
    }
    if "primary_candidate" in s.notes:
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
    sub = bundle.submitted_em_code
    return {
        "encounterId": bundle.encounter_id,
        "summary": card.summary,
        "captured": [_code_json(s, submitted_em=sub) for s in card.captured_codes],
        "declined": [_code_json(s) for s in card.declined_codes],
        "em": ({"code": ws.em_code.code, "label": ws.em_code.label} if ws.em_code else None),
        "primary": ({"code": ws.primary_diagnosis.code, "label": ws.primary_diagnosis.label}
                    if ws.primary_diagnosis else None),
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

    def row(s: CodeSuggestion) -> dict[str, Any]:
        return {
            "id": re.sub(r"[^a-z0-9]", "", s.code.lower()) or "x",
            "code": s.code, "label": s.label,
            "primary": "primary_candidate" in s.notes or None,
            "why": s.rationale,
            "evidence": s.evidence[0].quote if s.evidence else "",
        }

    em = next((s for s in card.captured_codes if s.kind == CodeKind.EM), None)
    dx = [s for s in card.captured_codes if s.kind == CodeKind.ICD10]
    withheld = [{**row(s), "status": "rejected" if s.disposition == Disposition.REJECTED
                 else "held"} for s in card.declined_codes]
    return {
        "encounterId": bundle.encounter_id,
        "engineVersion": _ENGINE_VERSION,
        "submittedEm": bundle.submitted_em_code,
        "coderId": coder_id,
        "em": row(em) if em else None,
        "dx": [row(s) for s in dx],
        "withheld": withheld,
        **result.inference,
        "degraded": _degraded_json(result),
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    # Raw CoverageSnapshot fields — the adapter boundary standing in for athenaNet.
    snapshots: list[dict[str, Any]] = Field(default_factory=list, max_length=5_000)


class ChaseActionRequest(StrictRequest):
    action: str = Field(min_length=1, max_length=32)
    assignee: str = Field(default="", max_length=128)
    reason: str = Field(default="", max_length=4_000)
    channel: Optional[str] = Field(default=None, max_length=32)
    template_id: Optional[str] = Field(default=None, max_length=128)
    outcome: Optional[str] = Field(default=None, max_length=128)
    minutes_spent: float = Field(default=0.0, ge=0, le=1_440)
    note: str = Field(default="", max_length=4_000)
    athena_write_id: str = Field(default="", max_length=256)
    eligibility_status: str = Field(default="", max_length=128)


class DecisionModel(StrictRequest):
    kind: str = Field(min_length=1, max_length=32)
    engine_code: Optional[str] = Field(default=None, max_length=32)
    action: str = Field(min_length=1, max_length=32)
    final_code: Optional[str] = Field(default=None, max_length=32)
    reason_code: Optional[str] = Field(default=None, max_length=128)
    note: str = Field(default="", max_length=4_000)


class FeedbackModel(StrictRequest):
    encounter_id: str = Field(min_length=1, max_length=128)
    engine_version: str = Field(default=_ENGINE_VERSION, max_length=256)
    submitted_em: Optional[str] = Field(default=None, max_length=32)
    engine_em: Optional[str] = Field(default=None, max_length=32)
    final_em: Optional[str] = Field(default=None, max_length=32)
    decisions: list[DecisionModel] = Field(default_factory=list, max_length=500)
    time_spent_seconds: Optional[float] = Field(default=None, ge=0, le=86_400)
    overall_note: str = Field(default="", max_length=4_000)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def _cpt_status(cpt, settings: Settings | None = None) -> str:
    """One human-readable line the UI can show for the CPT pill tooltip."""
    if cpt.loaded:
        return f"{cpt.size} official descriptors loaded from {cpt.source}"
    if cpt.fetch_error:
        return f"auto-fetch failed: {cpt.fetch_error}"
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
        "icd10Codes": orch.icd10.size,
        "currentFyLoaded": orch.icd10.has_current_fy,
        "ncciLoaded": bool(orch.ncci and orch.ncci.loaded),
        "coverageLoaded": bool(orch.coverage and orch.coverage.loaded),
        "cptDescriptors": orch.cpt.size if orch.cpt.loaded else 0,
        "cptStatus": _cpt_status(orch.cpt, settings),
        "denialPriors": orch.denial_priors.size,
        "encounters": len(get_encounters()),
    }


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
    ):
        return JSONResponse(status_code=503, content={"status": "warming"})
    try:
        chase_context["store"].healthcheck()
        request.app.state.access_audit_store.healthcheck()
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
    _request_orchestrator(request)
    return [{
        "id": b.encounter_id,
        "encounterType": b.encounter_type,
        "submittedEm": b.submitted_em_code,
        "preview": (b.signed_note or b.transcript)[:110],
    } for b in get_encounters().values()]


def _get_bundle(encounter_id: str) -> VisitBundle:
    bundle = get_encounters().get(encounter_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"unknown encounter '{encounter_id}'")
    return bundle


@router.get("/api/encounters/{encounter_id}")
def get_encounter(encounter_id: str) -> dict[str, Any]:
    b = _get_bundle(encounter_id)
    return {"id": b.encounter_id, "encounterType": b.encounter_type,
            "submittedEm": b.submitted_em_code, "intake": b.intake_text,
            "transcript": b.transcript, "signedNote": b.signed_note,
            "placeOfService": b.place_of_service}


@router.post("/api/encounters/{encounter_id}/billing")
def bill_stored(request: Request, encounter_id: str) -> dict[str, Any]:
    bundle = _get_bundle(encounter_id)
    principal = _request_principal(request)
    result = _request_orchestrator(request).process(
        bundle, user_id=principal.user_id
    )
    return _billing_json(bundle, result)


@router.post("/api/billing")
def bill_adhoc(request: Request, req: ProcessRequest) -> dict[str, Any]:
    principal = _request_principal(request)
    request.state.audit_resource_id = req.encounterId
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


@router.get("/api/encounters/{encounter_id}/coder-view")
def coder_view(request: Request, encounter_id: str) -> dict[str, Any]:
    bundle = _get_bundle(encounter_id)
    orch = _request_orchestrator(request)
    uid = _request_principal(request).user_id
    result = orch.process(bundle, user_id=uid)
    view = _coder_view_json(bundle, result, uid)
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


@router.get("/api/encounters/{encounter_id}/billing-tab")
def billing_tab(request: Request, encounter_id: str,
                generated_at: str = "") -> dict[str, Any]:
    """The provider billing tab (F1): the same visit reorganised as a billing
    reference. The clinical note is never altered — this is a view over it."""
    bundle = _get_bundle(encounter_id)
    result = _request_orchestrator(request).process(
        bundle, user_id=_request_principal(request).user_id
    )
    return build_billing_tab_view(
        bundle, result, engine_version=_ENGINE_VERSION, generated_at=generated_at)


@router.post("/api/completeness")
def completeness(req: CompletenessRequest) -> dict[str, Any]:
    """Run a note-completeness rule pack before the claim reaches a coder.

    This is the pre-submission check: fixing a gap here means it never becomes a
    denial, and never costs a coder a review minute.
    """
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


@router.post("/api/feedback")
def submit_feedback(request: Request, fb: FeedbackModel) -> dict[str, Any]:
    principal = _require_roles(request, "Coder", "Admin")
    request.state.audit_resource_id = fb.encounter_id
    store = get_feedback_store(_request_settings(request))
    record = EncounterFeedback(
        encounter_id=fb.encounter_id, coder_id=principal.user_id,
        engine_version=fb.engine_version, submitted_em=fb.submitted_em,
        engine_em=fb.engine_em, final_em=fb.final_em,
        decisions=[coder_feedback.CodeDecision(**d.model_dump()) for d in fb.decisions],
        time_spent_seconds=fb.time_spent_seconds, overall_note=fb.overall_note,
    )
    store.append(record)
    return {"ok": True, "stored": len(store.load())}


@router.get("/api/benchmarks/em")
def em_benchmarks(specialty: Optional[str] = None, state: str = "NH") -> dict[str, Any]:
    """State + national E/M mix and allowed amounts — population benchmarks for
    opportunity-sizing only, never an input to coding an encounter."""
    from .benchmarks.em_benchmark import EMBenchmark, StateEMBenchmark
    nh = StateEMBenchmark()
    if state.upper() != nh.state:
        raise HTTPException(status_code=404, detail=f"no bundled benchmark for state '{state}'")
    nat = EMBenchmark()
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


@router.get("/api/benchmarks/payer-denials")
def payer_denials(state: str = "NH") -> dict[str, Any]:
    """Issuer-level Marketplace denial rates (CMS TC-PUF PY2026) — the
    'how tough is this payer' prior."""
    from .knowledge.denial_stats import PayerDenialRates
    rates = PayerDenialRates()
    rows = rates.for_state(state)
    return {"state": state.upper(), "source": "CMS Marketplace TC-PUF PY2026 (2024 claims)",
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
    return {
        # This object is safe to cache: PostgresChaseStore carries only immutable
        # connection configuration. Chase rows themselves live in PostgreSQL.
        "store": chase_store_from_settings(settings),
        "policy": policy,
        # The default transport records without contacting anyone; real messaging
        # is gated on the deployment layer and the BAA.
        "runner": SequenceRunner(policy=policy),
        "roster": DirectContractList(settings=settings),
    }


def _request_chase_context(request: Request) -> dict[str, Any]:
    context = getattr(request.app.state, "chase_context", None)
    if context is None:
        context = _build_chase_context(_request_settings(request))
        request.app.state.chase_context = context
    return context


@router.get("/api/chase/worklist")
def chase_worklist(request: Request, bucket: str = "", assignee: str = "",
                   needs_person: bool = False) -> dict[str, Any]:
    """The master list: one integrated view over missing, expired and eligibility."""
    _require_roles(request, "BillingStaff", "Admin")
    ctx = _request_chase_context(request)
    return {
        **ctx["store"].to_dict(bucket=bucket, assignee=assignee,
                               needs_person=needs_person),
        "policy": ctx["policy"].to_dict(),
    }


@router.post("/api/chase/detect")
def chase_detect(request: Request, req: ChaseDetectRequest) -> dict[str, Any]:
    """Run detection over posted coverage snapshots and merge into the worklist.

    Re-running is safe: an open row for the same patient and trigger is refreshed
    rather than duplicated, so daily detection does not inflate the backlog.
    """
    from .chase import CoverageSnapshot, detect_all

    _require_roles(request, "BillingStaff", "Admin")
    ctx = _request_chase_context(request)
    snapshots = [CoverageSnapshot(**s) for s in req.snapshots]
    found = detect_all(snapshots, today=req.today, roster=ctx["roster"])
    ctx["store"].extend(found)
    return {"checked": len(snapshots), "detected": len(found),
            **ctx["store"].to_dict()}


@router.get("/api/chase/metrics")
def chase_metrics(request: Request,
                  baseline_minutes_per_card: Optional[float] = None,
                  baseline_claims_held_monthly: Optional[int] = None) -> dict[str, Any]:
    from .chase.metrics import baseline_comparison

    _require_roles(request, "BillingStaff", "Admin")
    return baseline_comparison(
        _request_chase_context(request)["store"].all_items(),
        baseline_minutes_per_card=baseline_minutes_per_card,
        baseline_claims_held_monthly=baseline_claims_held_monthly)


@router.post("/api/chase/{chase_id}/action")
def chase_action(request: Request, chase_id: str,
                 req: ChaseActionRequest) -> dict[str, Any]:
    """Staff actions on one row.

    `release` and `suppress` are the human decisions; `log_attempt` is how a
    phone call enters the contact history; `submitted` and `verify` are the
    completion loop, and only `verify` closes a row.
    """
    from .chase import Channel

    principal = _require_roles(request, "BillingStaff", "Admin")
    ctx = _request_chase_context(request)
    store, runner, action = ctx["store"], ctx["runner"], req.action
    actor = principal.user_id

    def transition(item):
        if action == "release":
            runner.release(item, by=actor, reason=req.reason)
        elif action == "suppress":
            runner.suppress(item, by=actor, reason=req.reason)
        elif action == "assign":
            item.assignee = req.assignee or actor
        elif action == "log_attempt":
            runner.log_staff_attempt(
                item, channel=Channel(req.channel or "phone"),
                template_id=req.template_id or "phone.call", actor=actor,
                outcome=req.outcome or "reached",
                minutes_spent=req.minutes_spent, note=req.note)
        elif action == "submitted":
            runner.mark_submitted(item, note=req.note)
        elif action == "verify":
            runner.close_verified(item, verified_by=actor,
                                  athena_write_id=req.athena_write_id,
                                  eligibility_status=req.eligibility_status)
        else:
            raise HTTPException(status_code=400, detail=f"unknown action '{action}'")

    try:
        item = store.mutate(chase_id, transition)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if item is None:
        raise HTTPException(status_code=404, detail=f"unknown chase '{chase_id}'")
    return item.to_dict()


@router.get("/api/feedback/summary")
def feedback_summary(request: Request) -> dict[str, Any]:
    _require_roles(request, "Admin")
    feedbacks = get_feedback_store(_request_settings(request)).load()
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
        # Build the durable worklist boundary at startup as well. In production
        # this is where a missing Postgres configuration fails closed.
        application.state.chase_context = _build_chase_context(settings)
        # Force one database round trip so readiness also proves the durable
        # chase schema exists; a configured-but-unreachable store is not ready.
        application.state.chase_context["store"].healthcheck()
        application.state.access_audit_store.healthcheck()
        orchestrator = get_orchestrator(settings)
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
        application.state.orchestrator = None
        application.state.chase_context = None


def create_app(
    *,
    settings: Settings | None = None,
    frontend_dist: str | os.PathLike[str] | None = None,
    access_audit_store: AccessAuditStore | None = None,
    rate_limiter: FixedWindowLimiter | None = None,
    auth_failure_limiter: FixedWindowLimiter | None = None,
) -> FastAPI:
    """Compose one API instance and its same-origin static frontend."""
    config = settings or get_settings()
    audit_store = access_audit_store or (
        FileAccessAuditStore(config.access_audit_path)
        if config.environment in {"staging", "production"}
        else MemoryAccessAuditStore()
    )
    principal_limiter = rate_limiter or FixedWindowLimiter()
    failed_auth_limiter = auth_failure_limiter or FixedWindowLimiter()
    denied_auditor = DeniedAccessAuditor(settings=config, store=audit_store)
    application = FastAPI(
        title="CosmaNeura Billing Engine",
        version="0.1.0",
        lifespan=_lifespan,
    )
    application.state.settings = config
    application.state.ready = False
    application.state.startup_error = None
    application.state.orchestrator = None
    application.state.chase_context = None
    application.state.access_audit_store = audit_store
    application.state.rate_limiter = principal_limiter
    application.state.auth_failure_limiter = failed_auth_limiter

    # Starlette's last-added middleware is outermost. Request order is:
    # exact CORS -> origin -> auth -> audit -> principal rate -> body cap -> API.
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
        expose_headers=["Retry-After"],
        allow_credentials=False,
    )
    application.include_router(router)

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
