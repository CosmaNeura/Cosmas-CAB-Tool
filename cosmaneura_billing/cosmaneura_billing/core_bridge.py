"""Default-off Core workload receiver using the existing source/header domain."""
from __future__ import annotations

from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import os
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request as WireRequest, build_opener, ProxyHandler, HTTPRedirectHandler
from uuid import UUID
from starlette.requests import Request

from .auth import CoreWorkloadPolicy, CoreWorkloadAuthenticator, CorePrincipal, PrincipalError
from .billing_header import digest as source_digest, source_snapshot, CoreHeaderActor
from .billing_header_store import PostgresBillingHeaderStore
from .provider_handoff import HandoffRequest, prepare_handoff
from .provider_handoff_store import PostgresHandoffStore, _PostgresUnit
from .provider_admission import admission_for
from .core_bridge_contract import (CoreOperation, CoreBridgeError, require, canonical, digest,
    authorization_request, verify_authorization)


@dataclass(frozen=True, repr=False)
class CoreBridgePolicy:
    inbound: CoreWorkloadPolicy
    core_origin: str
    core_token_scope: str
    managed_identity_client_id: str

    def __post_init__(self):
        u = urlsplit(self.core_origin)
        require(type(self.inbound) is CoreWorkloadPolicy and u.scheme == "https" and u.hostname
                and u.path in ("", "/") and not u.query and not u.fragment and not u.username
                and not u.password and u.port in (None, 443) and len(self.core_origin) <= 256)
        require(str(UUID(self.managed_identity_client_id)) == self.managed_identity_client_id
                and self.core_token_scope.endswith("/.default") and len(self.core_token_scope) <= 256
                and not any(c.isspace() for c in self.core_token_scope))

    @classmethod
    def parse(cls, text):
        if text == "":
            return None
        try:
            p = json.loads(text)
            require(type(p) is dict and set(p) == {"schemaVersion", "inbound", "coreOrigin", "coreTokenScope", "managedIdentityClientId"}
                    and p["schemaVersion"] == "cab-core-bridge-policy.v1")
            return cls(CoreWorkloadPolicy(**p["inbound"]), p["coreOrigin"], p["coreTokenScope"], p["managedIdentityClientId"])
        except Exception:
            raise ValueError("invalid Core bridge policy") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise CoreBridgeError()


class CoreOperationAuthorizer:
    """One fixed-origin POST per check; no retry or response-body logging."""
    def __init__(self, policy):
        from azure.identity import ManagedIdentityCredential
        self.policy = policy
        self.credential = ManagedIdentityCredential(client_id=policy.managed_identity_client_id,
                                                    retry_total=0, connection_timeout=3, read_timeout=3)
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def __call__(self, operation, purpose, request_sha):
        try:
            token = self.credential.get_token(self.policy.core_token_scope).token
            req = WireRequest(self.policy.core_origin.rstrip("/") +
                f"/internal/cab/v1/operations/{operation['operationId']}/authorize",
                data=canonical(authorization_request(operation, purpose, request_sha)).encode(), method="POST",
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
            with self.opener.open(req, timeout=3) as response:
                require(response.status == 200)
                raw = response.read(65537)
                require(len(raw) <= 65536)
            return json.loads(raw)
        except HTTPError as exc:
            exc.close()
            raise CoreBridgeError("core_authority_unavailable", 403 if exc.code in (403,404) else 503) from None
        except Exception:
            raise CoreBridgeError("core_authority_unavailable", 503) from None


_ACTIVE = ContextVar("cab_core_workload", default=None)


class CoreLease:
    def __init__(self, *, operation, purpose, request_sha, principal, workload, authorizer):
        self.operation, self.purpose, self.request_sha = operation, purpose, request_sha
        self.principal, self.workload, self.authorizer = principal, workload, authorizer
        self.authorization = None
        self.active = False

    def recheck(self):
        require(self.active and datetime.now(timezone.utc).timestamp() < self.workload.expires_at)
        self.authorization = verify_authorization(self.authorizer(self.operation, self.purpose, self.request_sha),
                                                  self.operation, self.purpose, self.request_sha)
        return self.authorization

    def check_source(self, stored):
        require(stored is not None and stored.source == "provider_completed_note"
                and source_snapshot(stored)["noteSha256"] == self.operation["source"]["textSha256"],
                "source_changed", 409)

    @contextmanager
    def use(self):
        require(_ACTIVE.get() is None)
        self.active = True
        token = _ACTIVE.set(self)
        try:
            self.recheck()
            yield self
        finally:
            self.active = False
            _ACTIVE.reset(token)


def assert_core_model_authority(source, actor, stored):
    lease = _ACTIVE.get()
    require(lease is not None and type(actor) is CoreHeaderActor
            and source["coreOperationId"] == lease.operation["operationId"]
            and actor == lease.principal.header_actor())
    lease.check_source(stored)
    lease.recheck()


def require_core_generation():
    lease = _ACTIVE.get()
    require(lease is not None and lease.active and lease.purpose == "header.generate")


class _CoreLockedSource:
    def _locked_encounter(self, conn, encounter_id):
        lease = _ACTIVE.get()
        require(lease is not None and lease.active)
        return conn.execute("SELECT public.cosmaneura_core_lock_encounter(%s::uuid,%s) AS source",
            (lease.principal.projection_id, encounter_id)).fetchone()["source"]


class CoreHeaderStore(_CoreLockedSource, PostgresBillingHeaderStore):
    @contextmanager
    def _unit(self, *args, **kwargs):
        lease = _ACTIVE.get()
        require(lease is not None)
        lease.recheck()
        with super()._unit(*args, **kwargs) as unit:
            lease.check_source(unit.stored)
            yield unit
            # Source is locked in this same unit; remote authority is a fresh
            # point-in-time decision, never advertised as a cross-DB lock.
            lease.recheck()


class CoreHandoffStore(_CoreLockedSource, PostgresHandoffStore):
    def accept(self, prepared, actor, **kwargs):
        lease = _ACTIVE.get()
        require(lease is not None and prepared.expected_source is None)
        # Core cannot renew an old source operation merely to fetch a prior CAB
        # pin. Resolve only this server-derived record; the unchanged writer's
        # lock/CAS rejects any race after this read. Replay uses its original pin.
        with self.context.connection() as conn:
            prior = conn.execute("SELECT public.cosmaneura_core_operation(%s::uuid,%s::uuid) AS operation",
                (actor.projection_id, prepared.operation_id)).fetchone()["operation"]
            pin = None
            if prior:
                require(prior["operationSha256"] == digest(lease.operation), "handoff_conflict", 409)
                row = conn.execute("SELECT document FROM provider_source_admissions WHERE practice_id=%s AND operation_id=%s::uuid",
                    (self.practice_id, prepared.operation_id)).fetchone()
                require(row and row["document"]["recordKey"] == prepared.record_key, "handoff_conflict", 409)
                pin = row["document"]["expectedSource"]
            else:
                head = conn.execute("""SELECT h.header_source,h.source_revision,b.connection_id FROM provider_handoff_heads h
                    JOIN provider_source_bindings b ON b.practice_id=h.practice_id AND b.record_key=h.record_key
                    WHERE h.practice_id=%s AND h.record_key=%s""", (self.practice_id, prepared.record_key)).fetchone()
                if head:
                    require(str(head["connection_id"]) == lease.connection_id
                        and prepared.source_revision > head["source_revision"], "source_changed", 409)
                    pin = head["header_source"]
        resolved = prepare_handoff(HandoffRequest.model_validate({**prepared.intent, "expectedSource": pin}), self.practice_id)
        return super().accept(resolved, actor, **kwargs)

    @contextmanager
    def unit(self, prepared, actor):
        lease = _ACTIVE.get()
        require(lease is not None and actor == lease.principal.header_actor())
        with super().unit(prepared, actor) as unit:
            yield _CoreUnit(self, unit.conn, prepared)
            lease.recheck()


class _CoreUnit(_PostgresUnit):
    def admit(self, prepared, actor):
        lease = _ACTIVE.get()
        authority = lease.recheck()
        row = self.conn.execute("SELECT document, clock_timestamp() AS now FROM provider_source_connections WHERE practice_id=%s AND connection_id=%s::uuid",
            (self.store.practice_id, lease.connection_id)).fetchone()
        require(row is not None and row["document"]["dataClass"] == lease.operation["admission"]["dataClass"])
        grant = admission_for(prepared, actor, row["document"], not_before=row["now"].isoformat(), expires_at=authority["expiresAt"])
        metadata = {k: v for k, v in lease.operation.items() if k != "noteText"}
        command = {"projectionId": actor.projection_id, "operationSha256": digest(lease.operation),
                   "metadata": metadata, "authorization": authority, "grant": grant}
        self.conn.execute("SELECT public.cosmaneura_prepare_core_handoff(%s)", (self.store.context.Jsonb(command),))
        return super().admit(prepared, actor)


def _resolve(context, workload, practice, actor):
    with context.connection() as conn:
        row = conn.execute("SELECT public.cosmaneura_resolve_core_actor(%s::uuid,%s::uuid,%s,%s,%s,%s,%s) AS actor",
            (workload.tenant_id, workload.caller_id, workload.audience, workload.role, practice, actor["issuer"], actor["subject"])).fetchone()
    require(row and type(row["actor"]) is dict)
    p = row["actor"]
    principal = CorePrincipal(p["userId"], p["tenantId"], p["practiceId"], frozenset({"Provider"}),
        p["issuer"], p["subject"], p["projectionId"], p["mappingVersion"])
    principal.header_actor()  # Validate the normalized immutable projection.
    return principal, p["connectionId"]


def _load_operation(context, encounters, principal, operation_id):
    require(str(UUID(operation_id)) == operation_id)
    with context.connection() as conn:
        row = conn.execute("SELECT public.cosmaneura_core_operation(%s::uuid,%s::uuid) AS operation",
                           (principal.projection_id, operation_id)).fetchone()
    require(row and type(row["operation"]) is dict, "core_operation_not_found", 404)
    data = row["operation"]
    stored = encounters.get(data["receipt"]["encounterId"])
    require(stored is not None and source_digest(source_snapshot(stored)) == source_digest(data["receipt"]["headerSource"]),
            "source_changed", 409)
    operation = CoreOperation.model_validate({**data["metadata"], "noteText": stored.bundle.signed_note}).document()
    require(digest(operation) == data["operationSha256"])
    return operation, data["receipt"]


_PATHS = {"/api/core/v1/completed-notes": "completed-note.accept",
          **{f"/api/core/v1/header/{kind}": f"header.{kind}" for kind in ("read", "generate", "save")}}


def _unique(pairs):
    result = {}
    for k, v in pairs:
        require(k not in result, "invalid_core_request", 422)
        result[k] = v
    return result


def create_core_bridge(*, settings=None, policy=None, verifier=None, authorizer=None, orchestrator_factory=None):
    """Unconfigured composition has no intake/header routes and no dependencies."""
    from fastapi import FastAPI, Request, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import ValidationError
    from starlette.concurrency import run_in_threadpool
    from . import api
    from .access_audit import build_access_event, classify_access
    from .billing_header_routes import HeaderRead, HeaderGenerate, HeaderSave, read_header, generate_header, save_header
    from .billing_header import BillingHeaderError
    from .persistence import PostgresContext, postgres_services
    from .model_admission import parse_policy
    from .security import RequestBodyLimitMiddleware, SecurityHeadersMiddleware
    from uuid import uuid4

    if policy is None:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        @app.get("/api/live")
        def disabled_live():
            return {"status": "alive", "bridge": "disabled"}
        return app
    require(type(policy) is CoreBridgePolicy and settings is not None
        and settings.state_store == "postgres" and settings.auth_mode == "container_apps"
        and settings.billing_header_enabled and settings.provider_handoff_enabled)
    require(settings.environment in {"test", "development"} or
            verifier is None and authorizer is None and orchestrator_factory is None)
    verifier = verifier or CoreWorkloadAuthenticator(policy.inbound)
    authorizer = authorizer or CoreOperationAuthorizer(policy)
    services = postgres_services(settings)
    context = services.encounters.context
    headers, handoffs = CoreHeaderStore(context), CoreHandoffStore(context)

    def healthcheck():
        services.encounters.healthcheck(); headers.healthcheck(); handoffs.healthcheck()
        with context.connection() as conn:
            checks = conn.execute("""SELECT
             has_function_privilege(current_user,'cosmaneura_resolve_core_actor(uuid,uuid,text,text,text,text,text)','EXECUTE') AS resolve,
             has_function_privilege(current_user,'cosmaneura_core_operation(uuid,uuid)','EXECUTE') AS read,
             has_function_privilege(current_user,'cosmaneura_prepare_core_handoff(jsonb)','EXECUTE') AS intake,
             has_function_privilege(current_user,'cosmaneura_core_lock_encounter(uuid,text)','EXECUTE') AS source""").fetchone()
            require(checks and all(checks.values()), "core_bridge_unavailable", 503)

    @asynccontextmanager
    async def lifespan(app):
        healthcheck()
        factory = orchestrator_factory or api.get_orchestrator
        app.state.orchestrator = factory(settings, feedback_store=services.feedback)
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            shutdown = getattr(app.state.orchestrator, "shutdown", None)
            if callable(shutdown): shutdown()
            app.state.orchestrator = None

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.encounter_store = settings, services.encounters
    app.state.feedback_store, app.state.billing_header_store = services.feedback, headers
    app.state.model_data_policy, app.state.ready = parse_policy(settings.model_data_policy_json), False
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=600000)
    app.add_middleware(SecurityHeadersMiddleware, deployed_https=settings.environment in {"staging", "production"})

    @app.get("/api/live")
    def live(): return {"status": "alive"}

    @app.get("/api/ready")
    def ready():
        try:
            require(app.state.ready); healthcheck()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    def execute(request, raw):
        require(app.state.ready, "core_bridge_unavailable", 503)
        require(not request.url.query and "origin" not in request.headers, "invalid_core_request", 422)
        require(not any(k.lower().startswith(b"x-ms-client-principal") for k, _ in request.scope["headers"]),
                "core_workload_authentication_failed", 401)
        workload = verifier.authenticate(request.scope["headers"])
        body = json.loads(raw, object_pairs_hook=_unique)
        purpose = _PATHS[request.url.path]
        require(type(body) is dict and body.get("schemaVersion") == "core-cab-workload.v1"
                and body.get("purpose") == purpose, "invalid_core_request", 422)
        receipt = None
        if purpose == "completed-note.accept":
            require(set(body) == {"schemaVersion", "purpose", "operation", "expectedSource"}
                    and body["expectedSource"] is None, "invalid_core_request", 422)
            operation = CoreOperation.model_validate(body["operation"]).document()
            require(canonical(operation) == canonical(body["operation"]), "invalid_core_request", 422)
            principal, connection_id = _resolve(context, workload, operation["practiceId"], operation["actor"])
        else:
            require(set(body) == {"schemaVersion", "purpose", "sourceOperationId", "practiceId", "actor", "command"}
                and type(body["actor"]) is dict and set(body["actor"]) == {"issuer", "subject"}, "invalid_core_request", 422)
            principal, connection_id = _resolve(context, workload, body["practiceId"], body["actor"])
            operation, receipt = _load_operation(context, services.encounters, principal, body["sourceOperationId"])
        lease = CoreLease(operation=operation, purpose=purpose, request_sha=digest(body),
                          principal=principal, workload=workload, authorizer=authorizer)
        lease.connection_id = connection_id
        request.state.principal = principal
        event_id = str(uuid4())
        request.state.audit_event_id = event_id
        classified = classify_access("POST", request.url.path)
        attempt = build_access_event(settings=settings, scope=request.scope, classified=classified,
                                     event_id=event_id, status=0, outcome="attempted")
        services.audit.append(attempt)
        try:
            with lease.use():
                if purpose == "completed-note.accept":
                    source = operation["source"]
                    source_id = "core:" + digest([connection_id, operation["practiceId"], source["noteId"]])
                    handoff = HandoffRequest.model_validate({"schemaVersion": "cab.completed-note-handoff.v1",
                        "operationId": operation["operationId"], "sourceRecordId": source_id,
                        "sourceRevision": source["revision"], "expectedSource": body["expectedSource"],
                        "completion": operation["completion"], "visit": operation["visit"], "supportingDocuments": [],
                        "note": {"id": source["noteId"], "version": str(source["revision"]),
                                 "sha256": source["textSha256"], "text": operation["noteText"]}})
                    result = handoffs.accept(prepare_handoff(handoff, principal.practice_id), principal.header_actor(),
                                             audit_completion=api._completion_event(request))
                    request.state.audit_completion_committed = True
                    result["launchPath"] = None  # Core owns browser identity and continuation.
                else:
                    kind = purpose.split(".")[1]
                    cls, handler = {"read": (HeaderRead, read_header), "generate": (HeaderGenerate, generate_header),
                                    "save": (HeaderSave, save_header)}[kind]
                    command = cls.model_validate(body["command"])
                    require(command.encounterId == receipt["encounterId"]
                        and source_digest(command.expectedSource) == source_digest(receipt["headerSource"]), "source_changed", 409)
                    result = handler(request, command)
                if not getattr(request.state, "audit_completion_committed", False):
                    services.audit.append(api._completion_event(request))
                    request.state.audit_completion_committed = True
                saved_receipt = result["receipt"] if purpose == "completed-note.accept" else receipt
                current = result.get("current", True)
                source_dto = None
                if current:
                    stored = services.encounters.get(saved_receipt["encounterId"])
                    require(stored is not None and source_digest(source_snapshot(stored)) == source_digest(saved_receipt["headerSource"]),
                            "source_changed", 409)
                    model_policy = {"allowed": False, "reason": "operational_source_model_not_approved"}
                    try:
                        with api._inference_scope(request, stored):
                            model_policy = {"allowed": True, "reason": None}
                    except HTTPException as exc:
                        if exc.status_code != 403: raise
                    source_dto = {"encounterId": saved_receipt["encounterId"], "encounterVersion": stored.version,
                        "headerSource": source_snapshot(stored), "noteText": stored.bundle.signed_note,
                        "intake": "", "transcript": "", "visitContext": operation["visit"],
                        "completion": operation["completion"], "projection": {
                            "version": operation["source"]["projectionVersion"],
                            **{k: operation["source"][k] for k in ("excludedRecordingTime", "contentSha256", "textSha256")}},
                        "inferencePolicy": model_policy}
                return {"schemaVersion": "core-cab-result.v1", "purpose": purpose,
                        "sourceOperationId": operation["operationId"], "requestSha256": digest(body),
                        "result": result, "source": source_dto}
        except Exception as exc:
            if not getattr(request.state, "audit_completion_committed", False):
                status = (exc.status_code if isinstance(exc, (CoreBridgeError, BillingHeaderError, HTTPException))
                          else 422 if isinstance(exc, (ValidationError, ValueError, TypeError, KeyError)) else 503)
                services.audit.append(api._completion_event(request, status=status,
                    outcome="denied" if status < 500 else "error"))
            raise

    async def endpoint(request: Request):
        try:
            return await run_in_threadpool(execute, request, await request.body())
        except PrincipalError:
            return JSONResponse({"detail": "core_workload_authentication_failed"}, status_code=401)
        except (CoreBridgeError, BillingHeaderError) as exc:
            return JSONResponse({"detail": exc.code}, status_code=exc.status_code)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        except (ValidationError, ValueError, TypeError, KeyError):
            return JSONResponse({"detail": "invalid_core_request"}, status_code=422)
        except Exception:
            return JSONResponse({"detail": "core_bridge_unavailable"}, status_code=503)

    for path in _PATHS:
        app.add_api_route(path, endpoint, methods=["POST"])
    return app


def from_environment():
    policy = CoreBridgePolicy.parse(os.environ.get("COSMANEURA_CORE_BRIDGE_POLICY_JSON", ""))
    if policy is None:
        return create_core_bridge()
    from .settings import get_settings
    return create_core_bridge(settings=get_settings(), policy=policy)


app = from_environment()
