"""Authenticated billing discovery/import and exact source-to-model transition."""
from dataclasses import replace
from uuid import uuid4

from test_model_policy_api import setup
from test_athena_review import billing_command
from athena_configured_fixture import CASE
from cosmaneura_billing.billing_header import digest

def test_public_reviewed_note_import_then_full_pipeline_header(setup):
    f, client = setup
    # Retain real API/service/store composition; substitute only the fake wire.
    client.app.state.athena_review_service._factory = f.service._factory
    task = client.post("/api/chase/task", json={"chaseId": CASE})
    assert task.status_code == 200 and task.json()["athenaReview"]["billingAvailable"] is True
    current = client.post("/api/athena/card-chase", json={"chaseId": CASE}).json()["view"]
    discovery = client.post("/api/athena/billing/read", json={"chaseId": CASE, "departmentId": "1"})
    assert discovery.status_code == 200, discovery.text
    token = discovery.json()["pagination"]["selectionToken"]
    preview = client.post("/api/athena/billing/read", json={"chaseId": CASE, "departmentId": "1",
        "sourceEncounterId": "88001", "discoveryToken": token})
    assert preview.status_code == 200, preview.text
    note = preview.json()["note"]
    body = {"chaseId": CASE, **billing_command(current, note), "discoveryToken": token}
    rejected = client.post("/api/athena/billing/apply", json={**body, "confirmedFinishedNote": False})
    assert rejected.status_code == 422 and f.fake.calls == []
    imported = client.post("/api/athena/billing/apply", json=body)
    assert imported.status_code == 200, imported.text
    encounter = imported.json()["view"]["billingImport"]["encounterId"]
    source_calls = len(f.vendor_calls)
    replay = client.post("/api/athena/billing/apply", json=body)
    assert replay.status_code == 200 and replay.json()["operationReceipt"]["replayed"]
    assert replay.json()["view"] == imported.json()["view"] and len(f.vendor_calls) == source_calls
    detail = client.post("/api/encounters/detail", json={"encounterId": encounter})
    assert detail.status_code == 200 and detail.json()["signedNote"] == note["plainText"]
    assert detail.json()["dataClass"] == "operational" and detail.json()["readOnly"] is False
    assert detail.json()["inferencePolicy"] == {"allowed": True, "reason": None}
    generated = client.post("/api/encounters/header/generate", json={"encounterId": encounter,
        "expectedSource": detail.json()["headerSource"], "operationId": str(uuid4())})
    assert generated.status_code == 200, generated.text
    header = generated.json()["view"]["header"]
    assert header["generation"]["modelInputProof"]["sourceSha256"] == digest(detail.json()["headerSource"])
    assert len(f.fake.calls) == 2 and all(call["store"] is False for call in f.fake.calls)
    assert client.post("/api/encounters/detail", json={"encounterId": encounter}).json()["signedNote"] == note["plainText"]
    assert all(method in {"GET", "POST"} and (method == "GET" or path == "/oauth2/v1/token")
               for method, path in f.vendor_calls)


def test_operational_source_denied_without_policy_under_both_synthetic_flag_values(setup):
    f, client = setup
    client.app.state.model_data_policy = None
    encounter = f.stored.bundle.encounter_id
    for synthetic_only in (False, True):
        client.app.state.settings = replace(f.settings, model_data_policy_json="",
                                           azure_openai_synthetic_only=synthetic_only)
        detail = client.post("/api/encounters/detail", json={"encounterId": encounter})
        assert detail.status_code == 200
        assert detail.json()["inferencePolicy"] == {"allowed": False, "reason": "operational_source_model_not_approved"}
        for route in ("billing", "coder-view", "billing-tab"):
            result = client.post("/api/encounters/" + route, json={"encounterId": encounter})
            assert result.status_code == 403, result.text
        result = client.post("/api/encounters/header/generate", json={"encounterId": encounter,
            "expectedSource": detail.json()["headerSource"], "operationId": str(uuid4())})
        assert result.status_code == 403, result.text
    assert f.fake.calls == []


def test_provider_policy_requires_its_missing_canonical_reader_before_readiness(setup):
    import json
    from cosmaneura_billing.model_admission import parse_policy
    f, client = setup
    document = f.policy.document
    document.pop("source")
    document.update(schemaVersion="cab-model-data-policy.v2", sources={
        "athena_staff_review_operational": {},
        "provider_completed_note": {"connectionId": str(uuid4()), "connectionVersion": 1, "dataClass": "synthetic"}})
    raw = json.dumps(document)
    client.app.state.model_data_policy = parse_policy(raw)
    client.app.state.settings = replace(f.settings, model_data_policy_json=raw)
    assert client.get("/api/ready").status_code == 503
    assert f.fake.calls == []
