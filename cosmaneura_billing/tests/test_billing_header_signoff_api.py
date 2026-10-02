from copy import deepcopy
from uuid import uuid4

import pytest

from cosmaneura_billing.billing_header_signoff import MemoryHeaderFeedbackAdapter
from test_billing_header_api import client, generate, edit_intent, BASE, ENCOUNTER


def request_for(client, view):
    encounter_id = view["header"]["expectedSource"]["encounterId"]
    object.__setattr__(client.app.state.settings, "development_roles", ("Coder",))
    engine_response = client.post("/api/encounters/coder-view", json={"encounterId": encounter_id})
    assert engine_response.status_code == 200, engine_response.text
    engine = engine_response.json()
    rows = ([engine["em"]] if engine["em"] else []) + engine["dx"]
    return {"encounterId": encounter_id, "expectedSource": view["header"]["expectedSource"],
        "headerId": view["header"]["headerId"], "expectedRevision": view["revisionNumber"],
        "acknowledged": True, "humanProposalDecisions": [],
        "feedback": {"encounter_id": encounter_id, "encounter_version": engine["encounterVersion"],
            "review_id": str(uuid4()), "review_context": engine["reviewContext"],
            "final_em": engine["em"]["code"] if engine["em"] else None,
            "decisions": [{"kind": row["kind"], "engine_code": row["code"],
                           "action": "accept", "final_code": row["code"]} for row in rows]}}


def test_composed_route_keeps_manifest_and_current_header_binding(client):
    store = MemoryHeaderFeedbackAdapter(client.app.state.billing_header_store)
    client.app.state.feedback_store = store
    view = generate(client)
    payload = request_for(client, view)
    response = client.post(BASE + "/signoff", json=payload)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["headerSignoff"]["headerRevision"] == 0 and store.count() == 1
    assert client.post(BASE + "/signoff", json=payload).json()["replayed"] is True
    changed = client.post(BASE + "/save", json=edit_intent(view))
    assert changed.status_code == 200, changed.text
    assert changed.json()["view"]["signoff"] is None
    assert client.post(BASE + "/signoff", json=payload).status_code == 409
    assert store.count() == 1


@pytest.mark.parametrize("tamper", ["manifest", "context", "source", "revision", "ack", "actor", "encounter"])
def test_composed_route_rejects_unbound_or_stale_review_without_feedback(client, tamper):
    store = MemoryHeaderFeedbackAdapter(client.app.state.billing_header_store)
    client.app.state.feedback_store = store
    view = generate(client)
    payload = request_for(client, view)
    if tamper == "manifest": payload["feedback"]["decisions"] = []; payload["feedback"]["final_em"] = None
    elif tamper == "context": payload["feedback"]["review_context"] = "forged.token"
    elif tamper == "source": payload["expectedSource"] = {**payload["expectedSource"], "bundleSha256": "0" * 64}
    elif tamper == "revision": payload["expectedRevision"] = 1
    elif tamper == "ack": payload["acknowledged"] = False
    elif tamper == "actor": object.__setattr__(client.app.state.settings, "development_roles", ("Provider",))
    else: payload["feedback"]["encounter_id"] = "another-synthetic-encounter"
    response = client.post(BASE + "/signoff", json=payload)
    assert response.status_code in (403, 409, 422), response.text
    assert store.count() == 0


def test_regular_feedback_is_not_silently_promoted_to_header_signoff(client):
    store = MemoryHeaderFeedbackAdapter(client.app.state.billing_header_store)
    client.app.state.feedback_store = store
    view = generate(client)
    payload = request_for(client, view)
    assert client.post("/api/feedback", json=deepcopy(payload["feedback"])).status_code == 200
    response = client.post(BASE + "/signoff", json=payload)
    assert response.status_code == 409 and response.json()["detail"] == "review_conflict"
    assert store.count() == 1


@pytest.mark.parametrize("mode", ["reject", "retain_icd", "retain_em", "empty"])
def test_real_signed_current_manifest_drift_recovers_without_rewriting_header(client, monkeypatch, mode):
    encounter_id = ENCOUNTER
    if mode == "retain_em":
        from cosmaneura_billing.corpus.mdm_cases import fixture_llm, qualified_case
        _, _, qualified_source = qualified_case(4)
        client.app.state.orchestrator.coding.llm = fixture_llm()
        client.app.state.encounter_store.upsert(qualified_source, source="synthetic_fixture")
        encounter_id = qualified_source.encounter_id
    store = MemoryHeaderFeedbackAdapter(client.app.state.billing_header_store)
    client.app.state.feedback_store = store
    baseline = generate(client, encounter_id=encounter_id)
    source = deepcopy(client.app.state.encounter_store.get(encounter_id))
    saved = client.post(BASE + "/save", json=edit_intent(baseline))
    assert saved.status_code == 200
    view = saved.json()["view"]
    original = client.app.state.orchestrator.process
    # A later successful pipeline run on the same source omits an original
    # code. The real coder route issues a NEW signed manifest for that result.
    kind = "EM" if mode == "retain_em" else "ICD10"
    omitted = next(row for row in baseline["header"]["suggestions"] if row["kind"] == kind)
    def changed_run(*args, **kwargs):
        result = original(*args, **kwargs)
        result.provider_card.captured_codes = [row for row in result.provider_card.captured_codes
            if mode != "empty" and row.code != omitted["code"]]
        return result
    monkeypatch.setattr(client.app.state.orchestrator, "process", changed_run)
    payload = request_for(client, view)
    assert not any(d["engine_code"] == omitted["code"] for d in payload["feedback"]["decisions"])
    response = client.post(BASE + "/signoff", json=payload)
    assert response.status_code == 422 and response.json()["detail"] == "invalid_signoff"
    assert store.count() == 0
    missing = baseline["header"]["suggestions"] if mode == "empty" else [omitted]
    retain = mode.startswith("retain")
    payload["baselineDecisions"] = [{"id": row["id"], "decision": "retain" if retain else "reject",
        "reason": "Coder reviewed the earlier suggestion against unchanged documentation"} for row in missing]
    if retain:
        assert client.post(BASE + "/signoff", json=payload).status_code == 422
        payload["feedback"]["decisions"].append({"kind": kind, "engine_code": None,
            "action": "add", "final_code": omitted["code"], "reason_code": "other",
            "note": "Rationale: Coder retains earlier supported code.\nEvidence: " + baseline["header"]["evidence"][0]["text"]})
        if kind == "EM": payload["feedback"]["final_em"] = omitted["code"]
    response = client.post(BASE + "/signoff", json=payload)
    assert response.status_code == 200, response.text
    reviewed = response.json()["view"]
    assert reviewed["header"] == baseline["header"] and reviewed["revision"] == view["revision"]
    assert reviewed["signoff"]["baselineDecisions"] == sorted(payload["baselineDecisions"], key=lambda d: d["id"])
    assert client.app.state.encounter_store.get(encounter_id) == source and store.count() == 1
    assert client.post(BASE + "/signoff", json=payload).json()["replayed"] is True
    assert any(e.action == "billing_header.signoff" and e.status == 200
               for e in client.app.state.access_audit_store.events)
