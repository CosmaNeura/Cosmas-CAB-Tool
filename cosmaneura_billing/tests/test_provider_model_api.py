"""Registered provider note through public API and the actual guarded Azure adapter."""
import base64
from dataclasses import replace
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
import cosmaneura_billing.api as api
from cosmaneura_billing.billing_header import source_snapshot
from provider_model_fixture import prepare_provider_model_fixture
from test_provider_model_admission import DSN, schema


@pytest.fixture
def setup(schema, tmp_path, monkeypatch):
    f = prepare_provider_model_fixture(DSN, tmp_path, synthetic_only=True)
    monkeypatch.setattr(api, "get_orchestrator", lambda *a, **k: f.orchestrator)
    app = api.create_app(settings=f.settings, frontend_dist=tmp_path / "missing")
    principal = {"auth_typ": "aad", "claims": [
        {"typ": "oid", "val": f.actor.user_id}, {"typ": "tid", "val": f.actor.tenant_id},
        {"typ": "roles", "val": "Admin"}]}
    headers = {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(principal).encode()).decode()}
    with TestClient(app, headers=headers) as client:
        yield f, client


def test_registered_synthetic_provider_generates_and_recovers_after_policy_removal(setup):
    f, client = setup
    target = {"encounterId": f.stored.bundle.encounter_id}
    command = {**target, "expectedSource": source_snapshot(f.stored), "operationId": str(uuid4())}
    assert client.get("/api/ready").status_code == 200
    assert client.post("/api/encounters/detail", json=target).json()["inferencePolicy"] == {"allowed": True, "reason": None}
    response = client.post("/api/encounters/header/generate", json=command)
    assert response.status_code == 200, response.text
    proof = response.json()["view"]["header"]["generation"]["modelInputProof"]
    assert proof["schemaVersion"] == "cab-model-input-proof.v2"
    assert proof["sourceOperationId"] == f.payload["operationId"] and len(f.fake.calls) == 2
    client.app.state.model_data_policy = None
    client.app.state.settings = replace(f.settings, model_data_policy_json="")
    assert client.post("/api/encounters/detail", json=target).json()["inferencePolicy"]["allowed"] is False
    denied = client.post("/api/encounters/billing", json=target)
    assert denied.status_code == 403 and len(f.fake.calls) == 2
    replay = client.post("/api/encounters/header/generate", json=command)
    assert replay.status_code == 200 and replay.json()["created"] is False
    assert replay.json()["view"] == response.json()["view"] and len(f.fake.calls) == 2


def test_provider_policy_readiness_requires_025_execute_with_intake_disabled(setup):
    import psycopg
    from psycopg import sql
    f, client = setup
    assert not f.settings.provider_handoff_enabled
    assert client.get("/api/ready").status_code == 200
    privilege = "ON FUNCTION public.cosmaneura_provider_model_source(uuid,uuid,text,text)"
    try:
        with psycopg.connect(f.owner.database_url) as db:
            db.execute(sql.SQL("REVOKE EXECUTE " + privilege + " FROM {}").format(sql.Identifier(f.owner.database_runtime_role)))
        assert client.get("/api/ready").status_code == 503
        assert not f.fake.calls
    finally:
        with psycopg.connect(f.owner.database_url) as db:
            db.execute(sql.SQL("GRANT EXECUTE " + privilege + " TO {}").format(sql.Identifier(f.owner.database_runtime_role)))
