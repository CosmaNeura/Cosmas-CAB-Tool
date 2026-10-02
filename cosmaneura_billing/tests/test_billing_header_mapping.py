"""Source-bound human mappings, separate from immutable machine holds."""
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing import billing_header_mapping as mapping
from cosmaneura_billing.billing_header_signoff import MemoryHeaderFeedbackAdapter
from test_billing_header_api import client, BASE
from header_mapping_fixture import ENCOUNTER, QUOTE, VISIT, MappingExtraction, generate, edit_intent, request_for


@pytest.fixture
def mapped_setup(client):
    client.app.state.orchestrator.coding.llm = MappingExtraction()
    client.app.state.encounter_store.upsert(deepcopy(VISIT), source="synthetic_fixture")
    view = generate(client)
    candidate, = mapping.mapping_candidates(view["header"])
    request = {"encounterId": ENCOUNTER, "expectedSource": view["header"]["expectedSource"],
        "headerId": view["header"]["headerId"], "expectedRevision": 0,
        "candidateId": candidate["id"], "query": "G43.009"}
    response = client.post(BASE + "/diagnosis-catalog", json=request)
    assert response.status_code == 200, response.text
    catalog = response.json(); choice = next(row for row in catalog["matches"] if row["code"] == "G43.009")
    row = {"id": "human-mapping", "kind": "ICD10", "code": choice["code"], "label": choice["label"],
        "evidenceIds": candidate["evidenceIds"], "mapping": {"candidateId": candidate["id"], "catalogVersion": catalog["catalogVersion"]}}
    draft = h.create_draft(view["header"]); draft["codes"].append(row); draft["diagnosisOrderConfirmed"] = True
    return view, request, draft


def test_mapping_save_review_primary_reload_and_originals_immutable(client, mapped_setup):
    view, search, draft = mapped_setup
    original_source = deepcopy(client.app.state.encounter_store.get(ENCOUNTER).bundle)
    assert not view["header"]["suggestions"]
    assert any(f["id"] == "primary_diagnosis_sequencing" for f in view["header"]["flags"])
    command = edit_intent(view, draft=draft, reason="Reviewed all documented migraine attributes against catalog")
    saved = client.post(BASE + "/save", json=command)
    assert saved.status_code == 200, saved.text
    current = saved.json()["view"]
    assert current["header"] == view["header"] and current["revision"]["reviewStatus"] == "unreviewed"
    assert current["revision"]["actor"]["role"] == "provider"
    assert current["revision"]["draft"]["codes"][0]["mapping"] == draft["codes"][0]["mapping"]
    assert current["revision"]["history"][0]["changes"][0]["after"]["mapping"] == draft["codes"][0]["mapping"]
    assert client.post(BASE + "/save", json=command).json()["created"] is False
    assert client.post(BASE + "/diagnosis-catalog", json=search).status_code == 409
    assert client.post(BASE + "/save", json={**command, "operationId": str(uuid4())}).status_code == 409
    client.app.state.feedback_store = MemoryHeaderFeedbackAdapter(client.app.state.billing_header_store)
    payload = request_for(client, current)
    payload["feedback"]["decisions"].append({"kind": "ICD10", "engine_code": None, "action": "add", "final_code": "G43.009",
        "reason_code": "dx_missed_by_engine", "note": "Rationale: Clinician documented defining attributes.\nEvidence: " + QUOTE})
    payload["humanProposalDecisions"] = [{"id": "human-mapping", "decision": "accept", "reason": "Source and catalog reviewed"},
        {"id": h.DIAGNOSIS_ORDER_DECISION, "decision": "accept", "reason": "Reviewed as encounter primary"}]
    rejected = deepcopy(payload); rejected["humanProposalDecisions"][0]["decision"] = "reject"
    assert client.post(BASE + "/signoff", json=rejected).status_code == 422
    signed = client.post(BASE + "/signoff", json=payload)
    assert signed.status_code == 200, signed.text
    result = signed.json()["view"]
    loaded = client.post(BASE, json={"encounterId": ENCOUNTER, "expectedSource": search["expectedSource"]}).json()["view"]
    assert loaded == result and loaded["header"] == view["header"]
    assert loaded["signoff"]["actor"]["role"] == "coder"
    assert loaded["signoff"]["codeDecisions"][0]["final_code"] == "G43.009"
    assert client.post(BASE + "/signoff", json=payload).json()["replayed"] is True
    assert client.app.state.encounter_store.get(ENCOUNTER).bundle == original_source


@pytest.mark.parametrize("tamper", ["unrelated_evidence", "empty_evidence", "candidate", "em", "label", "code", "catalog", "extra", "duplicate"])
def test_mapping_requires_exact_candidate_evidence_and_catalog(client, mapped_setup, tamper):
    view, _, draft = mapped_setup
    row = draft["codes"][0]
    if tamper == "unrelated_evidence": row["evidenceIds"] = ["unknown"]
    elif tamper == "empty_evidence": row["evidenceIds"] = []
    elif tamper == "candidate": row["mapping"]["candidateId"] = "primary_diagnosis_sequencing"
    elif tamper == "em": row.update(kind="EM", code="99213")
    elif tamper == "label": row["label"] = "Different clinical meaning"
    elif tamper == "code": row["code"] = "G43"
    elif tamper == "catalog": row["mapping"]["catalogVersion"] = "icd10cm-sha256:" + "0" * 64
    elif tamper == "extra": row["mapping"]["accepted"] = True
    else: draft["codes"].append({**deepcopy(row), "id": "duplicate", "code": "G43.019"})
    response = client.post(BASE + "/save", json=edit_intent(view, draft=draft))
    assert response.status_code == (409 if tamper == "catalog" else 422), response.text
    assert not client.app.state.billing_header_store._data["revisions"]


def test_catalog_is_bounded_authorized_source_pinned_and_audited(client, mapped_setup):
    _, request, _ = mapped_setup
    response = client.post(BASE + "/diagnosis-catalog", json={**request, "query": "migraine"})
    assert response.status_code == 200 and len(response.json()["matches"]) <= 20
    assert response.json()["nextOffset"] == 20
    assert [r["code"] for r in response.json()["matches"]] == sorted(r["code"] for r in response.json()["matches"])
    events = [asdict(e) for e in client.app.state.access_audit_store.events if e.action == "billing_header.catalog" and e.status == 200]
    assert events and all(e["resource_id"] == client.app.state.encounter_store.get(ENCOUNTER).resource_id for e in events)
    assert "migraine" not in str(events) and "G43.009" not in str(events) and "query" not in str(events)
    later = client.post(BASE + "/diagnosis-catalog", json={**request, "query": "unspecified", "offset": 1000})
    assert later.status_code == 200 and later.json()["nextOffset"] == 1020
    version, entries = mapping.catalog_snapshot()
    assert entries == tuple(sorted(entries))
    for patch in [{"expectedRevision": 99}, {"expectedSource": {}}, {"headerId": str(uuid4())}, {"candidateId": "withheld-0"}, {"offset": 1_000_001}, {"query": "x"}]:
        assert client.post(BASE + "/diagnosis-catalog", json={**request, **patch}).status_code in (404, 409, 422)
    object.__setattr__(client.app.state.settings, "development_roles", ("BillingStaff",))
    assert client.post(BASE + "/diagnosis-catalog", json=request).status_code == 403


def test_unavailable_catalog_does_not_fallback_and_historical_metadata_remains_viewable(client, mapped_setup, monkeypatch):
    view, request, draft = mapped_setup
    saved = client.post(BASE + "/save", json=edit_intent(view, draft=draft)).json()["view"]
    monkeypatch.setattr(mapping, "catalog_snapshot", lambda: (_ for _ in ()).throw(ValueError("unavailable")))
    assert client.post(BASE + "/diagnosis-catalog", json={**request, "expectedRevision": 1}).status_code == 503
    assert client.post(BASE, json={"encounterId": ENCOUNTER, "expectedSource": request["expectedSource"]}).json()["view"] == saved
    draft["note"] = "Another review"
    assert client.post(BASE + "/save", json=edit_intent(saved, draft=draft)).status_code == 503
