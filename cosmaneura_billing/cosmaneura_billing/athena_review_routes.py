"""Authenticated staff-only Athena reads and explicit local application."""
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .athena_client import AthenaStaffReadClient, MockAthenaStaffReadClient
from .athena_review import AthenaReviewError, AthenaReviewService, ReviewActor
from .athena_review_store import MemoryAthenaReviewStore, PostgresAthenaReviewStore
from .persistence import PostgresContext

router = APIRouter()


class CaseRead(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chaseId: str = Field(min_length=1, max_length=128)


class SourceRead(CaseRead):
    sourcePatientId: str | None = Field(default=None, pattern=r"^601(?:7[89]|8[0-4])$")
    expectedBindingSha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expectedAdmissionSha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    operationId: str = Field(min_length=36, max_length=36)
    expectedRevision: int = Field(ge=0, le=9999)


class SourceApply(CaseRead):
    reviewId: str = Field(min_length=36, max_length=36)
    operationId: str = Field(min_length=36, max_length=36)
    expectedRevision: int = Field(ge=1, le=9999)
    confirmedPatientMatch: bool
    selectedPhoneField: Literal["mobile_phone", "home_phone"] | None
    selectedPhoneCountry: Literal["US"] | None = None
    selectedEmail: bool
    selectedPolicyIds: list[str] = Field(max_length=15)
    confirmedPolicyReview: bool
    reviewReason: str = Field(min_length=1, max_length=600)


class BillingRead(CaseRead):
    discoveryToken: str | None = Field(default=None, min_length=1, max_length=12288)
    departmentId: str = Field(pattern=r"^[1-9][0-9]{0,17}$")
    sourceEncounterId: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,17}$")


class BillingApply(BillingRead):
    sourceEncounterId: str = Field(pattern=r"^[1-9][0-9]{0,17}$")
    operationId: str = Field(min_length=36, max_length=36)
    expectedRevision: int = Field(ge=1, le=9999)
    expectedNoteSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expectedMetadataSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmedPatientMatch: bool
    confirmedFinishedNote: bool
    confirmedClinicalContext: bool
    patientAge: int = Field(ge=0, le=130)
    patientSex: Literal["M", "F", "other", "unknown"]
    encounterType: Literal["office_new", "office_established", "telehealth_new", "telehealth_established"]
    placeOfService: Literal["11", "02", "10"]
    providerSpecialty: str = Field(min_length=1, max_length=100)
    reviewReason: str = Field(min_length=1, max_length=600)


def make_athena_service(*, mode, practice_id, chase_store, encounter_store=None, database_context=None,
                        client_id="", client_secret="", authorize=None, signing_key=None,
                        credential_environment="", configured_client_factory=None,deployment_id="",storage_id=""):
    if mode == "disabled":
        return None
    if mode not in ("mock", "preview", "configured") or (mode in ("preview","configured") and database_context is None):
        raise ValueError("Athena review configuration is invalid")
    if database_context is not None:
        if database_context.practice_id != practice_id:
            raise ValueError("Athena practice configuration is invalid")
        from .athena_bindings import PostgresAthenaBindingStore
        store = PostgresAthenaReviewStore(database_context, bindings=PostgresAthenaBindingStore(database_context) if mode=="configured" else None,
                                         deployment_id=deployment_id,storage_id=storage_id)
    else:
        if not callable(authorize):
            raise ValueError("Athena local authorization hook is required")
        store = MemoryAthenaReviewStore(chase_store, practice_id=practice_id,
                                        authorize=authorize, encounter_store=encounter_store)
    if mode == "configured":
        from .athena_configured_review import ConfiguredAthenaReviewService
        if credential_environment not in {"preview","production"}:
            raise ValueError("Athena credential environment is required")
        return ConfiguredAthenaReviewService(store,credential_environment=credential_environment,
            client_id=client_id,client_secret=client_secret,discovery_key=signing_key,client_factory=configured_client_factory)
    client = (MockAthenaStaffReadClient() if mode == "mock" else
              AthenaStaffReadClient(client_id=client_id, client_secret=client_secret))
    return AthenaReviewService(store, client, discovery_key=signing_key)


def configure_athena(application, config, chase_context, encounter_store=None):
    application.state.athena_review_service = make_athena_service(
        mode=config.athena_review_mode, practice_id=config.practice_id or "development",
        chase_store=chase_context["store"], encounter_store=encounter_store,
        database_context=(PostgresContext(config.database_url, practice_id=config.practice_id)
                          if config.athena_review_mode != "disabled" and config.state_store == "postgres" else None),
        client_id=config.athena_client_id, client_secret=config.athena_client_secret,
        credential_environment=config.athena_credential_environment,
        deployment_id=config.athena_deployment_id,storage_id=config.athena_storage_id,
        authorize=lambda actor: actor.role in config.allowed_roles,
        signing_key=getattr(application.state, "review_context_signing_key", None))


def _context(request, chase_id):
    from . import api
    principal = api._require_chase_staff(request)
    service = getattr(request.app.state, "athena_review_service", None)
    if service is None:
        raise HTTPException(404, "athena_unavailable")
    config = api._request_settings(request)
    if principal.practice_id != service.practice_id:
        raise HTTPException(403, "not_authorized")
    tenant, user = principal.tenant_id, principal.user_id
    if config.auth_mode == "disabled" and not service.durable:
        tenant, user = (str(uuid5(NAMESPACE_URL, "athena-local:" + value)) for value in (tenant, user))
    actor = ReviewActor(tenant, user, "Admin" if principal.has_any_role(("Admin",)) else "BillingStaff")
    with service.store.unit(chase_id, actor) as unit:
        api._set_audit_resource(request, unit.resource)
    return service, actor


def _run(request, req, kind):
    from . import api
    try:
        service, actor = _context(request, req.chaseId)
        if kind == "current":
            value = service.current(req.chaseId, actor)
            return value if getattr(service,"mode",None)=="configured" else {"view":value}
        if kind == "billing":
            return service.billing_read(req.chaseId, actor, department_id=req.departmentId,
                                        encounter_id=req.sourceEncounterId, discovery_token=req.discoveryToken)
        command = req.model_dump(exclude={"chaseId"}, exclude_unset=(kind=="refresh"))
        result = getattr(service, kind)(req.chaseId, actor, command,
                                        audit=api._completion_event(request) if service.durable else None)
        if service.durable:
            request.state.audit_completion_committed = True
        return result if getattr(service,"mode",None)=="configured" else {"view": result}
    except AthenaReviewError as exc:
        raise HTTPException(exc.status_code, exc.code) from None


@router.post("/api/athena/card-chase")
def current(request: Request, req: CaseRead):
    return _run(request, req, "current")


@router.post("/api/athena/card-chase/read")
def refresh(request: Request, req: SourceRead):
    return _run(request, req, "refresh")


@router.post("/api/athena/card-chase/apply")
def apply(request: Request, req: SourceApply):
    return _run(request, req, "apply")


@router.post("/api/athena/billing/read")
def billing(request: Request, req: BillingRead):
    return _run(request, req, "billing")


@router.post("/api/athena/billing/apply")
def billing_apply(request: Request, req: BillingApply):
    return _run(request, req, "billing_apply")
