"""Full-pipeline header requests, source races, attribution and feature isolation."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cosmaneura_billing.api import create_app
from cosmaneura_billing.billing_header import create_draft, source_snapshot
from cosmaneura_billing.settings import Settings

BASE = "/api/encounters/header"
ENCOUNTER = "ENC-100237"


@pytest.fixture
def client(tmp_path):
    app = create_app(settings=Settings(environment="test", billing_header_enabled=True,
        practice_id="header-api-test", development_roles=("Provider",),
        feedback_path=str(tmp_path / "feedback.jsonl")), frontend_dist=tmp_path / "dist")
    with TestClient(app) as client:
        yield client


def intent(client, *, encounter_id=ENCOUNTER, **extra):
    detail = client.post("/api/encounters/detail", json={"encounterId": encounter_id})
    assert detail.status_code == 200
    return {"encounterId": encounter_id, "expectedSource": detail.json()["headerSource"], **extra}


def generate(client, payload=None, *, encounter_id=ENCOUNTER):
    response = client.post(BASE + "/generate", json=payload or intent(client, encounter_id=encounter_id, operationId=str(uuid4())))
    assert response.status_code == 200, response.text
    return response.json()["view"]


def edit_intent(view, **extra):
    draft = create_draft(view["header"], view["revision"])
    draft["facts"]["patient_location"] = "Synthetic patient confirmed home location"
    return {"encounterId": view["header"]["expectedSource"]["encounterId"], "expectedSource": view["header"]["expectedSource"],
            "headerId": view["header"]["headerId"], "expectedRevision": view["revisionNumber"],
            "operationId": str(uuid4()), "draft": draft, "reason": "Staff review of synthetic visit", **extra}


def test_pipeline_generated_baseline_anchored_and_note_unchanged(client):
    source = client.app.state.encounter_store.get(ENCOUNTER)
    before = deepcopy(source.bundle)
    view = generate(client)
    header = view["header"]
    assert view["revision"] is None and view["revisionNumber"] == 0
    assert header["generation"]["inferenceMode"] == "mock"
    assert header["suggestions"] and all(c["evidenceIds"] for c in header["suggestions"])
    # The unchanged demo lacks source-qualified clinical complexity. It must
    # retain its decision explanations without inventing an E/M recommendation.
    assert not any(c["kind"] == "EM" for c in header["suggestions"])
    assert any("E/M level is unestablished" in flag["message"] for flag in header["flags"])
    assert not any(c["code"] == "Z80.0" for c in header["suggestions"])
    assert header["expectedSource"] == source_snapshot(source)
    assert source.bundle == before
    events = client.app.state.access_audit_store.events
    assert any(e.action == "billing_header.compute" and e.status == 200 for e in events)


def test_generate_retry_and_new_request_preserve_latest_edits_without_inference(client, monkeypatch):
    command = intent(client, operationId=str(uuid4()))
    view = generate(client, command)
    saved = client.post(BASE + "/save", json=edit_intent(view))
    assert saved.status_code == 200, saved.text
    monkeypatch.setattr(client.app.state.orchestrator, "process", lambda *a, **k: pytest.fail("repeated inference"))
    assert generate(client, command)["revisionNumber"] == 1
    assert generate(client)["revisionNumber"] == 1


@pytest.mark.parametrize("role", ["Provider", "Coder", "Admin"])
def test_authorized_roles_can_edit_and_actor_is_server_derived(client, role):
    object.__setattr__(client.app.state.settings, "development_roles", (role,))
    view = generate(client)
    payload = edit_intent(view)
    response = client.post(BASE + "/save", json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()["view"]
    assert saved["header"] == view["header"]
    assert saved["revision"]["actor"]["role"] == role.lower()
    assert saved["revision"]["reviewStatus"] == "unreviewed"
    assert saved["revision"]["history"][0]["changes"][0]["path"] == "facts.patient_location"
    assert client.post(BASE + "/save", json={**payload, "actor": {"role": "Admin"}}).status_code == 422


def test_billing_staff_cannot_read_or_edit_header(client):
    payload = intent(client)
    object.__setattr__(client.app.state.settings, "development_roles", ("BillingStaff",))
    for suffix, body in (("", payload), ("/generate", {**payload, "operationId": str(uuid4())})):
        assert client.post(BASE + suffix, json=body).status_code == 403


def test_source_changed_before_and_during_generation_fails_without_header(client, monkeypatch):
    command = intent(client, operationId=str(uuid4()))
    wrong = deepcopy(command)
    wrong["expectedSource"]["noteSha256"] = "0" * 64
    assert client.post(BASE + "/generate", json=wrong).status_code == 409
    store = client.app.state.encounter_store
    original = client.app.state.orchestrator.process
    def concurrent_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        old = store.get(ENCOUNTER).bundle
        store.upsert(replace(old, transcript=old.transcript + " New synthetic source revision."))
        return result
    monkeypatch.setattr(client.app.state.orchestrator, "process", concurrent_edit)
    assert client.post(BASE + "/generate", json=command).status_code == 409
    latest = client.post(BASE, json=intent(client))
    assert latest.status_code == 200 and latest.json()["view"] is None


def test_revision_conflict_and_reason_required_do_not_change_baseline(client):
    view = generate(client)
    payload = edit_intent(view)
    assert client.post(BASE + "/save", json={**payload, "reason": " "}).status_code == 422
    assert client.post(BASE + "/save", json=payload).status_code == 200
    assert client.post(BASE + "/save", json={**payload, "operationId": str(uuid4())}).status_code == 409
    changed = deepcopy(payload)
    changed["draft"]["note"] = "Conflicting retry"
    assert client.post(BASE + "/save", json=changed).status_code == 409
    response = client.post(BASE, json=intent(client)).json()["view"]
    assert response["header"] == view["header"] and response["revisionNumber"] == 1


def test_unsupported_code_and_evidence_cannot_be_saved(client):
    view = generate(client)
    payload = edit_intent(view)
    payload["draft"]["codes"].append({"id": "human-code", "kind": "EM", "code": "99215",
                                       "label": "Human proposal", "evidenceIds": []})
    assert client.post(BASE + "/save", json=payload).status_code == 422
    payload["draft"]["codes"][-1]["evidenceIds"] = ["invented-source"]
    assert client.post(BASE + "/save", json=payload).status_code == 422


def test_degraded_inference_never_creates_header(client, monkeypatch):
    monkeypatch.setattr(client.app.state.orchestrator, "process", lambda *a, **k: SimpleNamespace(degraded=True))
    response = client.post(BASE + "/generate", json=intent(client, operationId=str(uuid4())))
    assert response.status_code == 503 and response.json()["detail"] == "generation_unavailable"
    assert client.post(BASE, json=intent(client)).json()["view"] is None


def test_feature_is_off_without_schema_dependency_and_rate_limited(tmp_path, client):
    dormant = create_app(settings=Settings(feedback_path=str(tmp_path / "off.jsonl")))
    assert dormant.state.billing_header_store is None
    with TestClient(dormant) as other:
        assert "billingHeader" not in other.get("/api/session").json()["capabilities"]
        assert other.post(BASE, json={"encounterId": ENCOUNTER, "expectedSource": {}}).status_code == 404
    object.__setattr__(client.app.state.settings, "inference_requests_per_minute", 1)
    assert client.post(BASE + "/generate", json=intent(client, operationId=str(uuid4()))).status_code == 200
    assert client.post(BASE + "/generate", json=intent(client, operationId=str(uuid4()))).status_code == 429


def test_no_visit_duration_or_em_time_inferred_from_generic_minutes(client):
    store = client.app.state.encounter_store
    b = store.get(ENCOUNTER).bundle
    store.upsert(replace(b, signed_note=b.signed_note + " Symptoms last 20 minutes. Not an office visit."))
    facts = {f["key"]: f for f in generate(client)["header"]["facts"]}
    assert facts["visit_duration"]["status"] == "not_documented"
    assert facts["em_time"]["status"] == "not_documented"
    assert facts["visit_type"]["status"] == "not_documented"


def test_contradictory_location_consent_and_example_pos_are_not_resolved_by_first_hit(client):
    store = client.app.state.encounter_store
    b = store.get(ENCOUNTER).bundle
    store.upsert(replace(b, place_of_service="10", encounter_type="telehealth_established",
        signed_note=b.signed_note + " Patient was at home. Patient was in hospital."
        " Patient consented to this video visit. Patient declined consent to video visit."
        " POS: 11. Example POS: 10."))
    header = generate(client)["header"]
    facts = {f["key"]: f for f in header["facts"]}
    assert facts["patient_location"]["status"] == "conflicting"
    assert facts["consent"]["status"] == "conflicting"
    assert len(facts["patient_location"]["evidenceIds"]) == 2
    assert facts["place_of_service"]["status"] == "documented"
    assert facts["place_of_service"]["value"] == "POS: 11."


@pytest.mark.parametrize("joiner", ["; ", " and "])
def test_conflicting_values_in_one_sentence_are_not_documented_as_one_fact(client, joiner):
    store = client.app.state.encounter_store
    b = store.get(ENCOUNTER).bundle
    store.upsert(replace(b, signed_note=b.signed_note + f" POS: 10{joiner}POS: 11."
        f" Session duration: 20 minutes{joiner}Session duration: 40 minutes."))
    facts = {f["key"]: f for f in generate(client)["header"]["facts"]}
    assert facts["place_of_service"]["status"] == "conflicting"
    assert facts["visit_duration"]["status"] == "conflicting"


def test_enabled_header_store_is_rechecked_for_readiness(client, monkeypatch):
    assert client.get("/api/ready").status_code == 200
    from cosmaneura_billing.persistence import PersistenceUnavailable
    def unavailable():
        raise PersistenceUnavailable("synthetic unavailable store")
    monkeypatch.setattr(client.app.state.billing_header_store, "healthcheck", unavailable)
    assert client.get("/api/ready").status_code == 503


def test_held_engine_code_is_not_an_editable_generated_proposal(client, monkeypatch):
    from cosmaneura_billing.corpus.mdm_cases import fixture_llm, qualified_case
    _, _, source = qualified_case(4)
    client.app.state.orchestrator.coding.llm = fixture_llm()
    client.app.state.encounter_store.upsert(source, source="synthetic_fixture")
    from cosmaneura_billing import billing_header_runtime as runtime
    original = runtime.build_billing_tab_view
    def held(*args, **kwargs):
        result = original(*args, **kwargs)
        result["em"]["status"] = "held"
        result["em"]["status_reason"] = "Synthetic held output requires review."
        return result
    monkeypatch.setattr(runtime, "build_billing_tab_view", held)
    header = generate(client, encounter_id=source.encounter_id)["header"]
    assert not any(row["kind"] == "EM" for row in header["suggestions"])
    assert not any(row["kind"] == "EM" for row in create_draft(header)["codes"])
    held_flag = next(row for row in header["flags"] if "Synthetic held output requires review." in row["message"])
    assert held_flag["message"].endswith(": Synthetic held output requires review.")
    assert held_flag["evidenceIds"]
    assert all(ref in {item["id"] for item in header["evidence"]} for ref in held_flag["evidenceIds"])
