"""Source-qualified provider receiver, retries and fixed-fixture admission."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from cosmaneura_billing.api import create_app
from cosmaneura_billing.billing_header import HeaderActor, source_snapshot
from cosmaneura_billing.persistence import MemoryEncounterStore, PersistenceUnavailable, SYNTHETIC_DEMO_SOURCE
from cosmaneura_billing.provider_handoff import (FixtureRequest, HandoffError, HandoffRequest,
    fixture_choices, prepare_fixture, prepare_handoff, sha256)
from cosmaneura_billing.provider_handoff_store import MemoryHandoffStore
from cosmaneura_billing.settings import Settings

BASE = "/api/provider/encounters/handoff"
ACTOR = HeaderActor(str(uuid4()), str(uuid4()), "Provider test", "Provider")


def envelope(**changes):
    text = "SYNTHETIC TEST NOTE. Completed office visit. Assessment: viral illness; supportive care discussed."
    return {"schemaVersion": "cab.completed-note-handoff.v1", "operationId": str(uuid4()),
        "sourceRecordId": "synthetic-record-1", "sourceRevision": 1, "expectedSource": None,
        "completion": {"status": "completed", "confirmedByProvider": True, "completedAt": "2026-09-30T12:00:00Z"},
        "note": {"id": "note-1", "version": "1", "text": text, "sha256": sha256(text)},
        "visit": {"patientAge": 30, "patientSex": "F", "encounterType": "office_established",
                  "placeOfService": "11", "providerSpecialty": "Family Medicine"}, **changes}


def prepared(payload):
    return prepare_handoff(HandoffRequest.model_validate(payload), "provider-test")


def admit(store, p, actor=ACTOR):
    from provider_admission_support import register_memory
    if not p.fixture and p.operation_id not in store.operations:
        register_memory(store, p, actor)
    return store.accept(p, actor)


def memory():
    encounters = MemoryEncounterStore(practice_id="provider-test")
    return encounters, MemoryHandoffStore(encounters, authorize=lambda a: a.user_id == ACTOR.user_id)


def test_revision_cas_receipts_and_unchanged_text_invalidates_old_header():
    encounters, store = memory()
    first = envelope()
    one = admit(store, prepared(first), ACTOR)
    assert one["current"] and not one["replayed"]
    assert one["launchPath"] == "/?view=header&encounterId=source-encounter-v1%3A" + one["receipt"]["encounterId"].split(":",1)[1]
    assert admit(store, prepared(first), ACTOR) == {**one, "replayed": True}
    second = envelope(sourceRevision=2, expectedSource=one["receipt"]["headerSource"])
    second["completion"]["completedAt"] = "2026-09-30T13:00:00Z"
    two = admit(store, prepared(second), ACTOR)
    assert two["receipt"]["encounterVersion"] == 2
    assert two["receipt"]["headerSource"]["noteSha256"] == one["receipt"]["headerSource"]["noteSha256"]
    old = admit(store, prepared(first), ACTOR)
    assert old["receipt"] == one["receipt"] and old["replayed"]
    assert old["current"] is False and old["launchPath"] is None
    with pytest.raises(HandoffError, match="source_changed"):
        admit(store, prepared(envelope(expectedSource=two["receipt"]["headerSource"])), ACTOR)
    with pytest.raises(PersistenceUnavailable):
        encounters.upsert(prepared(first).bundle)


def test_same_revision_new_operation_requires_exact_current_cas():
    _, store = memory()
    req = envelope()
    one = admit(store, prepared(req), ACTOR)
    with pytest.raises(HandoffError, match="source_changed"):
        admit(store, prepared({**req, "operationId": str(uuid4())}), ACTOR)
    pin = one["receipt"]["headerSource"]
    same = {**req, "operationId": str(uuid4()), "expectedSource": pin}
    two = admit(store, prepared(same), ACTOR)
    assert two["receipt"]["encounterVersion"] == 1 and two["receipt"]["receiptId"] != one["receipt"]["receiptId"]
    malformed = deepcopy(same); malformed["operationId"] = str(uuid4())
    malformed["expectedSource"]["encounterVersion"] = True
    with pytest.raises(HandoffError, match="source_changed"):
        admit(store, prepared(malformed), ACTOR)
    altered = deepcopy(same); altered["operationId"] = str(uuid4())
    altered["completion"]["status"] = "signed"
    with pytest.raises(HandoffError, match="handoff_conflict"):
        admit(store, prepared(altered), ACTOR)


def test_replay_requires_authorized_same_actor_and_exact_request():
    _, store = memory(); req = envelope()
    admit(store, prepared(req), ACTOR)
    with pytest.raises(HandoffError, match="handoff_conflict"):
        admit(store, prepared({**req, "sourceRecordId": "another-source"}), ACTOR)
    with pytest.raises(HandoffError, match="not_authorized"):
        admit(store, prepared(req), replace(ACTOR, user_id=str(uuid4())))
    store.authorize = lambda a: True
    with pytest.raises(HandoffError, match="handoff_conflict"):
        admit(store, prepared(req), replace(ACTOR, user_id=str(uuid4())))
    with pytest.raises(HandoffError, match="not_authorized"):
        admit(store, prepared(req), replace(ACTOR, role="Coder"))


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(synthetic=True), lambda x: x.update(practiceId="forged"),
    lambda x: x.update(sourceRevision=True), lambda x: x["completion"].update(confirmedByProvider=1),
    lambda x: x["completion"].update(confirmedByProvider=False), lambda x: x["completion"].update(status="draft"),
    lambda x: x["completion"].update(completedAt="2026-09-30T12:00:00"),
    lambda x: x["completion"].update(completedAt="2026-09-30T12:00:00+00:99"),
    lambda x: x["completion"].update(completedAt="2026-09-30T12:00:00-04:60"),
    lambda x: x["note"].update(text="tampered"), lambda x: x["note"].update(id="https://chart.invalid/1"),
    lambda x: x["visit"].update(placeOfService="10"), lambda x: x.update(submitted_em_code="99215"),
    lambda x: x.update(supportingDocuments=[{**x["note"], "kind": "transcript"}]),
])
def test_closed_envelope_rejects_forged_or_ambiguous_source(mutate):
    req = envelope(); mutate(req)
    with pytest.raises(ValidationError):
        HandoffRequest.model_validate(req)


def test_document_exact_bytes_and_identity_only_not_targets():
    req = envelope(); req["note"]["text"] += "\n  "
    req["note"]["sha256"] = sha256(req["note"]["text"])
    p = prepared(req)
    assert p.bundle.signed_note == req["note"]["text"]
    assert p.bundle.submitted_em_code is None and p.bundle.submitted_icd10 == []
    assert req["sourceRecordId"] not in p.bundle.encounter_id
    assert "text" not in p.manifest["note"]


def test_fixture_requires_exact_preseeded_server_manifest():
    encounters, store = memory()
    req = FixtureRequest(operationId=str(uuid4()), fixtureKey=fixture_choices()[0]["key"])
    p = prepare_fixture(req)
    with pytest.raises(HandoffError, match="fixture_unavailable"):
        store.accept(p, ACTOR)
    encounters.upsert(p.bundle, source=SYNTHETIC_DEMO_SOURCE)
    result = store.accept(p, ACTOR)
    assert result["receipt"]["encounterId"] == req.fixtureKey and result["current"]
    row = encounters.get(req.fixtureKey)
    encounters._items[req.fixtureKey] = replace(row, bundle=replace(row.bundle, signed_note="changed"))
    with pytest.raises(HandoffError, match="fixture_unavailable"):
        store.accept(p, ACTOR)
    assert len(store.operations) == 1


class FixtureExtraction:
    """Two exact authored extraction answers for the unmodified fixed fixture."""
    mode = "mock"
    model_label = "offline-provider-fixture"

    def complete_json(self, system, user, **kwargs):
        quote = "Assessment: Essential hypertension [I10], controlled on current therapy. Continue current plan."
        assert quote in user
        if "TASK: propose_diagnoses" in user:
            return {"diagnoses": [{"concept": "Essential hypertension", "primary": True, "quote": quote}]}
        return {"chief_complaint": "Hypertension follow-up", "mdm_problem_events": [
            {"name": "Essential hypertension", "source": "signed_note", "identity_quote": quote,
             "addressed_quote": quote, "status_quote": quote}], "mdm_data_events": [], "mdm_risk_events": []}


def test_fixture_api_handoff_to_full_header_with_mock_pipeline(tmp_path, monkeypatch):
    encounters, _ = memory()
    fixture = prepare_fixture(FixtureRequest(operationId=str(uuid4()), fixtureKey="SYN-RC-0001"))
    encounters.upsert(fixture.bundle, source=SYNTHETIC_DEMO_SOURCE)
    app = create_app(settings=Settings(environment="test", practice_id="provider-test",
        development_roles=("Provider",), billing_header_enabled=True, provider_handoff_enabled=True),
        encounter_store=encounters, frontend_dist=tmp_path / "absent")
    with TestClient(app) as client:
        app.state.orchestrator.llm = app.state.orchestrator.coding.llm = FixtureExtraction()
        assert client.get("/api/session").json()["capabilities"]["providerHandoff"] is True
        choices = client.get(BASE + "/fixtures").json()
        assert any(row["key"] == "SYN-RC-0001" for row in choices["fixtures"])
        command = {"operationId": str(uuid4()), "fixtureKey": "SYN-RC-0001"}
        response = client.post(BASE + "/fixture", json=command)
        assert response.status_code == 200, response.text
        receipt = response.json()["receipt"]
        assert response.json()["generation"] == "not_started"
        result = client.post("/api/encounters/header/generate", json={
            "encounterId": receipt["encounterId"], "expectedSource": receipt["headerSource"], "operationId": str(uuid4())})
        assert result.status_code == 200, result.text
        assert result.json()["view"]["header"]["generation"]["inferenceMode"] == "mock"
        monkeypatch.setattr(app.state.orchestrator, "process", lambda *a, **k: pytest.fail("handoff must not infer"))
        assert client.post(BASE + "/fixture", json=command).json()["replayed"] is True
        assert client.post(BASE, json=envelope()).status_code == 403
        assert client.post(BASE + "/fixture", json={**command, "synthetic": True}).status_code == 422
        assert encounters.get(receipt["encounterId"]).bundle == fixture.bundle
        assert client.get("/api/ready").status_code == 200
        for service in (app.state.provider_handoff_store, app.state.billing_header_store):
            with monkeypatch.context() as patch:
                def unavailable():
                    raise PersistenceUnavailable("synthetic lost permission")
                patch.setattr(service, "healthcheck", unavailable)
                assert client.get("/api/ready").status_code == 503


@pytest.mark.parametrize("header,handoff,role,status", [(True,False,"Provider",404), (False,True,"Provider",404),
                                                     (True,True,"Coder",403), (True,True,"BillingStaff",403)])
def test_release_controls(tmp_path, header, handoff, role, status):
    app = create_app(settings=Settings(environment="test", practice_id="provider-test", development_roles=(role,),
        billing_header_enabled=header, provider_handoff_enabled=handoff), frontend_dist=tmp_path / "none")
    with TestClient(app) as client:
        assert client.get(BASE + "/fixtures").status_code == status
        assert client.get("/api/session").json()["capabilities"].get("providerHandoff", False) is False


def test_setting_rejects_bool_coercion():
    with pytest.raises(ValueError):
        Settings(provider_handoff_enabled=1).validate()
    with pytest.raises(ValueError):
        Settings.from_env({"COSMANEURA_PROVIDER_HANDOFF_ENABLED": "perhaps"})


@pytest.mark.parametrize("character", ["\r", "\x00", "\x7f"])
def test_unsupported_source_controls_fail_without_normalization(character):
    req = envelope(); req["note"]["text"] += character
    req["note"]["sha256"] = sha256(req["note"]["text"])
    with pytest.raises(ValidationError):
        HandoffRequest.model_validate(req)
