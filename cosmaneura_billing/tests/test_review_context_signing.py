"""Public encounter provenance must never authorize a coder review token."""
import base64
import hashlib
import hmac
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from cosmaneura_billing import api
from cosmaneura_billing.auth import AuthPrincipal
from cosmaneura_billing.settings import Settings, SettingsError

KEY = b"synthetic-review-signing-key-32-bytes-only"
PRINCIPAL = AuthPrincipal(user_id="local-developer", tenant_id="development",
    practice_id="development", roles=frozenset({"Admin"}))


def encounter():
    return SimpleNamespace(resource_id=str(uuid4()), version=1,
        bundle=SimpleNamespace(submitted_em_code=None))


def issue(stored, principal=PRINCIPAL, key=KEY):
    return api._issue_review_context(stored, principal, signing_key=key,
        recommendation_manifest=(("EM", "99213"),))


def verify(token, stored, principal=PRINCIPAL, key=KEY, version=1):
    return api._verify_review_context(token, stored, principal, version, signing_key=key)


def test_public_source_uuid_cannot_forge_v3_review_manifest(tmp_path):
    app = api.create_app(settings=Settings(environment="test", cpt_auto_fetch=False,
        feedback_path=tmp_path / "feedback.jsonl"))
    stored = app.state.encounter_store.get("ENC-100237")
    assert stored.resource_id == str(uuid5(NAMESPACE_URL,
        f"{app.state.encounter_store.practice_id}:ENC-100237"))
    token = issue(stored)
    encoded, _ = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload.update(recommendationCount=0, recommendationDigest=api._recommendation_digest(()))
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    forged = hmac.new(UUID(stored.resource_id).bytes,
        api._review_context_binding(encoded, PRINCIPAL, 1, stored.resource_id), hashlib.sha256).hexdigest()
    with pytest.raises(ValueError, match="invalid review context"):
        verify(encoded + "." + forged, stored)
    assert verify(token, stored)["recommendationCount"] == 1


def test_legacy_resource_signed_tokens_are_rejected():
    stored = encounter()
    payload = {"v": 2, "engineVersion": "invented", "submittedEm": None,
        "recommendationCount": 0, "recommendationDigest": api._recommendation_digest(())}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    binding = json.dumps([encoded, PRINCIPAL.practice_id, PRINCIPAL.tenant_id,
        PRINCIPAL.user_id, 1], separators=(",", ":")).encode()
    signature = hmac.new(UUID(stored.resource_id).bytes, binding, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):
        verify(encoded + "." + signature, stored)


@pytest.mark.parametrize("changed", ["encounter", "version", "practice", "tenant", "actor", "key"])
def test_shared_key_does_not_allow_cross_context_replay(changed):
    stored = encounter()
    token = issue(stored)
    principal, key, version = PRINCIPAL, KEY, 1
    if changed == "encounter":
        stored = encounter()
    elif changed == "version":
        version = 2
    elif changed == "key":
        key = b"different-synthetic-review-signing-key"
    else:
        attr = {"practice": "practice_id", "tenant": "tenant_id", "actor": "user_id"}[changed]
        principal = replace(principal, **{attr: "another"})
    with pytest.raises(ValueError):
        verify(token, stored, principal, key, version)


def test_configured_key_shares_tokens_between_local_replicas(tmp_path):
    settings = Settings(environment="test", review_context_signing_key=KEY.decode(),
        feedback_path=tmp_path / "feedback.jsonl", cpt_auto_fetch=False)
    first = api.create_app(settings=settings)
    second = api.create_app(settings=settings, encounter_store=first.state.encounter_store)
    stored = first.state.encounter_store.get("ENC-100237")
    token = issue(stored, key=first.state.review_context_signing_key)
    assert verify(token, stored, key=second.state.review_context_signing_key)["v"] == 3


def test_local_ephemeral_keys_are_unique_and_missing_durable_key_fails_closed(tmp_path):
    settings = Settings(environment="test", feedback_path=tmp_path / "feedback.jsonl",
        cpt_auto_fetch=False)
    first, second = api.create_app(settings=settings), api.create_app(settings=settings)
    assert first.state.review_context_signing_key != second.state.review_context_signing_key
    # PostgreSQL composition creates no random-key fallback or network call.
    durable = api.create_app(settings=replace(settings, state_store="postgres",
        chase_store="postgres", practice_id="synthetic", database_url="dbname=unused",
        auth_mode="container_apps", entra_tenant_id=str(uuid4())))
    assert durable.state.review_context_signing_key is None
    with pytest.raises(HTTPException) as raised:
        api._request_review_signing_key(SimpleNamespace(app=durable))
    assert raised.value.status_code == 503


def test_coder_endpoint_stops_before_inference_without_signing_key(tmp_path, monkeypatch):
    app = api.create_app(settings=Settings(environment="test", cpt_auto_fetch=False,
        feedback_path=tmp_path / "feedback.jsonl"))
    app.state.review_context_signing_key = None
    def unexpected(*args, **kwargs):
        raise AssertionError("inference ran without a signing key")
    monkeypatch.setattr(api, "_request_orchestrator", unexpected)
    response = TestClient(app).post("/api/encounters/coder-view", json={"encounterId": "ENC-100237"})
    assert response.status_code == 503
    assert response.json() == {"detail": "review_signing_unavailable"}
    monkeypatch.setattr(api, "_request_feedback_store", unexpected)
    feedback = TestClient(app).post("/api/feedback", json={
        "review_id": str(uuid4()), "review_context": "untrusted-context",
        "encounter_id": "ENC-100237", "encounter_version": 1, "decisions": [],
    })
    assert feedback.status_code == 503
    assert feedback.json() == {"detail": "review_signing_unavailable"}
    app.state.ready = True
    app.state.chase_context = {}
    app.state.orchestrator = SimpleNamespace(ncci=SimpleNamespace(loaded=True),
        coverage=SimpleNamespace(loaded=True))
    assert api.readiness(SimpleNamespace(app=app)).status_code == 503


@pytest.mark.parametrize("value", ["short", " " * 32, "é" * 32, "x" * 257, None])
def test_signing_settings_validate_without_exposing_secret(value):
    with pytest.raises(SettingsError) as raised:
        Settings(review_context_signing_key=value)
    assert "requires 32" in str(raised.value)
    if isinstance(value, str):
        assert value not in str(raised.value)


def test_signing_key_parses_and_is_redacted():
    settings = Settings.from_env({"COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY": KEY.decode()})
    assert settings.review_context_signing_key == KEY.decode()
    assert KEY.decode() not in repr(settings)
