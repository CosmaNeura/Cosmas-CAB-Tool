"""Deployment-owned model disclosure policy; no browser or source-read authority.

Policies are immutable for one application revision. Clearing a policy revokes
that revision only after all old processes stop; expiry is checked live. This
first lane authorizes only the full CAB pipeline and Azure OpenAI, never TA4H.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import UUID

ATHENA_SOURCE = "athena_staff_review_operational"
PROVIDER_SOURCE = "provider_completed_note"
MODEL_SCOPED_SOURCES = frozenset((ATHENA_SOURCE, PROVIDER_SOURCE))


def requires_model_scope(settings, source):
    return source == ATHENA_SOURCE or (source == PROVIDER_SOURCE and
        (settings.azure_openai_enabled or settings.azure_language_enabled
         or settings.azure_language_shadow_enabled or bool(settings.model_data_policy_json)))


class ModelAdmissionError(PermissionError):
    code = "operational_source_model_not_approved"
    status_code = 403

    def __init__(self):
        super().__init__(self.code)


def _require(condition):
    if not condition:
        raise ModelAdmissionError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _instant(value):
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:0\d|1\d|2[0-3]):[0-5]\d)", value
    ) or value.endswith("-00:00"):
        raise ValueError()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


@dataclass(frozen=True, repr=False)
class ModelDataPolicy:
    _json: str

    @property
    def document(self):
        return json.loads(self._json)

    @property
    def sha256(self):
        return hashlib.sha256(self._json.encode()).hexdigest()


def parse_policy(text: str) -> ModelDataPolicy | None:
    """Closed bounded configuration; malformed input never echoes its values."""
    if text == "":
        return None
    try:
        if type(text) is not str or len(text) > 8192:
            raise ValueError()
        p = json.loads(text, object_pairs_hook=_unique)
        if type(p) is not dict or p.get("schemaVersion") not in {"cab-model-data-policy.v1", "cab-model-data-policy.v2"}:
            raise ValueError()
        v2 = p["schemaVersion"] == "cab-model-data-policy.v2"
        keys = {"schemaVersion", "policyId", "reviewRef", "serverPracticeId", "tenantId",
                "deploymentId", "storageId", "databaseName", "runtimeRole", "sources" if v2 else "source",
                "notBefore", "expiresAt", "azureOpenAI"}
        if set(p) != keys:
            raise ValueError()
        if not v2 and p["source"] != ATHENA_SOURCE:
            raise ValueError()
        if v2:
            sources = p["sources"]
            if type(sources) is not dict or not sources or not set(sources) <= MODEL_SCOPED_SOURCES:
                raise ValueError()
            if ATHENA_SOURCE in sources and sources[ATHENA_SOURCE] != {}:
                raise ValueError()
            if PROVIDER_SOURCE in sources:
                provider = sources[PROVIDER_SOURCE]
                if type(provider) is not dict or set(provider) != {"connectionId", "connectionVersion", "dataClass"}:
                    raise ValueError()
                if type(provider["connectionId"]) is not str or str(UUID(provider["connectionId"])) != provider["connectionId"]:
                    raise ValueError()
                if type(provider["connectionVersion"]) is not int or not 1 <= provider["connectionVersion"] <= 1_000_000:
                    raise ValueError()
                if provider["dataClass"] not in {"synthetic", "deidentified"}:
                    raise ValueError()
        for key in ("policyId", "tenantId"):
            if type(p[key]) is not str or str(UUID(p[key])) != p[key]:
                raise ValueError()
        for key in ("serverPracticeId", "reviewRef", "databaseName", "runtimeRole"):
            value = p[key]
            if type(value) is not str or not value.strip() or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise ValueError()
        for key in ("deploymentId", "storageId"):
            if type(p[key]) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", p[key]):
                raise ValueError()
        if _instant(p["notBefore"]) >= _instant(p["expiresAt"]):
            raise ValueError()
        a = p["azureOpenAI"]
        if type(a) is not dict or set(a) != {"endpoint", "deployment", "model", "apiVersion", "managedIdentityClientId"}:
            raise ValueError()
        if type(a["endpoint"]) is not str or not re.fullmatch(r"https://[a-z0-9][a-z0-9-]*\.openai\.azure\.com/?", a["endpoint"]):
            raise ValueError()
        for key in ("deployment", "model", "apiVersion"):
            if type(a[key]) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", a[key]):
                raise ValueError()
        if type(a["managedIdentityClientId"]) is not str or str(UUID(a["managedIdentityClientId"])) != a["managedIdentityClientId"]:
            raise ValueError()
        return ModelDataPolicy(_canonical(p))
    except (ValueError, TypeError, RecursionError, OverflowError, AttributeError):
        raise ValueError("invalid model data policy") from None


def _service(settings):
    return {"endpoint": settings.azure_openai_endpoint, "deployment": settings.azure_openai_deployment,
            "model": settings.azure_openai_model, "apiVersion": settings.azure_openai_api_version,
            "managedIdentityClientId": settings.azure_client_id}


def _sources(policy):
    return policy["sources"] if policy["schemaVersion"] == "cab-model-data-policy.v2" else {policy["source"]: {}}


_SEAL = object()
_ACTIVE = ContextVar("cab_operational_model_input", default=None)


class ModelInputScope:
    def __init__(self, seal, policy, settings, stored, actor, context):
        _require(seal is _SEAL)
        from .billing_header import digest, source_snapshot
        self._policy, self._settings, self._stored = policy, settings, stored
        self._actor, self._context = actor, context
        self._source_hash = digest(source_snapshot(stored))
        self._bundle_hash = _digest(asdict(stored.bundle))
        self._active, self._client, self._calls = True, None, 0
        self._provider_source = None

    @property
    def proof(self):
        p = self._policy.document
        proof = {"schemaVersion": "cab-model-input-proof.v1", "policyId": p["policyId"],
                "policySha256": self._policy.sha256, "sourceSha256": self._source_hash,
                "serviceSha256": _digest(p["azureOpenAI"]), "expiresAt": p["expiresAt"]}
        if self._stored.source == PROVIDER_SOURCE:
            _require(self._provider_source is not None)
            proof.update(schemaVersion="cab-model-input-proof.v2",
                sourceOperationId=self._provider_source["sourceOperationId"],
                sourceAdmissionSha256=self._provider_source["sourceAdmissionSha256"])
        return proof

    def _configuration(self):
        p, s = self._policy.document, self._settings
        _require(self._active and _instant(p["notBefore"]) <= _now() < _instant(p["expiresAt"]))
        _require(s.azure_openai_enabled and (not s.azure_openai_synthetic_only or self._stored.source == PROVIDER_SOURCE)
                 and not s.azure_language_enabled and not s.azure_language_shadow_enabled and not s.azure_language_mock)
        _require(self._stored.source in _sources(p))
        _require(s.state_store == "postgres" and s.auth_mode == "container_apps"
                 and s.practice_id == p["serverPracticeId"] == self._context.practice_id
                 and s.entra_tenant_id == p["tenantId"] == self._actor.tenant_id
                 and s.athena_deployment_id == p["deploymentId"] and s.athena_storage_id == p["storageId"]
                 and _service(s) == p["azureOpenAI"])
        configured = parse_policy(s.model_data_policy_json)
        _require(configured is not None and configured.sha256 == self._policy.sha256)

    def assert_persistable(self, connection, stored):
        """Revalidate policy, actor, actual storage and exact source in caller's unit."""
        from .billing_header import digest, source_snapshot
        from .persistence import PostgresEncounterStore
        self._configuration()
        _require(stored is not None and stored.source == self._stored.source and stored.source in MODEL_SCOPED_SOURCES
                 and digest(source_snapshot(stored)) == self._source_hash)
        p, actor = self._policy.document, self._actor
        try:
            row = connection.execute("""SELECT session_user::text AS role, current_user::text AS effective_role,
                current_database() AS database, public.cosmaneura_practice_allowed(%s) AS practice,
                public.cosmaneura_billing_header_editor_allowed(%s::uuid,%s::uuid,%s) AS member,
                (SELECT NOT rolsuper AND NOT rolbypassrls AND NOT rolcreaterole AND NOT rolcreatedb
                 FROM pg_roles WHERE rolname=session_user) AS restricted""",
                (p["serverPracticeId"], actor.tenant_id, actor.user_id, actor.role)).fetchone()
            _require(row and row["role"] == row["effective_role"] == p["runtimeRole"]
                     and row["database"] == p["databaseName"] and row["practice"] is True
                     and row["member"] is True and row["restricted"] is True)
            actual = connection.execute(PostgresEncounterStore._select() +
                " WHERE practice_id=%s AND external_id=%s", (p["serverPracticeId"], stored.bundle.encounter_id)).fetchone()
            _require(actual and digest(source_snapshot(PostgresEncounterStore._bundle(actual))) == self._source_hash)
            if stored.source == PROVIDER_SOURCE:
                row = connection.execute("SELECT public.cosmaneura_provider_model_source(%s::uuid,%s::uuid,%s,%s) AS source",
                    (actor.tenant_id, actor.user_id, actor.role, stored.bundle.encounter_id)).fetchone()
                source = row["source"] if row else None
                _require(type(source) is dict and digest(source.get("headerSource")) == self._source_hash
                    and _digest(source.get("connection")) == _digest(_sources(p)[PROVIDER_SOURCE]))
                _require(not self._settings.azure_openai_synthetic_only or source["connection"]["dataClass"] == "synthetic")
                _require(self._provider_source is None or _digest(self._provider_source) == _digest(source))
                if "coreOperationId" in source:
                    from .core_bridge import assert_core_model_authority
                    assert_core_model_authority(source, actor, stored)
                self._provider_source = source
        except ModelAdmissionError:
            raise
        except Exception:
            raise ModelAdmissionError() from None
        self._configuration()

    def _check(self):
        try:
            with self._context.connection() as connection:
                self.assert_persistable(connection, self._stored)
        except ModelAdmissionError:
            raise
        except Exception:
            raise ModelAdmissionError() from None

    def _request(self, client):
        _require(self._active and self._client is client and client.max_retries == self._retries)
        _require(client._operational_transport_identity() == self._policy.document["azureOpenAI"])
        if self._provider_source is not None and "coreOperationId" in self._provider_source:
            from .core_bridge import require_core_generation
            require_core_generation()
        self._check()
        self._calls += 1
        _require(self._calls <= 2 * (self._retries + 1))


@contextmanager
def operational_scope(policy, *, settings, stored, actor, context):
    """Only actual Postgres source/member authority may mint a request capability."""
    from .billing_header import HeaderActor, CoreHeaderActor
    from .persistence import PostgresContext
    _require(type(policy) is ModelDataPolicy and type(context) is PostgresContext and type(actor) in (HeaderActor, CoreHeaderActor))
    scope = ModelInputScope(_SEAL, policy, settings, stored, actor, context)
    try:
        scope._check()
        yield scope
    finally:
        scope._active = False


@contextmanager
def pipeline_scope(scope, orchestrator, bundle, source):
    from .llm import AzureOpenAILLM
    from .agents.coding import CodingAgent
    _require(type(scope) is ModelInputScope and _ACTIVE.get() is None)
    _require(source in MODEL_SCOPED_SOURCES and source == scope._stored.source and _digest(asdict(bundle)) == scope._bundle_hash)
    _require(type(orchestrator.llm) is AzureOpenAILLM and type(orchestrator.coding) is CodingAgent
             and orchestrator.coding.llm is orchestrator.llm and orchestrator.secondary_coding is None
             and orchestrator.ta4h is None and orchestrator.shadow_runner is None and orchestrator.ta4h_mode == "off")
    _require(not orchestrator.llm._injected_client or scope._settings.environment in {"test", "development"})
    _require(orchestrator.settings is scope._settings
             and _service(orchestrator.settings) == scope._policy.document["azureOpenAI"]
             and (not orchestrator.settings.azure_openai_synthetic_only or source == PROVIDER_SOURCE))
    _require(type(orchestrator.llm.max_retries) is int and 0 <= orchestrator.llm.max_retries <= 4)
    scope._check()
    _require(scope._client is None)  # A permit is for one complete pipeline only.
    scope._client = orchestrator.llm
    scope._retries = orchestrator.llm.max_retries
    token = _ACTIVE.set(scope)
    try:
        yield
        scope._check()
    finally:
        _ACTIVE.reset(token)


def check_model_request(client):
    scope = _ACTIVE.get()
    if scope is not None:
        scope._request(client)
