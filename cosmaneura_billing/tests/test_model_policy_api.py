"""Actual restricted-PG imported source through API, pipeline and header receipt."""
import base64
from dataclasses import replace
from datetime import timedelta
import json
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from athena_production_fixture import prepare_schema
from model_admission_fixture import prepare_model_fixture
import cosmaneura_billing.api as api
import cosmaneura_billing.model_admission as admission
import cosmaneura_billing.billing_header_routes as header_routes
from cosmaneura_billing.billing_header import digest, source_snapshot

DSN = os.environ.get("CAB_ATHENA_PRODUCTION_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit local synthetic PostgreSQL fixture required")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    prepare_schema(DSN)
    fixture = prepare_model_fixture(DSN, tmp_path)
    monkeypatch.setattr(api, "get_orchestrator", lambda *a, **k: fixture.orchestrator)
    app = api.create_app(settings=fixture.settings, frontend_dist=tmp_path / "missing")
    claims = {"auth_typ": "aad", "claims": [
        {"typ": "oid", "val": fixture.actor.user_id},
        {"typ": "tid", "val": fixture.actor.tenant_id}, {"typ": "roles", "val": "Admin"}]}
    headers = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}
    with TestClient(app, headers=headers) as client:
        yield fixture, client


def intent(fixture):
    return {"encounterId": fixture.stored.bundle.encounter_id,
            "expectedSource": source_snapshot(fixture.stored), "operationId": str(uuid4())}


def test_approved_import_generates_header_and_replay_needs_no_new_model_authority(setup):
    fixture, client = setup
    command = intent(fixture)
    detail = client.post("/api/encounters/detail", json={"encounterId": command["encounterId"]})
    assert detail.status_code == 200
    assert detail.json()["inferencePolicy"] == {"allowed": True, "reason": None}
    response = client.post("/api/encounters/header/generate", json=command)
    assert response.status_code == 200, response.text
    assert response.json()["created"] is True and len(fixture.fake.calls) == 2
    proof = response.json()["view"]["header"]["generation"]["modelInputProof"]
    assert proof["policySha256"] == fixture.policy.sha256
    assert proof["sourceSha256"] == digest(source_snapshot(fixture.stored))
    client.app.state.model_data_policy = None
    client.app.state.settings = replace(fixture.settings, model_data_policy_json="")
    assert client.post("/api/encounters/detail", json={"encounterId": command["encounterId"]}).json()["inferencePolicy"]["allowed"] is False
    replay = client.post("/api/encounters/header/generate", json=command)
    assert replay.status_code == 200 and replay.json()["created"] is False
    assert replay.json()["view"] == response.json()["view"] and len(fixture.fake.calls) == 2


def test_policy_presence_keeps_adhoc_and_legacy_closed_after_expiry(setup, monkeypatch):
    fixture, client = setup
    monkeypatch.setattr(admission, "_now", lambda: admission._instant(fixture.policy.document["expiresAt"]) + timedelta(seconds=1))
    assert client.post("/api/billing", json={"encounterId": "authored-adhoc", "signedNote": "Authored fixture"}).status_code == 403
    original = api._get_stored_encounter
    monkeypatch.setattr(api, "_get_stored_encounter", lambda *args: replace(original(*args), source="manual"))
    for endpoint in ("billing", "coder-view", "billing-tab"):
        response = client.post("/api/encounters/" + endpoint, json={"encounterId": fixture.stored.bundle.encounter_id})
        assert response.status_code == 403, response.text
    assert fixture.fake.calls == []


def test_expiry_before_header_insert_rolls_back_without_repeating_model(setup, monkeypatch):
    fixture, client = setup
    command = intent(fixture)
    original = header_routes.generated_header
    def expire_after_pipeline(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(admission, "_now", lambda: admission._instant(fixture.policy.document["expiresAt"]) + timedelta(seconds=1))
        return result
    monkeypatch.setattr(header_routes, "generated_header", expire_after_pipeline)
    response = client.post("/api/encounters/header/generate", json=command)
    assert response.status_code == 403 and len(fixture.fake.calls) == 2
    assert client.app.state.billing_header_store.latest(command["encounterId"], expected_source=command["expectedSource"]) is None
