"""P2 access-control, request-boundary, and access-audit regression tests."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
import json
from uuid import UUID

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cosmaneura_billing import api
from cosmaneura_billing.access_audit import MemoryAccessAuditStore
from cosmaneura_billing.auth import (
    AuthPrincipal,
    EntraAuthMiddleware,
    PrincipalError,
    parse_container_apps_principal,
)
from cosmaneura_billing.chase.synthetic_demo import EXPECTED_PACK_DIGEST
from cosmaneura_billing.security import (
    FixedWindowLimiter,
    RequestBodyLimitMiddleware,
    send_json,
)
from cosmaneura_billing.settings import Settings


TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ORIGIN = "https://pilot.example.test"


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "environment": "test",
        "auth_mode": "container_apps",
        "entra_tenant_id": TENANT_ID,
        "practice_id": "practice-test",
        "cors_origins": (ORIGIN,),
        "feedback_path": str(tmp_path / "feedback.jsonl"),
        "access_audit_path": str(tmp_path / "access-audit.jsonl"),
    }
    values.update(overrides)
    return Settings(**values)


def _principal_payload(
    *,
    user_id: str = USER_A,
    tenant_id: str = TENANT_ID,
    roles: tuple[str, ...] = ("Admin",),
    auth_type: str = "aad",
) -> dict:
    return {
        "auth_typ": auth_type,
        "name_typ": "name",
        "claims": [
            {"typ": "oid", "val": user_id},
            {"typ": "tid", "val": tenant_id},
            *({"typ": "roles", "val": role} for role in roles),
        ],
    }


def _encode_principal(payload: dict | None = None, **kwargs) -> str:
    raw = json.dumps(payload or _principal_payload(**kwargs), separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _auth_headers(**kwargs) -> dict[str, str]:
    return {"X-MS-CLIENT-PRINCIPAL": _encode_principal(**kwargs)}


def _client(tmp_path, *, store=None, limiter=None, **settings_overrides):
    settings = _settings(tmp_path, **settings_overrides)
    store = store or MemoryAccessAuditStore()
    application = api.create_app(
        settings=settings,
        frontend_dist=tmp_path / "missing-frontend",
        access_audit_store=store,
        rate_limiter=limiter,
    )
    return TestClient(application), store


def _server_review_context(client, *, user_id: str = USER_A) -> str:
    stored = client.app.state.encounter_store.get("ENC-100237")
    return api._issue_review_context(
        stored,
        AuthPrincipal(
            user_id=user_id,
            tenant_id=TENANT_ID,
            practice_id="practice-test",
            roles=frozenset({"Coder"}),
        ),
    )


def _run_asgi(app, scope: dict, request_messages: list[dict]) -> list[dict]:
    sent: list[dict] = []
    messages = iter(request_messages)

    async def receive():
        try:
            return next(messages)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _status(messages: list[dict]) -> int:
    return next(message["status"] for message in messages
                if message["type"] == "http.response.start")


def test_valid_container_apps_principal_is_normalized(tmp_path):
    principal = parse_container_apps_principal(
        _encode_principal(roles=("Coder", "BillingStaff")),
        _settings(tmp_path),
    )

    assert principal == AuthPrincipal(
        user_id=USER_A,
        tenant_id=TENANT_ID,
        practice_id="practice-test",
        roles=frozenset({"Coder", "BillingStaff"}),
    )


def test_role_typ_metadata_does_not_contaminate_identity_claims(tmp_path):
    payload = _principal_payload(roles=("Coder",))
    payload["role_typ"] = "roles"

    principal = parse_container_apps_principal(
        _encode_principal(payload),
        _settings(tmp_path),
    )

    assert principal.user_id == USER_A
    assert principal.tenant_id == TENANT_ID
    assert principal.roles == frozenset({"Coder"})


@pytest.mark.parametrize(
    "encoded",
    [
        "not-base64!",
        base64.b64encode(b"not-json").decode("ascii"),
        _encode_principal({"auth_typ": "aad", "claims": []}),
        _encode_principal(auth_type="github"),
    ],
)
def test_malformed_or_incomplete_principal_is_rejected(tmp_path, encoded):
    with pytest.raises(PrincipalError):
        parse_container_apps_principal(encoded, _settings(tmp_path))


def test_wrong_tenant_and_unpermitted_role_are_rejected(tmp_path):
    with pytest.raises(PrincipalError, match="tenant"):
        parse_container_apps_principal(
            _encode_principal(tenant_id="22222222-2222-4222-8222-222222222222"),
            _settings(tmp_path),
        )
    with pytest.raises(PermissionError, match="role"):
        parse_container_apps_principal(
            _encode_principal(roles=("Unassigned",)),
            _settings(tmp_path),
        )


def test_public_probes_do_not_require_auth_but_api_does(tmp_path):
    client, _ = _client(tmp_path)

    assert client.get("/api/live").json() == {"status": "alive"}
    assert client.get("/api/ready").status_code == 503
    missing = client.get("/api/health")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/ready/lookalike").status_code == 401


def test_missing_malformed_wrong_tenant_and_role_fail_closed(tmp_path):
    client, store = _client(tmp_path)
    protected = "/api/feedback/summary"

    assert client.get(protected).status_code == 401
    assert client.get(
        protected, headers={"X-MS-CLIENT-PRINCIPAL": "malformed"}
    ).status_code == 401
    assert client.get(
        protected,
        headers=_auth_headers(
            tenant_id="22222222-2222-4222-8222-222222222222"
        ),
    ).status_code == 401
    assert client.get(
        protected, headers=_auth_headers(roles=("Unassigned",))
    ).status_code == 403
    assert client.get(protected, headers=_auth_headers()).status_code == 200

    assert [event.outcome for event in store.events[:4]] == [
        "denied", "denied", "denied", "denied",
    ]
    assert all(not event.actor_user_id for event in store.events[:3])
    assert store.events[3].actor_user_id == USER_A


def test_duplicate_principal_headers_are_rejected(tmp_path):
    settings = _settings(tmp_path)

    async def downstream(scope, receive, send):
        await send_json(send, 200, {"principal": scope["state"]["principal"].user_id})

    middleware = EntraAuthMiddleware(
        downstream,
        settings=settings,
        failure_limiter=FixedWindowLimiter(),
    )
    encoded = _encode_principal()
    messages = _run_asgi(
        middleware,
        {
            "type": "http",
            "method": "GET",
            "path": "/api/feedback/summary",
            "query_string": b"",
            "headers": [
                (b"x-ms-client-principal", encoded.encode("ascii")),
                (b"x-ms-client-principal", encoded.encode("ascii")),
            ],
            "client": ("127.0.0.1", 12345),
        },
        [{"type": "http.request", "body": b"", "more_body": False}],
    )

    assert _status(messages) == 401


def test_conflicting_convenience_identity_header_is_rejected(tmp_path):
    client, store = _client(tmp_path)
    headers = _auth_headers()
    headers["X-MS-CLIENT-PRINCIPAL-ID"] = USER_B

    assert client.get("/api/feedback/summary", headers=headers).status_code == 401
    assert store.events[-1].actor_user_id == ""
    assert store.events[-1].actor_roles == ()


@pytest.mark.parametrize(
    ("path", "payload", "spoofed_field"),
    [
        ("/api/billing", {"encounterId": "SYN-1"}, "userId"),
        ("/api/feedback", {"encounter_id": "SYN-1"}, "coder_id"),
        ("/api/chase/action", {"chaseId": "unknown", "action": "assign"}, "actor"),
    ],
)
def test_caller_supplied_body_identity_is_rejected(
    tmp_path, path, payload, spoofed_field
):
    client, _ = _client(tmp_path)
    payload[spoofed_field] = "attacker-controlled"

    response = client.post(path, json=payload, headers=_auth_headers())

    assert response.status_code == 422
    assert any(
        error["loc"][-1] == spoofed_field and error["type"] == "extra_forbidden"
        for error in response.json()["detail"]
    )


@pytest.mark.parametrize("query_name", ["user_id", "coder_id", "userId", "actor"])
def test_deprecated_query_identity_is_rejected(tmp_path, query_name):
    client, _ = _client(tmp_path)

    response = client.get(
        f"/api/feedback/summary?{query_name}=attacker-controlled",
        headers=_auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "caller-supplied identity is not accepted"
    }


def test_declared_body_over_limit_is_rejected_before_downstream():
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    messages = _run_asgi(
        RequestBodyLimitMiddleware(downstream, max_bytes=4),
        {
            "type": "http",
            "method": "POST",
            "path": "/api/billing",
            "headers": [(b"content-length", b"5")],
        },
        [{"type": "http.request", "body": b"12345", "more_body": False}],
    )

    assert _status(messages) == 413
    assert called is False


def test_streamed_body_over_limit_is_rejected_without_content_length():
    async def consume_body(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send_json(send, 200, {"ok": True})

    messages = _run_asgi(
        RequestBodyLimitMiddleware(consume_body, max_bytes=4),
        {
            "type": "http",
            "method": "POST",
            "path": "/api/billing",
            "headers": [],
        },
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": False},
        ],
    )

    assert _status(messages) == 413


def test_forged_small_length_and_compressed_body_are_rejected():
    async def consume_body(scope, receive, send):
        await receive()
        await send_json(send, 200, {"ok": True})

    base_scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/billing",
    }
    forged = _run_asgi(
        RequestBodyLimitMiddleware(consume_body, max_bytes=4),
        {**base_scope, "headers": [(b"content-length", b"1")]},
        [{"type": "http.request", "body": b"12345", "more_body": False}],
    )
    compressed = _run_asgi(
        RequestBodyLimitMiddleware(consume_body, max_bytes=4),
        {**base_scope, "headers": [(b"content-encoding", b"gzip")]},
        [{"type": "http.request", "body": b"123", "more_body": False}],
    )

    assert _status(forged) == 413
    assert _status(compressed) == 415


def test_rate_limit_is_per_principal_and_resets_next_window(tmp_path):
    now = [0.0]
    limiter = FixedWindowLimiter(clock=lambda: now[0])
    client, _ = _client(
        tmp_path,
        limiter=limiter,
        general_requests_per_minute=2,
    )
    path = "/api/feedback/summary"

    assert client.get(path, headers=_auth_headers(user_id=USER_A)).status_code == 200
    assert client.get(path, headers=_auth_headers(user_id=USER_A)).status_code == 200
    limited = client.get(path, headers=_auth_headers(user_id=USER_A))
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"

    assert client.get(path, headers=_auth_headers(user_id=USER_B)).status_code == 200
    now[0] = 61.0
    assert client.get(path, headers=_auth_headers(user_id=USER_A)).status_code == 200


def test_cors_allows_only_the_configured_exact_origin(tmp_path):
    client, _ = _client(tmp_path)
    preflight = {
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization,Content-Type",
    }

    allowed = client.options(
        "/api/feedback/summary", headers={"Origin": ORIGIN, **preflight}
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in allowed.headers

    denied = client.options(
        "/api/feedback/summary",
        headers={"Origin": "https://attacker.example", **preflight},
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers

    simple = client.get(
        "/api/feedback/summary",
        headers={
            "Origin": "https://attacker.example",
            **_auth_headers(),
        },
    )
    assert simple.status_code == 403


def test_role_policies_and_server_owned_chase_actor(tmp_path):
    client, _ = _client(tmp_path)
    provider = _auth_headers(roles=("Provider",))
    coder = _auth_headers(roles=("Coder",))
    billing_staff = _auth_headers(roles=("BillingStaff",))
    admin = _auth_headers(roles=("Admin",))
    provider_with_extra_staff = _auth_headers(
        roles=("Provider", "BillingStaff"),
    )
    provider_coder = _auth_headers(roles=("Provider", "Coder"))
    coder_billing_staff = _auth_headers(roles=("Coder", "BillingStaff"))
    admin_coder = _auth_headers(roles=("Admin", "Coder"))

    provider_session = client.get("/api/session", headers=provider).json()
    billing_staff_session = client.get(
        "/api/session", headers=billing_staff,
    ).json()
    coder_session = client.get("/api/session", headers=coder).json()
    admin_session = client.get("/api/session", headers=admin).json()

    assert provider_session["views"] == [
        "provider",
    ]
    assert billing_staff_session["views"] == [
        "chase",
    ]
    assert coder_session["views"] == [
        "provider", "coder", "chase",
    ]
    assert admin_session["views"] == [
        "provider", "coder", "chase",
    ]
    assert all(
        "actions" not in session
        for session in (provider_session, billing_staff_session, coder_session)
    )
    assert admin_session["actions"] == {
        "seedSyntheticChase": {
            "seedDigest": EXPECTED_PACK_DIGEST,
            "caseCount": 4,
        },
    }
    assert client.get(
        "/api/session", headers=admin_coder,
    ).json()["actions"] == admin_session["actions"]
    assert client.get(
        "/api/session", headers=provider_with_extra_staff,
    ).json()["views"] == ["provider"]
    assert client.post(
        "/api/chase/worklist", json={}, headers=provider_with_extra_staff,
    ).status_code == 403
    for expanded in (provider_coder, coder_billing_staff):
        assert client.get("/api/session", headers=expanded).json()["views"] == [
            "provider", "coder", "chase",
        ]
    seeded = client.post(
        "/api/chase/demo-seed",
        json={"seedDigest": EXPECTED_PACK_DIGEST},
        headers=admin,
    )
    assert seeded.status_code == 200
    chase_id = client.post(
        "/api/chase/worklist", json={}, headers=coder_billing_staff,
    ).json()["rows"][0]["chaseId"]
    # Multiple effective roles compose deliberately: Coder grants all views;
    # BillingStaff additionally grants staff transition authority.
    assert client.post(
        "/api/chase/action",
        json={
            "chaseId": chase_id,
            "action_id": "10000000-0000-4000-8000-000000000004",
            "action": "assign",
        },
        headers=coder_billing_staff,
    ).status_code == 200

    # Provider: provider surface only.
    assert client.get("/api/encounters", headers=provider).status_code == 200
    assert client.post(
        "/api/encounters/coder-view",
        json={"encounterId": "ENC-100237"},
        headers=provider,
    ).status_code == 403
    assert client.post(
        "/api/chase/worklist", json={}, headers=provider,
    ).status_code == 403

    assert client.post(
        "/api/feedback",
        json={
            "review_id": "20000000-0000-4000-8000-000000000001",
            "review_context": _server_review_context(client),
            "encounter_id": "ENC-100237",
            "encounter_version": 1,
            "engine_em": "99214",
            "final_em": "99214",
            "decisions": [],
        },
        headers=provider,
    ).status_code == 403

    # BillingStaff: insurance follow-up only.
    assert client.get(
        "/api/encounters", headers=billing_staff,
    ).status_code == 403
    assert client.post(
        "/api/encounters/coder-view",
        json={"encounterId": "ENC-100237"},
        headers=billing_staff,
    ).status_code == 403
    assert client.get(
        "/api/feedback/summary", headers=billing_staff,
    ).status_code == 403
    assert client.post(
        "/api/chase/worklist", json={}, headers=billing_staff,
    ).status_code == 200

    # Coder and Admin: all three user-facing surfaces.
    for headers in (coder, admin):
        assert client.get("/api/encounters", headers=headers).status_code == 200
        assert client.post(
            "/api/encounters/coder-view",
            json={"encounterId": "ENC-100237"},
            headers=headers,
        ).status_code == 200
        assert client.post(
            "/api/chase/worklist", json={}, headers=headers,
        ).status_code == 200

    assert client.get(
        "/api/chase/policy/status", headers=coder,
    ).status_code == 403
    assert client.post(
        "/api/chase/demo-seed",
        json={"seedDigest": EXPECTED_PACK_DIGEST},
        headers=coder,
    ).status_code == 403
    assert client.post(
        "/api/chase/action",
        json={
            "chaseId": "chase-demo",
            "action_id": "10000000-0000-4000-8000-000000000002",
            "action": "assign",
        },
        headers=coder,
    ).status_code == 403
    assert client.post(
        "/api/chase/action",
        json={
            "chaseId": "chase-demo",
            "action_id": "10000000-0000-4000-8000-000000000003",
            "action": "release",
            "reason": "reviewed",
        },
        headers=billing_staff,
    ).status_code == 403
    assert client.post(
        "/api/chase/release-message",
        json={
            "chaseId": "chase-demo",
            "release_id": "10000000-0000-4000-8000-000000000001",
        },
        headers=coder,
    ).status_code == 403


def test_review_context_cannot_be_replayed_by_another_actor(tmp_path):
    client, _ = _client(tmp_path)
    context_for_a = _server_review_context(client, user_id=USER_A)

    response = client.post(
        "/api/feedback",
        headers=_auth_headers(user_id=USER_B, roles=("Coder",)),
        json={
            "review_id": "20000000-0000-4000-8000-000000000099",
            "review_context": context_for_a,
            "encounter_id": "ENC-100237",
            "encounter_version": 1,
            "engine_em": "99214",
            "final_em": "99214",
            "decisions": [],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "review context is invalid; reload the review"
    )
    assert not (tmp_path / "feedback.jsonl").exists()


def test_auth_runs_before_engine_and_authenticated_unknown_stays_404(tmp_path):
    client, _ = _client(tmp_path)
    assert client.app.state.orchestrator is None

    assert client.get("/api/health").status_code == 401
    assert client.app.state.orchestrator is None
    assert client.post(
        "/api/encounters/billing",
        json={"encounterId": "DOES-NOT-EXIST"},
        headers=_auth_headers(roles=("Coder",)),
    ).status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/encounters/ENC-100237"),
        ("post", "/api/encounters/ENC-100237/billing"),
        ("get", "/api/encounters/ENC-100237/coder-view"),
        ("get", "/api/encounters/ENC-100237/billing-tab"),
        ("post", "/api/chase/synthetic-patient/action"),
        ("post", "/api/chase/synthetic-patient/release-message"),
    ],
)
def test_identifier_bearing_legacy_request_targets_are_unreachable(
    tmp_path, method, path,
):
    client, _ = _client(tmp_path)

    response = getattr(client, method)(path, headers=_auth_headers())

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/encounters/detail", {"encounterId": "../ENC-100237"}),
        ("/api/encounters/billing", {"encounterId": "ENC 100237"}),
        ("/api/encounters/coder-view", {"encounterId": "ENC-100237/claim"}),
        ("/api/chase/action", {"chaseId": "patient/path"}),
    ],
)
def test_fixed_path_resource_ids_are_strictly_validated(tmp_path, path, body):
    client, _ = _client(tmp_path)

    response = client.post(path, json=body, headers=_auth_headers())

    assert response.status_code == 422
    identifier = "chaseId" if "chaseId" in body else "encounterId"
    assert any(
        identifier in error["loc"] for error in response.json()["detail"]
    )


def test_fixed_encounter_route_audit_never_carries_the_external_id(tmp_path):
    store = MemoryAccessAuditStore()
    client, _ = _client(tmp_path, store=store)

    response = client.post(
        "/api/encounters/detail",
        json={"encounterId": "ENC-100237"},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    event = store.events[-1]
    assert event.route == "/api/encounters/detail"
    assert event.action == "encounter.view"
    UUID(event.resource_id)
    assert "ENC-100237" not in json.dumps(asdict(event), sort_keys=True)


def test_audit_records_throttled_and_oversized_attempts(tmp_path):
    store = MemoryAccessAuditStore()
    client, _ = _client(
        tmp_path,
        store=store,
        limiter=FixedWindowLimiter(clock=lambda: 0.0),
        general_requests_per_minute=1,
        max_request_bytes=32,
    )
    headers = _auth_headers()

    assert client.get("/api/feedback/summary", headers=headers).status_code == 200
    assert client.get("/api/feedback/summary", headers=headers).status_code == 429
    assert store.events[-1].outcome == "throttled"

    oversized = client.post(
        "/api/billing",
        content=b"{" + b"x" * 64 + b"}",
        headers={**headers, "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert store.events[-1].status == 413
    assert store.events[-1].action == "encounter.compute"


def test_access_audit_records_minimal_identity_and_not_request_secrets(tmp_path):
    store = MemoryAccessAuditStore()
    client, _ = _client(tmp_path, store=store)
    raw_note = "SENSITIVE-NOTE-MUST-NOT-BE-AUDITED"
    bearer = "Bearer SECRET-TOKEN-MUST-NOT-BE-AUDITED"
    encoded_principal = _encode_principal(roles=("Coder",))

    response = client.post(
        "/api/feedback",
        json={
            "review_id": "20000000-0000-4000-8000-000000000002",
            "review_context": _server_review_context(client),
            "encounter_id": "ENC-100237",
            "encounter_version": 1,
            "overall_note": raw_note,
            "decisions": [{
                "kind": "ICD10",
                "engine_code": "R05.9",
                "action": "accept",
                "final_code": "R05.9",
                "note": raw_note,
            }],
        },
        headers={
            "X-MS-CLIENT-PRINCIPAL": encoded_principal,
            "Authorization": bearer,
            "X-Forwarded-For": "203.0.113.77",
        },
    )

    assert response.status_code == 200
    assert len(store.events) == 2
    reservation, event = store.events
    assert reservation.event_id == event.event_id
    assert reservation.outcome == "attempted" and reservation.status == 0
    assert event.actor_user_id == USER_A
    assert event.actor_roles == ("Coder",)
    assert event.action == "encounter.decide"
    UUID(event.resource_id)
    assert event.resource_id != "ENC-100237"
    assert event.route == "/api/feedback"
    assert event.status == 200
    assert event.outcome == "allowed"
    assert event.source_ip != "203.0.113.77"

    serialized = json.dumps([asdict(row) for row in store.events], sort_keys=True)
    assert raw_note not in serialized
    assert bearer not in serialized
    assert encoded_principal not in serialized


def test_access_audit_failure_blocks_mutation_before_route_runs(tmp_path):
    class FailingStore:
        def healthcheck(self):
            return None

        def append(self, event):
            raise OSError("audit volume unavailable")

    client, _ = _client(tmp_path, store=FailingStore())

    response = client.post(
        "/api/feedback",
        headers=_auth_headers(roles=("Coder",)),
        json={
            "review_id": "20000000-0000-4000-8000-000000000003",
            "review_context": _server_review_context(client),
            "encounter_id": "ENC-100237",
            "encounter_version": 1,
            "engine_em": "99214",
            "final_em": "99214",
            "decisions": [],
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "access audit is unavailable"}
    assert not (tmp_path / "feedback.jsonl").exists()


def test_durable_reservation_avoids_ambiguous_retry_if_completion_fails(tmp_path):
    class CompletionFailsStore:
        def __init__(self):
            self.events = []

        def healthcheck(self):
            return None

        def append(self, event):
            if self.events:
                raise OSError("completion unavailable")
            self.events.append(event)

    store = CompletionFailsStore()
    client, _ = _client(tmp_path, store=store)

    response = client.post(
        "/api/feedback",
        headers=_auth_headers(roles=("Coder",)),
        json={
            "review_id": "20000000-0000-4000-8000-000000000004",
            "review_context": _server_review_context(client),
            "encounter_id": "ENC-100237",
            "encounter_version": 1,
            "engine_em": "99214",
            "final_em": "99214",
            "decisions": [],
        },
    )

    assert response.status_code == 200
    assert store.events[0].outcome == "attempted"
    assert "ENC-100237" in (tmp_path / "feedback.jsonl").read_text()
