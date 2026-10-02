"""Authenticated completed-note receiver; server-owned admission is empty by default."""
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request

from .billing_header import BillingHeaderError, HeaderActor
from .persistence import PersistenceUnavailable, PostgresContext
from .provider_handoff import (COMPLETION_MEANING, FixtureRequest, HandoffError,
    HandoffRequest, fixture_choices, prepare_fixture, prepare_handoff)
from .provider_handoff_store import MemoryHandoffStore, PostgresHandoffStore
from .seed_demo_release_candidate import SyntheticDemoSeedError

router = APIRouter()


def configure_handoff(application, config, encounter_store):
    application.state.provider_handoff_store = None
    if not (config.provider_handoff_enabled and config.billing_header_enabled):
        return
    if config.state_store == "postgres":
        application.state.provider_handoff_store = PostgresHandoffStore(
            PostgresContext(config.database_url, practice_id=config.practice_id))
    else:
        application.state.provider_handoff_store = MemoryHandoffStore(encounter_store,
            authorize=lambda actor: actor.role in config.allowed_roles)


def _context(request):
    from . import api
    config = api._request_settings(request)
    store = getattr(request.app.state, "provider_handoff_store", None)
    if not config.provider_handoff_enabled or not config.billing_header_enabled or store is None:
        raise HTTPException(404, "handoff_disabled")
    principal = api._require_roles(request, "Provider", "Admin")
    if principal.practice_id != store.practice_id:
        raise HTTPException(403, "not_authorized")
    role = "Admin" if principal.has_any_role(("Admin",)) else "Provider"
    tenant, user = principal.tenant_id, principal.user_id
    if config.auth_mode == "disabled" and config.state_store == "local":
        tenant, user = str(uuid5(NAMESPACE_URL, f"cab-local:{tenant}")), str(uuid5(NAMESPACE_URL, f"cab-local:{user}"))
    return store, HeaderActor(tenant, user, f"{role} {user[:8]}", role)


@router.get("/api/provider/encounters/handoff/fixtures")
def fixtures(request: Request):
    _context(request)
    try:
        return {"fixtures": fixture_choices(), "completionMeaning": COMPLETION_MEANING}
    except (SyntheticDemoSeedError, ValueError, OSError):
        raise HTTPException(503, "fixture_manifest_unavailable") from None


@router.post("/api/provider/encounters/handoff/fixture")
def fixture_handoff(request: Request, req: FixtureRequest):
    from . import api
    store, actor = _context(request)
    try:
        prepared = prepare_fixture(req)
        result = store.accept(prepared, actor,
            audit_completion=api._completion_event(request) if store.durable else None)
        if store.durable:
            request.state.audit_completion_committed = True
        stored = api._request_encounter_store(request).get(result["receipt"]["encounterId"])
        api._set_audit_resource(request, stored.resource_id)
        return result
    except (HandoffError, BillingHeaderError) as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    except (PersistenceUnavailable, SyntheticDemoSeedError, ValueError, OSError):
        raise HTTPException(503, "handoff_unavailable") from None


@router.post("/api/provider/encounters/handoff")
def operational_handoff(request: Request, req: HandoffRequest):
    # The store consumes an exact operator-owned admission under the same
    # transaction/lock as source CAS. Empty registries deny every new input.
    from . import api
    store, actor = _context(request)
    try:
        prepared = prepare_handoff(req, store.practice_id)
        result = store.accept(prepared, actor,
            audit_completion=api._completion_event(request) if store.durable else None)
        if store.durable:
            request.state.audit_completion_committed = True
        stored = api._request_encounter_store(request).get(result["receipt"]["encounterId"])
        api._set_audit_resource(request, stored.resource_id)
        return result
    except (HandoffError, BillingHeaderError) as exc:
        raise HTTPException(exc.status_code, exc.code) from None
    except (PersistenceUnavailable, ValueError, OSError):
        raise HTTPException(503, "handoff_unavailable") from None
