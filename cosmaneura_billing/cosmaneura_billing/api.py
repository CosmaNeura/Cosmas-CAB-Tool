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
The orchestrator (and its knowledge bases, including the ~18s NCCI load) is
initialized once, on first use.
"""
from __future__ import annotations

import os
import re
import secrets
from functools import lru_cache
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import coder_feedback
from .coder_feedback import EncounterFeedback, FeedbackStore
from .corpus.acibench import load_acibench
from .corpus.demo import SAMPLE_VISIT
from .orchestrator import BillingOrchestrator
from .schemas import BillingResult, CodeKind, CodeSuggestion, Disposition, VisitBundle

app = FastAPI(title="CosmaNeura Billing Engine", version="0.1.0")
# Explicit origin allowlist, not a wildcard: POST /api/feedback mutates the
# learned preference layer (which can HOLD codes practice-wide), so any web
# page a coder happens to have open must not be able to reach it.
# COSMANEURA_CORS_ORIGINS is a comma-separated list; the default is the local
# Vite dev server only.
_CORS_ORIGINS = [
    o.strip() for o in os.environ.get(
        "COSMANEURA_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["content-type", "x-feedback-token"],
)

_KIND = {CodeKind.EM: "EM", CodeKind.ICD10: "ICD10",
         CodeKind.PROCEDURE: "PROCEDURE", CodeKind.MODIFIER: "MODIFIER"}
_ENGINE_VERSION = "cpt2026u4;icd10-FY2026;ncci-2026Q3-v32.2;mcd-NGS-2026-07-20"


# --------------------------------------------------------------------------- #
# Engine + encounter store (lazy singletons)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def get_orchestrator() -> BillingOrchestrator:
    return BillingOrchestrator()


@lru_cache(maxsize=1)
def get_encounters() -> dict[str, VisitBundle]:
    store: dict[str, VisitBundle] = {SAMPLE_VISIT.encounter_id: SAMPLE_VISIT}
    try:
        for b in load_acibench("train")[:10]:
            store[b.encounter_id] = b
    except FileNotFoundError:
        pass
    return store


def get_feedback_store() -> FeedbackStore:
    return FeedbackStore(os.environ.get("COSMANEURA_FEEDBACK_PATH", "coder_feedback.jsonl"))


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
    }


def _coder_view_json(bundle: VisitBundle, result: BillingResult) -> dict[str, Any]:
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
        "coderId": os.environ.get("COSMANEURA_CODER_ID", "coder_A"),
        "em": row(em) if em else None,
        "dx": [row(s) for s in dx],
        "withheld": withheld,
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class ProcessRequest(BaseModel):
    encounterId: str = "ADHOC-1"
    intake: str = ""
    transcript: str = ""
    signedNote: str = ""
    submittedEm: Optional[str] = None
    encounterType: str = "office_established"
    placeOfService: str = "11"
    patientAge: int = 0
    patientSex: str = "unknown"
    userId: Optional[str] = None      # reviewer identity for learned preferences


class DecisionModel(BaseModel):
    kind: str
    engine_code: Optional[str] = None
    action: str
    final_code: Optional[str] = None
    reason_code: Optional[str] = None
    note: str = ""


class FeedbackModel(BaseModel):
    encounter_id: str
    coder_id: str
    engine_version: str = _ENGINE_VERSION
    submitted_em: Optional[str] = None
    engine_em: Optional[str] = None
    final_em: Optional[str] = None
    decisions: list[DecisionModel] = Field(default_factory=list)
    time_spent_seconds: Optional[float] = None
    overall_note: str = ""


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def _cpt_status(cpt) -> str:
    """One human-readable line the UI can show for the CPT pill tooltip."""
    if cpt.loaded:
        return f"{cpt.size} official descriptors loaded from {cpt.source}"
    if cpt.fetch_error:
        return f"auto-fetch failed: {cpt.fetch_error}"
    if all(os.environ.get(v) for v in
           ("AMA_TOKEN_URL", "AMA_BASE_URL", "AMA_CONSUMER_KEY", "AMA_CONSUMER_SECRET")):
        return "AMA credentials present but no descriptors resolved"
    return ("no CPT license configured — set the four AMA_* variables "
            "(auto-fetch) or CPT_DATA_DIR (local Standard Data File); "
            "generic labels in use")


@app.get("/api/health")
def health() -> dict[str, Any]:
    orch = get_orchestrator()
    return {
        "status": "ok",
        "engineVersion": _ENGINE_VERSION,
        "icd10Codes": orch.icd10.size,
        "currentFyLoaded": orch.icd10.has_current_fy,
        "ncciLoaded": bool(orch.ncci and orch.ncci.loaded),
        "coverageLoaded": bool(orch.coverage and orch.coverage.loaded),
        "cptDescriptors": orch.cpt.size if orch.cpt.loaded else 0,
        "cptStatus": _cpt_status(orch.cpt),
        "denialPriors": orch.denial_priors.size,
        "encounters": len(get_encounters()),
    }


@app.get("/api/encounters")
def list_encounters() -> list[dict[str, Any]]:
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


@app.get("/api/encounters/{encounter_id}")
def get_encounter(encounter_id: str) -> dict[str, Any]:
    b = _get_bundle(encounter_id)
    return {"id": b.encounter_id, "encounterType": b.encounter_type,
            "submittedEm": b.submitted_em_code, "intake": b.intake_text,
            "transcript": b.transcript, "signedNote": b.signed_note,
            "placeOfService": b.place_of_service}


@app.post("/api/encounters/{encounter_id}/billing")
def bill_stored(encounter_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
    bundle = _get_bundle(encounter_id)
    result = get_orchestrator().process(bundle, user_id=user_id)
    return _billing_json(bundle, result)


@app.post("/api/billing")
def bill_adhoc(req: ProcessRequest) -> dict[str, Any]:
    bundle = VisitBundle(
        encounter_id=req.encounterId, patient_age=req.patientAge,
        patient_sex=req.patientSex, encounter_type=req.encounterType,
        place_of_service=req.placeOfService, intake_text=req.intake,
        transcript=req.transcript, signed_note=req.signedNote,
        submitted_em_code=req.submittedEm,
    )
    result = get_orchestrator().process(bundle, user_id=req.userId)
    return _billing_json(bundle, result)


@app.get("/api/encounters/{encounter_id}/coder-view")
def coder_view(encounter_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
    bundle = _get_bundle(encounter_id)
    orch = get_orchestrator()
    uid = user_id or os.environ.get("COSMANEURA_CODER_ID", "coder_A")
    result = orch.process(bundle, user_id=uid)
    view = _coder_view_json(bundle, result)
    view["coderId"] = uid
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


@app.get("/api/encounters/{encounter_id}/billing-tab")
def billing_tab(encounter_id: str, user_id: Optional[str] = None) -> dict[str, Any]:
    """F1 — the provider Billing Tab view: the same visit reorganized into a
    billing reference; the clinical note is read, never altered. Telehealth
    encounters additionally run the F2 completeness rule pack pre-submission."""
    from .billing_tab import build_billing_tab_view
    from .completeness import attestation_block, check_note

    bundle = _get_bundle(encounter_id)
    result = get_orchestrator().process(bundle, user_id=user_id)
    completeness = None
    if "telehealth" in bundle.encounter_type or bundle.place_of_service in ("02", "10"):
        results = check_note(bundle.signed_note)
        completeness = {"rulepack": "telehealth.v1",
                        "results": [r.to_json() for r in results],
                        "attestation_block": attestation_block(results)}
    return build_billing_tab_view(bundle, result, engine_version=_ENGINE_VERSION,
                                  completeness=completeness)


def require_feedback_token(x_feedback_token: Optional[str] = Header(None)) -> None:
    """Guard the only mutating endpoint. Coder feedback trains the preference
    layer, which can hold codes for the whole practice — so writes must be
    attributable, not anonymous. Set COSMANEURA_FEEDBACK_TOKEN in any shared
    or deployed environment; when unset (local dev) the guard stays open and
    says so in /api/health."""
    expected = os.environ.get("COSMANEURA_FEEDBACK_TOKEN")
    if not expected:
        return
    if not x_feedback_token or not secrets.compare_digest(x_feedback_token, expected):
        raise HTTPException(status_code=401, detail="invalid feedback token")


@app.post("/api/feedback", dependencies=[Depends(require_feedback_token)])
def submit_feedback(fb: FeedbackModel) -> dict[str, Any]:
    store = get_feedback_store()
    record = EncounterFeedback(
        encounter_id=fb.encounter_id, coder_id=fb.coder_id,
        engine_version=fb.engine_version, submitted_em=fb.submitted_em,
        engine_em=fb.engine_em, final_em=fb.final_em,
        decisions=[coder_feedback.CodeDecision(**d.model_dump()) for d in fb.decisions],
        time_spent_seconds=fb.time_spent_seconds, overall_note=fb.overall_note,
    )
    store.append(record)
    return {"ok": True, "stored": len(store.load())}


@app.get("/api/benchmarks/em")
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


@app.get("/api/benchmarks/payer-denials")
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


@app.get("/api/feedback/summary")
def feedback_summary() -> dict[str, Any]:
    feedbacks = get_feedback_store().load()
    if not feedbacks:
        return {"encounters": 0, "summary": None, "tuningBacklog": [], "goldRows": 0}
    return {
        "encounters": len(feedbacks),
        "summary": coder_feedback.summarize(feedbacks),
        "tuningBacklog": coder_feedback.tuning_signals(feedbacks),
        "goldRows": len(coder_feedback.export_gold(feedbacks)),
    }


# --------------------------------------------------------------------------- #
# F9 — biller-facing Card Chase worklist (mock athenaNet; synthetic data only).
# Mutating chase routes share the feedback-token guard: open in local dev,
# locked wherever COSMANEURA_FEEDBACK_TOKEN is set.
# --------------------------------------------------------------------------- #
from . import chase_api as _chase_api  # noqa: E402

_chase_api.register(app, dependencies=[Depends(require_feedback_token)])
