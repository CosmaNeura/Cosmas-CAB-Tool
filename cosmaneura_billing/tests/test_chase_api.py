"""The chase API surface: worklist, detection, staff actions, metrics."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cosmaneura_billing import api

TODAY = "2026-08-17"

SNAPSHOTS = [
    {"patient_ref": "API-1", "appointment_at": "2026-08-17",
     "has_insurance_package": False, "email": "a@example.invalid",
     "phone": "+15550100", "sms_consent": True, "voice_consent": True},
    {"patient_ref": "API-2", "appointment_at": "2026-08-18",
     "has_insurance_package": True, "card_image_on_file": True,
     "eligibility_status": "INACTIVE", "email": "b@example.invalid"},
    {"patient_ref": "API-3", "appointment_at": "2026-08-19",
     "has_insurance_package": True, "card_image_on_file": True,
     "eligibility_status": "ACTIVE", "email": "c@example.invalid"},
]


@pytest.fixture(autouse=True)
def _fresh_context(monkeypatch):
    """Each test gets an empty worklist — the demo context is a cached singleton."""
    monkeypatch.setenv("COSMANEURA_ENV", "test")
    monkeypatch.setenv("COSMANEURA_CHASE_STORE", "memory")
    api.get_chase_context.cache_clear()
    yield
    api.get_chase_context.cache_clear()


@pytest.fixture()
def client(tmp_path):
    return TestClient(api.create_app(
        settings=api.get_settings(), frontend_dist=tmp_path / "missing-frontend",
    ))


def _detect(client):
    return client.post("/api/chase/detect",
                       json={"today": TODAY, "snapshots": SNAPSHOTS}).json()


def _action(client, chase_id, payload, *, action_id=None):
    return client.post(
        f"/api/chase/{chase_id}/action",
        json={"action_id": action_id or str(uuid4()), **payload},
    )


def test_detect_populates_the_worklist_and_skips_clean_records(client):
    body = _detect(client)
    assert body["checked"] == 3
    assert body["detected"] == 2, "API-3 is clean and must not appear"
    refs = {r["patient_ref"] for r in body["rows"]}
    assert refs == {"API-1", "API-2"}


def test_detect_is_idempotent(client):
    _detect(client)
    second = _detect(client)
    assert second["counts"]["open"] == 2, "re-detection must not duplicate rows"


def test_worklist_buckets_and_filters(client):
    _detect(client)
    body = client.get("/api/chase/worklist").json()
    assert body["counts"]["by_bucket"] == {"missing": 1, "expired": 0,
                                          "eligibility": 1}
    eligibility = client.get("/api/chase/worklist?bucket=eligibility").json()
    assert [r["patient_ref"] for r in eligibility["rows"]] == ["API-2"]


def test_worklist_exposes_the_policy_and_who_approved_it(client):
    policy = client.get("/api/chase/worklist").json()["policy"]
    assert policy["approval_mode"] in ("per_policy", "per_message")
    # Unapproved out of the box: the staff who own the process approve it.
    assert policy["approved"] is False
    performers = {s["channel"]: s["performed_by"] for s in policy["steps"]}
    assert performers["phone"] == "staff"


def test_priority_orders_the_list(client):
    rows = _detect(client)["rows"]
    assert rows[0]["patient_ref"] == "API-1"      # appointment today
    assert rows[0]["priority"] > rows[1]["priority"]


# --------------------------------------------------------------------------- #
# Staff actions
# --------------------------------------------------------------------------- #
def test_assign_then_filter_by_assignee(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _action(client, chase_id, {"action": "assign", "assignee": "maggie"})
    body = client.get("/api/chase/worklist?assignee=maggie").json()
    assert len(body["rows"]) == 1


def test_log_a_phone_attempt_with_minutes(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    row = _action(client, chase_id, {
        "action": "log_attempt", "channel": "phone",
        "outcome": "left_message", "minutes_spent": 3.5,
        "note": "will call back"}).json()
    assert row["touches"] == 1
    assert row["staff_minutes"] == 3.5
    assert row["attempts"][0]["actor"] == "local-developer"


def test_staff_action_retry_is_exactly_once_and_conflicts_fail(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    action_id = str(uuid4())
    payload = {
        "action": "log_attempt", "channel": "phone",
        "outcome": "left_message", "minutes_spent": 2.0,
    }

    first = _action(client, chase_id, payload, action_id=action_id)
    replay = _action(client, chase_id, payload, action_id=action_id)
    conflict = _action(
        client, chase_id, {**payload, "minutes_spent": 3.0},
        action_id=action_id,
    )

    assert first.status_code == replay.status_code == 200
    assert replay.json()["touches"] == 1
    assert replay.json()["staff_minutes"] == 2.0
    assert conflict.status_code == 409


def test_caller_cannot_override_log_attempt_actor(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    r = _action(client, chase_id, {
        "action": "log_attempt", "actor": "cindy", "channel": "phone",
    })
    assert r.status_code == 422


def test_submission_does_not_close_but_verification_does(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    submitted = _action(
        client, chase_id, {"action": "submitted", "note": "uploaded"},
    ).json()
    assert submitted["state"] == "submitted" and submitted["open"] is True

    verified = _action(client, chase_id, {
        "action": "verify", "athena_write_id": "ath-9",
        "eligibility_status": "ACTIVE"}).json()
    assert verified["state"] == "verified_closed" and verified["open"] is False
    assert verified["resolution"]["athena_write_id"] == "ath-9"


def test_verify_records_the_session_principal(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    row = _action(client, chase_id, {"action": "verify"}).json()
    assert row["resolution"]["verified_by"] == "local-developer"


def test_suppress_requires_a_reason(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    bad = _action(client, chase_id, {"action": "suppress", "reason": ""})
    assert bad.status_code == 422
    ok = _action(client, chase_id, {
        "action": "suppress",
        "reason": "patient is self-pay by choice"}).json()
    assert ok["state"] == "resolved_other" and ok["open"] is False


def test_unknown_action_and_unknown_chase(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    assert _action(client, chase_id, {"action": "nope"}).status_code == 400
    assert _action(
        client, "does-not-exist", {"action": "assign"},
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_metrics_report_outstanding_and_flag_the_missing_baseline(client):
    _detect(client)
    body = client.get("/api/chase/metrics").json()
    assert body["measured"]["outstanding"] == 2
    assert body["baseline_available"] is False
    assert "re-ask" in body["note"]


def test_metrics_compute_a_delta_when_a_baseline_is_supplied(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _action(client, chase_id, {
        "action": "log_attempt", "channel": "phone",
        "outcome": "reached", "minutes_spent": 4.0})
    _action(client, chase_id, {"action": "verify"})
    body = client.get(
        "/api/chase/metrics?baseline_minutes_per_card=12&"
        "baseline_claims_held_monthly=400").json()
    assert body["baseline_available"] is True
    assert body["minutes_per_card_delta"] == -8.0
    assert body["measured"]["verified_closed"] == 1
