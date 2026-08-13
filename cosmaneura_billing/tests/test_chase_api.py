"""The Card Chase worklist API — the biller-facing surface over the F9 engine.

Covers: the collated queue with contact details; the approve→send outreach
flow; manual card entry during a phone call writing to mock Athena and
auto-closing the item; the direct-contract review bucket refusing outreach;
and the last-4-only rule on manual entry."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import cosmaneura_billing.api as api
import cosmaneura_billing.chase_api as chase_api


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("COSMANEURA_FEEDBACK_TOKEN", raising=False)
    chase_api._WORLD = None            # fresh synthetic world per test
    return TestClient(api.app)


def _queue(client):
    r = client.get("/api/chase/queue")
    assert r.status_code == 200
    return r.json()


def _first_open(payload):
    return next(i for i in payload["items"] if i["sequence_state"] == "open")


def test_queue_collates_patients_with_contact_details(client):
    q = _queue(client)
    assert len(q["items"]) >= 10
    for item in q["items"]:
        assert item["display_name"].startswith("Synthetic")
        assert item["phone"].startswith("(603) 555-")
        assert item["email"].endswith("@example.test")
        assert item["submission_link"].startswith("https://")
        assert item["trigger"] in {"no_card_on_file", "expired",
                                   "eligibility_inactive",
                                   "eligibility_timeout", "manual"}
    assert q["metrics"]["cards_outstanding"] > 0
    review = [i for i in q["items"] if i["sequence_state"] == "human_review"]
    assert len(review) == 2            # the two direct-contract patients
    # actionable work sorts ahead of parked/terminal items
    states = [i["sequence_state"] for i in q["items"]]
    assert states.index("open") < states.index("human_review")


def test_redetection_via_queue_get_is_idempotent(client):
    first = _queue(client)
    second = _queue(client)
    assert len(first["items"]) == len(second["items"])
    assert {i["chase_id"] for i in first["items"]} == \
        {i["chase_id"] for i in second["items"]}


def test_approve_then_send_logs_attempt_with_personalized_body(client):
    item = _first_open(_queue(client))
    cid = item["chase_id"]
    r = client.post(f"/api/chase/{cid}/approve", json={"staff_id": "grace"})
    assert r.status_code == 200 and r.json()["sequence_state"] == "staff_approved"

    r = client.post(f"/api/chase/{cid}/send", json={"staff_id": "grace"})
    assert r.status_code == 200
    body = r.json()
    assert body["item"]["sequence_state"] == "sent"
    assert len(body["item"]["attempts"]) == 1
    assert item["display_name"] in body["message_body"]
    assert "member" not in body["message_body"].lower()


def test_send_without_approval_is_409(client):
    item = _first_open(_queue(client))
    r = client.post(f"/api/chase/{item['chase_id']}/send",
                    json={"staff_id": "grace"})
    assert r.status_code == 409
    assert "approval" in r.json()["detail"]


def test_manual_entry_writes_to_athena_and_autocloses(client):
    """The phone-call path: staff types in the card info, staff is the
    verifier, mock Athena gets the package, the row closes itself."""
    item = _first_open(_queue(client))
    cid = item["chase_id"]
    r = client.post(f"/api/chase/{cid}/manual-entry", json={
        "staff_id": "grace", "payer_name": "Synthetic Health Plan",
        "member_id_last4": "4821"})
    assert r.status_code == 200
    out = r.json()
    assert out["write"]["written"] is True
    assert out["write"]["eligibility"] == "ACTIVE"
    assert out["item"]["sequence_state"] == "verified_closed"
    assert out["item"]["insurance"]["member_id_masked"] == "…4821"
    assert out["item"]["resolution"]["verified_by"] == "grace"


def test_manual_entry_rejects_more_than_last4(client):
    item = _first_open(_queue(client))
    r = client.post(f"/api/chase/{item['chase_id']}/manual-entry", json={
        "staff_id": "grace", "payer_name": "Synthetic Health Plan",
        "member_id_last4": "123456789"})
    assert r.status_code == 422        # a full member ID can never transit


def test_simulate_submission_closes_via_cardcapture_path(client):
    item = _first_open(_queue(client))
    cid = item["chase_id"]
    r = client.post(f"/api/chase/{cid}/simulate-submission",
                    json={"staff_id": "demo"})
    assert r.status_code == 200
    assert r.json()["item"]["sequence_state"] == "verified_closed"
    # replay is idempotent — nothing double-written
    again = client.post(f"/api/chase/{cid}/simulate-submission",
                        json={"staff_id": "demo"})
    assert again.json()["write"]["duplicate"] is True


def test_direct_contract_review_items_refuse_outreach(client):
    q = _queue(client)
    parked = next(i for i in q["items"]
                  if i["sequence_state"] == "human_review")
    r = client.post(f"/api/chase/{parked['chase_id']}/approve",
                    json={"staff_id": "grace"})
    assert r.status_code == 409        # illegal transition — stays parked


def test_unreachable_requires_reason(client):
    item = _first_open(_queue(client))
    r = client.post(f"/api/chase/{item['chase_id']}/unreachable",
                    json={"staff_id": "grace"})
    assert r.status_code == 422
    r = client.post(f"/api/chase/{item['chase_id']}/unreachable",
                    json={"staff_id": "grace",
                          "reason": "phone disconnected, email bounced"})
    assert r.status_code == 200
    assert r.json()["sequence_state"] == "unreachable"


def test_mutating_routes_honor_the_token_guard(client, monkeypatch):
    item = _first_open(_queue(client))
    monkeypatch.setenv("COSMANEURA_FEEDBACK_TOKEN", "s3cret")
    r = client.post(f"/api/chase/{item['chase_id']}/approve",
                    json={"staff_id": "grace"})
    assert r.status_code == 401
    r = client.post(f"/api/chase/{item['chase_id']}/approve",
                    json={"staff_id": "grace"},
                    headers={"x-feedback-token": "s3cret"})
    assert r.status_code == 200
