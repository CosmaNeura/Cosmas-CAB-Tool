"""Public receiver integration with local-only, exact synthetic admission."""
from copy import deepcopy
import hashlib
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cosmaneura_billing.api import create_app
from cosmaneura_billing.billing_header import BillingHeaderError, create_draft, sha256, source_snapshot, source_text, text, validate_generated
from cosmaneura_billing.corpus.mdm_cases import qualified_case
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.provider_handoff import HandoffRequest
from provider_admission_support import register_route
from cosmaneura_billing.settings import Settings


def synthetic_envelope(revision=1):
    _, _, source = qualified_case(3)
    note = "Synthetic integration note for José 🧭.\r\n" + source.signed_note.replace("\n", "\r\n")
    note += f"\r\nSource revision {revision}.\r\n"
    return {"schemaVersion": "cab.completed-note-handoff.v1", "operationId": str(uuid4()),
        "sourceRecordId": "completed-note-integration", "sourceRevision": revision, "expectedSource": None,
        "completion": {"status": "completed", "confirmedByProvider": True, "completedAt": "2026-09-30T12:00:00Z"},
        "note": {"id": "source-note", "version": str(revision), "sha256": hashlib.sha256(note.encode()).hexdigest(), "text": note},
        "supportingDocuments": [{"kind": "transcript", "id": "source-transcript", "version": str(revision),
            "text": "Synthetic discussion.\r\nNo new symptoms.", "sha256": hashlib.sha256(b"Synthetic discussion.\r\nNo new symptoms.").hexdigest()}],
        "visit": {"patientAge": 40, "patientSex": "F", "encounterType": "office_established", "placeOfService": "11", "providerSpecialty": "Family Medicine"}}


def synthetic_identity(req):
    value = req.model_dump() if isinstance(req, HandoffRequest) else deepcopy(req)
    return {k: v for k, v in value.items() if k not in {"operationId", "expectedSource"}}


class SyntheticExtraction:
    mode = "mock"
    model_label = "local-completed-note-contract-check"
    calls = 0

    def complete_json(self, system, user, **kwargs):
        self.calls += 1
        assert "Synthetic integration note for José 🧭." in user
        facts, _, _ = qualified_case(3)
        if "extract_clinical_facts" in user:
            return {k: v for k, v in facts.items() if not k.startswith("_")}
        return {"diagnoses": [{"concept": "Essential hypertension", "primary": True,
            "quote": "Hypertension is at individualized goal; continue the current treatment."}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = Settings(environment="test", practice_id="completed-note-contract", development_roles=("Provider",),
        billing_header_enabled=True, provider_handoff_enabled=True, feedback_path=str(tmp_path / "feedback.jsonl"))
    store = MemoryEncounterStore(practice_id=config.practice_id)
    app = create_app(settings=config, encounter_store=store, frontend_dist=tmp_path / "none")
    with TestClient(app) as client:
        extraction = SyntheticExtraction()
        app.state.orchestrator.coding.llm = extraction
        yield client, store, extraction, config


def test_actual_receiver_header_edit_reload_cas_and_exact_replay(setup):
    client, store, extraction, _ = setup
    envelope = synthetic_envelope()
    register_route(client, envelope)
    accepted = client.post("/api/provider/encounters/handoff", json=envelope)
    assert accepted.status_code == 200, accepted.text
    receipt = accepted.json()["receipt"]
    assert extraction.calls == 0 and accepted.json()["generation"] == "not_started"
    stored = store.get(receipt["encounterId"])
    assert stored.bundle.signed_note == envelope["note"]["text"] and stored.source == "provider_completed_note"
    generate = {"encounterId": receipt["encounterId"], "expectedSource": receipt["headerSource"], "operationId": str(uuid4())}
    response = client.post("/api/encounters/header/generate", json=generate)
    assert response.status_code == 200, response.text
    view = response.json()["view"]
    assert extraction.calls == 2 and view["header"]["suggestions"]
    draft = create_draft(view["header"], None)
    draft["note"] = "Provider reviewed the synthetic completed-note header."
    command = {**generate, "operationId": str(uuid4()), "headerId": view["header"]["headerId"], "expectedRevision": 0,
        "draft": draft, "reason": "Reviewed the exact source revision"}
    saved = client.post("/api/encounters/header/save", json=command)
    assert saved.status_code == 200, saved.text
    assert saved.json()["view"]["revision"]["actor"]["role"] == "provider"
    assert client.post("/api/provider/encounters/handoff", json=envelope).json()["replayed"] is True
    retry = client.post("/api/encounters/header/generate", json=generate)
    assert retry.json()["view"]["revisionNumber"] == 1 and extraction.calls == 2
    current = client.post("/api/encounters/header", json={k: v for k, v in generate.items() if k != "operationId"})
    assert current.json()["view"]["revision"]["draft"]["note"] == draft["note"]
    advanced = synthetic_envelope(2); advanced["expectedSource"] = receipt["headerSource"]
    register_route(client, advanced)
    assert client.post("/api/provider/encounters/handoff", json=advanced).status_code == 200
    old = client.post("/api/provider/encounters/handoff", json=envelope).json()
    assert old["current"] is False and old["launchPath"] is None
    assert client.post("/api/encounters/header/generate", json=generate).status_code == 409
    stale = {**envelope, "operationId": str(uuid4())}
    register_route(client, stale)
    assert client.post("/api/provider/encounters/handoff", json=stale).status_code == 409
    assert any(e.action == "provider_handoff.accept" and e.status == 200 for e in client.app.state.access_audit_store.events)


def test_current_policy_still_denies_every_normal_request_and_does_not_infer(setup, monkeypatch):
    client, store, extraction, config = setup
    for enabled in (True, False):
        object.__setattr__(config, "azure_openai_synthetic_only", enabled)
        response = client.post("/api/provider/encounters/handoff", json=synthetic_envelope())
        assert response.status_code == 403 and response.json()["detail"] == "operational_handoff_not_approved"
    assert not store.all() and extraction.calls == 0


def test_synthetic_harness_does_not_admit_mutated_source_and_inference_gate_remains(setup):
    client, store, extraction, config = setup
    original = synthetic_envelope(); register_route(client, original)
    wrong = deepcopy(original); wrong["note"]["text"] += " changed"
    wrong["note"]["sha256"] = hashlib.sha256(wrong["note"]["text"].encode()).hexdigest()
    assert client.post("/api/provider/encounters/handoff", json=wrong).status_code == 403
    accepted = client.post("/api/provider/encounters/handoff", json=original).json()["receipt"]
    object.__setattr__(config, "azure_openai_synthetic_only", True)
    response = client.post("/api/encounters/header/generate", json={"encounterId": accepted["encounterId"],
        "expectedSource": accepted["headerSource"], "operationId": str(uuid4())})
    assert response.status_code == 403 and extraction.calls == 0


def test_source_and_verbatim_evidence_allow_exact_crlf_but_metadata_does_not(setup):
    client, store, _, _ = setup
    req = synthetic_envelope()
    register_route(client, req)
    receipt = client.post("/api/provider/encounters/handoff", json=req).json()["receipt"]
    stored = store.get(receipt["encounterId"])
    assert source_snapshot(stored)["noteSha256"] == req["note"]["sha256"]
    assert sha256(stored.bundle.signed_note) == hashlib.sha256(req["note"]["text"].encode()).hexdigest()
    response = client.post("/api/encounters/header/generate", json={"encounterId": receipt["encounterId"], "expectedSource": receipt["headerSource"], "operationId": str(uuid4())})
    header = response.json()["view"]["header"]
    generated = {k: deepcopy(v) for k, v in header.items() if k not in {"schemaVersion", "headerId", "requestId", "generatedAt", "createdBy", "expectedSource", "reviewStatus"}}
    quote = req["note"]["text"].split("\r\n\r\n")[0]
    generated["evidence"].append({"id": "crlf-verbatim", "source": "note", "sourceId": receipt["headerSource"]["noteId"],
        "sourceVersion": "1", "start": 0, "end": len(quote), "text": quote, "coordinateSystem": "unicode_code_point"})
    assert "\r\n" in validate_generated(generated, stored)["evidence"][-1]["text"]
    assert source_text(quote) == quote
    with pytest.raises(BillingHeaderError): text("metadata\r\nchange")
    for bad in ("bare\rreturn", "null\x00", "surrogate\ud800", "del\x7f"):
        with pytest.raises(BillingHeaderError): source_text(bad)
