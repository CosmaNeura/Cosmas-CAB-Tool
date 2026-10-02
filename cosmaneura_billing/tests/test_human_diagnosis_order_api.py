"""Real signed recommendation context with separate human sequencing decisions."""
from copy import deepcopy
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.billing_header_signoff import MemoryHeaderFeedbackAdapter
from test_billing_header_api import client, generate, edit_intent, BASE, ENCOUNTER
from test_billing_header_signoff_api import request_for


def test_api_saves_order_only_and_requires_separate_coder_decision(client):
    initial = generate(client)
    original = deepcopy(client.app.state.encounter_store.get(ENCOUNTER).bundle)
    draft = h.create_draft(initial["header"])
    assert sum(row["kind"] == "ICD10" for row in draft["codes"]) >= 2
    draft["codes"].reverse(); draft["diagnosisOrderConfirmed"] = True
    command = edit_intent(initial, draft=draft)
    response = client.post(BASE + "/save", json=command)
    assert response.status_code == 200, response.text
    view = response.json()["view"]
    assert [c["path"] for c in view["revision"]["changes"]] == ["diagnosisOrder"]
    assert view["revision"]["actor"]["role"] == "provider"
    assert client.post(BASE + "/save", json=command).json()["created"] is False
    client.app.state.feedback_store = MemoryHeaderFeedbackAdapter(client.app.state.billing_header_store)
    payload = request_for(client, view)
    assert client.post(BASE + "/signoff", json=payload).status_code == 422
    payload["humanProposalDecisions"] = [{"id": h.DIAGNOSIS_ORDER_DECISION, "decision": "accept", "reason": "Source reviewed for primary and order"}]
    first_code = next(row["code"] for row in draft["codes"] if row["kind"] == "ICD10")
    invalid = deepcopy(payload)
    first = next(d for d in invalid["feedback"]["decisions"] if d["final_code"] == first_code)
    first.update(action="reject", final_code=None, reason_code="other", note="Do not retain primary")
    assert client.post(BASE + "/signoff", json=invalid).status_code == 422
    accepted = client.post(BASE + "/signoff", json=payload)
    assert accepted.status_code == 200, accepted.text
    signed = accepted.json()["view"]
    assert signed["signoff"]["actor"]["role"] == "coder"
    assert signed["signoff"]["humanProposalDecisions"] == payload["humanProposalDecisions"]
    loaded = client.post(BASE, json={"encounterId": ENCOUNTER, "expectedSource": view["header"]["expectedSource"]}).json()["view"]
    assert loaded == signed and signed["header"] == initial["header"]
    assert client.post(BASE + "/signoff", json=payload).json()["replayed"] is True
    assert client.app.state.encounter_store.get(ENCOUNTER).bundle == original


def test_legacy_save_replays_exact_raw_request_without_confirmation(client):
    view = generate(client)
    command = edit_intent(view); del command["draft"]["diagnosisOrderConfirmed"]
    saved = client.post(BASE + "/save", json=command)
    assert saved.status_code == 200
    assert saved.json()["view"]["revision"]["draft"]["diagnosisOrderConfirmed"] is False
    assert client.post(BASE + "/save", json=command).json()["created"] is False
    normalized_retry = deepcopy(command); normalized_retry["draft"]["diagnosisOrderConfirmed"] = False
    assert client.post(BASE + "/save", json=normalized_retry).status_code == 409


@pytest.mark.parametrize("tamper", ["empty", "truthy", "source", "actor", "reserved_code_id"])
def test_sequence_save_cannot_bypass_evidence_actor_or_source(client, tamper):
    view = generate(client)
    draft = h.create_draft(view["header"]); draft["diagnosisOrderConfirmed"] = True
    command = edit_intent(view, draft=draft)
    if tamper == "empty": draft["codes"][0]["evidenceIds"] = []
    elif tamper == "truthy": draft["diagnosisOrderConfirmed"] = 1
    elif tamper == "source": command["expectedSource"]["noteSha256"] = "0" * 64
    elif tamper == "actor": command["actor"] = {"role": "Coder", "id": str(uuid4())}
    else: draft["codes"][0]["id"] = h.DIAGNOSIS_ORDER_DECISION
    assert client.post(BASE + "/save", json=command).status_code in (409, 422)
    assert not client.app.state.billing_header_store._data["revisions"]
