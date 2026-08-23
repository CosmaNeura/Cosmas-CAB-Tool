"""The chase API surface: worklist, detection, staff actions, metrics."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cosmaneura_billing import api
from cosmaneura_billing.chase import State

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
        "/api/chase/action",
        json={
            "chaseId": chase_id,
            "action_id": action_id or str(uuid4()),
            **payload,
        },
    )


def _item(client, chase_id):
    return client.app.state.chase_context["store"].get(chase_id)


def _stage_phone_task(client, chase_id):
    item = _item(client, chase_id)
    item.state = State.IN_PROGRESS
    item.pending_task = {
        "channel": "phone",
        "template_id": "phone.call",
        "created_at": "2026-08-17T12:00:00+00:00",
    }
    return item


def _release_if_needed(client, chase_id):
    if _item(client, chase_id).state is State.NEEDS_REVIEW:
        response = _action(client, chase_id, {
            "action": "release", "reason": "reviewed synthetic test task",
        })
        assert response.status_code == 200


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
    body = client.post("/api/chase/worklist", json={}).json()
    assert body["counts"]["by_bucket"] == {"missing": 1, "expired": 0,
                                          "eligibility": 1}
    eligibility = client.post(
        "/api/chase/worklist", json={"bucket": "eligibility"},
    ).json()
    assert len(eligibility["rows"]) == 1
    assert eligibility["rows"][0]["trigger"] == "eligibility_inactive"
    assert eligibility["rows"][0]["patientLabel"] == "Patient record"
    assert "patient_ref" not in eligibility["rows"][0]
    assert "direct_contract" not in eligibility["rows"][0]
    assert "attempts" not in eligibility["rows"][0]


def test_worklist_exposes_the_policy_and_who_approved_it(client):
    policy = client.post("/api/chase/worklist", json={}).json()["policy"]
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
    body = client.post(
        "/api/chase/worklist", json={"assignee": "maggie"},
    ).json()
    assert len(body["rows"]) == 1


def test_log_a_phone_attempt_with_minutes(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _stage_phone_task(client, chase_id)
    row = _action(client, chase_id, {
        "action": "log_attempt", "channel": "phone",
        "outcome": "left_message", "minutes_spent": 3.5,
        "note": "will call back"}).json()
    assert row["touches"] == 1
    assert row["staffMinutes"] == 3.5
    assert _item(client, chase_id).attempts[0].actor == "local-developer"


def test_staff_action_retry_is_exactly_once_and_conflicts_fail(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _stage_phone_task(client, chase_id)
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
    assert replay.json()["staffMinutes"] == 2.0
    assert conflict.status_code == 409


def test_caller_cannot_override_log_attempt_actor(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _stage_phone_task(client, chase_id)
    r = _action(client, chase_id, {
        "action": "log_attempt", "actor": "cindy", "channel": "phone",
    })
    assert r.status_code == 422


def test_submission_does_not_close_but_verification_does(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _release_if_needed(client, chase_id)
    submitted = _action(
        client, chase_id, {"action": "submitted", "note": "uploaded"},
    ).json()
    assert submitted["state"] == "submitted" and submitted["open"] is True

    verified = _action(client, chase_id, {
        "action": "verify", "source_write_id": "source-9",
        "eligibility_status": "ACTIVE"}).json()
    assert verified["state"] == "verified_closed" and verified["open"] is False
    assert _item(client, chase_id).resolution["source_write_id"] == "source-9"


def test_minimum_necessary_task_reflects_workflow_state_for_staff_controls(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    initial = client.post("/api/chase/task", json={"chaseId": chase_id}).json()
    assert initial["state"] in {"detected", "needs_review"}
    assert initial["assigned"] is False
    assert initial["touches"] == 0
    assert initial["staffMinutes"] == 0
    assert initial["open"] is True
    assert initial["hasPendingTask"] is False
    assert initial["allowedActions"] == (
        ["assign"]
        if initial["state"] == "needs_review"
        else ["assign", "submitted"]
    )
    assert "assignee" not in initial
    assert "patient_ref" not in initial

    _action(client, chase_id, {"action": "assign"})
    _release_if_needed(client, chase_id)
    _stage_phone_task(client, chase_id)
    pending = client.post("/api/chase/task", json={"chaseId": chase_id}).json()
    assert pending["hasPendingTask"] is True
    assert "log_attempt" in pending["allowedActions"]
    _action(client, chase_id, {
        "action": "log_attempt",
        "channel": "phone",
        "outcome": "left_message",
        "minutes_spent": 2.5,
    })
    _action(client, chase_id, {"action": "submitted"})
    submitted = client.post("/api/chase/task", json={"chaseId": chase_id}).json()
    assert submitted["state"] == "submitted"
    assert submitted["assigned"] is True
    assert submitted["touches"] == 1
    assert submitted["staffMinutes"] == 2.5
    assert submitted["contact"] is None
    assert submitted["allowedActions"] == ["assign", "verify"]

    _action(client, chase_id, {"action": "verify"})
    closed = client.post("/api/chase/task", json={"chaseId": chase_id}).json()
    assert closed["state"] == "verified_closed"
    assert closed["open"] is False
    assert closed["contact"] is None
    assert closed["allowedActions"] == []


def test_verify_records_the_session_principal(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _release_if_needed(client, chase_id)
    _action(client, chase_id, {"action": "submitted"})
    _action(client, chase_id, {"action": "verify"})
    assert _item(client, chase_id).resolution["verified_by"] == "local-developer"


def test_action_response_is_minimum_necessary(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    body = _action(client, chase_id, {"action": "assign"}).json()
    assert set(body) == {
        "chaseId", "state", "assigned", "touches", "staffMinutes", "open",
    }
    assert not {
        "patient_ref", "direct_contract", "assignee", "pending_task",
        "attempts", "resolution",
    }.intersection(body)


def test_staff_actions_enforce_server_side_workflow_gates(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]

    assert _action(client, chase_id, {
        "action": "log_attempt", "channel": "phone",
    }).status_code == 422
    assert _action(client, chase_id, {"action": "verify"}).status_code == 422

    item = _item(client, chase_id)
    item.state = State.NEEDS_REVIEW
    assert _action(client, chase_id, {"action": "submitted"}).status_code == 422

    item.state = State.DETECTED
    assert _action(client, chase_id, {"action": "submitted"}).status_code == 200
    assert _action(client, chase_id, {"action": "verify"}).status_code == 200
    assert _action(client, chase_id, {"action": "assign"}).status_code == 422


def test_athena_specific_write_receipt_is_not_in_source_neutral_contract(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    response = _action(client, chase_id, {
        "action": "verify", "athena_write_id": "legacy-specific-id",
    })
    assert response.status_code == 422


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
    body = client.post("/api/chase/metrics", json={}).json()
    assert body["measured"]["outstanding"] == 2
    assert body["baseline_available"] is False
    assert "re-ask" in body["note"]


def test_metrics_compute_a_delta_when_a_baseline_is_supplied(client):
    chase_id = _detect(client)["rows"][0]["chase_id"]
    _stage_phone_task(client, chase_id)
    _action(client, chase_id, {
        "action": "log_attempt", "channel": "phone",
        "outcome": "reached", "minutes_spent": 4.0})
    _action(client, chase_id, {"action": "submitted"})
    _action(client, chase_id, {"action": "verify"})
    body = client.post(
        "/api/chase/metrics",
        json={
            "baselineMinutesPerCard": 12,
            "baselineClaimsHeldMonthly": 400,
        },
    ).json()
    assert body["baseline_available"] is True
    assert body["minutes_per_card_delta"] == -8.0
    assert body["measured"]["verified_closed"] == 1
