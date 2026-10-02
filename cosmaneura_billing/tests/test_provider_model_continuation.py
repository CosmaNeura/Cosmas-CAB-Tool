"""Model admission does not replace current source, member or saved-header authority."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
import psycopg
from cosmaneura_billing import model_admission as admission
from cosmaneura_billing.billing_header import create_draft, source_snapshot
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.migrations import _provision_chase_worker_role
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.persistence import PersistenceUnavailable, PostgresContext, PostgresEncounterStore
from test_provider_model_api import setup
from test_provider_model_admission import fixture, schema


def test_expired_model_permission_still_allows_exact_header_read_edit_and_replay(setup, monkeypatch):
    f, client = setup
    target = {"encounterId": f.stored.bundle.encounter_id, "expectedSource": source_snapshot(f.stored)}
    command = {**target, "operationId": str(uuid4())}
    original = client.post("/api/encounters/header/generate", json=command).json()["view"]["header"]
    assert original["generation"]["modelInputProof"]["sourceOperationId"] == f.payload["operationId"]
    monkeypatch.setattr(admission, "_now", lambda: admission._instant(f.policy.document["expiresAt"]) + timedelta(seconds=1))
    assert client.post("/api/encounters/detail", json={"encounterId": target["encounterId"]}).json()["inferencePolicy"]["allowed"] is False
    # Existing immutable generation recovers before any new model permission.
    assert client.post("/api/encounters/header/generate", json=command).json()["view"]["header"] == original
    draft = create_draft(original); draft["note"] = "Synthetic provider header reviewed after model expiry."
    save = {**target, "headerId": original["headerId"], "expectedRevision": 0,
            "operationId": str(uuid4()), "draft": draft, "reason": "Reviewed existing source"}
    saved = client.post("/api/encounters/header/save", json=save)
    assert saved.status_code == 200, saved.text
    loaded = client.post("/api/encounters/header", json=target)
    assert loaded.status_code == 200 and loaded.json()["view"]["header"] == original
    assert loaded.json()["view"]["revision"]["draft"] == draft
    assert client.post("/api/encounters/header/save", json=save).json()["created"] is False
    assert PostgresEncounterStore(f.context).get(target["encounterId"]) == f.stored
    assert len(f.fake.calls) == 2


def test_historical_offline_header_without_model_proof_survives_policy_change(setup):
    f, client = setup
    local = replace(f.settings, azure_openai_enabled=False, azure_openai_synthetic_only=False, model_data_policy_json="")
    client.app.state.settings = local; client.app.state.model_data_policy = None
    client.app.state.orchestrator = BillingOrchestrator(settings=local, llm=MockLLM())
    target = {"encounterId": f.stored.bundle.encounter_id, "expectedSource": source_snapshot(f.stored)}
    command = {**target, "operationId": str(uuid4())}
    response = client.post("/api/encounters/header/generate", json=command)
    assert response.status_code == 200, response.text
    header = response.json()["view"]["header"]
    assert header["generation"]["inferenceMode"] == "mock" and "modelInputProof" not in header["generation"]
    client.app.state.settings = f.settings; client.app.state.orchestrator = f.orchestrator
    draft = create_draft(header); draft["note"] = "Existing offline header retained."
    saved = client.post("/api/encounters/header/save", json={**target, "headerId": header["headerId"],
        "expectedRevision": 0, "operationId": str(uuid4()), "draft": draft, "reason": "Existing header review"})
    assert saved.status_code == 200, saved.text
    assert client.post("/api/encounters/header", json=target).json()["view"]["header"] == header
    assert client.post("/api/encounters/header/generate", json=command).json()["view"]["revision"]["draft"] == draft
    assert not f.fake.calls


def test_source_reader_rejects_foreign_actor_practice_and_worker(fixture):
    f = fixture
    query = "SELECT public.cosmaneura_provider_model_source(%s::uuid,%s::uuid,%s,%s)"
    for context, actor in ((f.context, replace(f.actor, user_id=str(uuid4()))),
                           (PostgresContext(f.context.dsn, practice_id="unrelated-practice"), f.actor)):
        with pytest.raises(PersistenceUnavailable):
            with context.connection() as db:
                db.execute(query, (actor.tenant_id, actor.user_id, actor.role, f.stored.bundle.encounter_id))
    worker = "provider_model_worker_" + uuid4().hex[:12]
    config = replace(f.owner, chase_worker_database_role=worker, chase_worker_database_password="local-test-only")
    with psycopg.connect(f.owner.database_url) as db:
        _provision_chase_worker_role(db, config)
        assert not db.execute("SELECT has_function_privilege(%s,'public.cosmaneura_provider_model_source(uuid,uuid,text,text)','EXECUTE')", (worker,)).fetchone()[0]
    assert not f.fake.calls
