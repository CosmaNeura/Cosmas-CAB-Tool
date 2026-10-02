"""Synthetic receiver/idempotency checks; no provider transport or chase mutation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import fields, replace
import sqlite3
from uuid import uuid4

import pytest

from cosmaneura_billing.chase.communications import (
    CommunicationsError, CommunicationsStore, Eligibility,
)


ACCOUNT = "AC" + "0" * 32
PHONE = "+12025550100"
SENDER = "+12025550199"
ALLOWED = Eligibility(**{field.name: True for field in fields(Eligibility)})


@pytest.fixture
def store(tmp_path):
    result = CommunicationsStore(tmp_path / "communications.sqlite")
    result.bind_case("practice-a", "case-a", "synthetic-patient-a", PHONE, SENDER, ACCOUNT)
    return result


def send(store, **changes):
    arguments = dict(practice_id="practice-a", case_id="case-a", operation_id=str(uuid4()),
                     channel="sms", text="Synthetic practice reminder. Reply STOP to stop.",
                     actor_id="synthetic-staff", eligibility=ALLOWED)
    arguments.update(changes)
    return store.send(**arguments)


def inbound(store, text="CALLBACK", **changes):
    arguments = dict(account_sid=ACCOUNT, provider_sid="SM" + uuid4().hex,
                     sender=PHONE, recipient=SENDER, text=text, event_id=str(uuid4()))
    arguments.update(changes)
    return store.receive_sms(**arguments)


def callback(store, op, status="delivered", **changes):
    arguments = dict(account_sid=ACCOUNT, provider_sid=op["providerSid"],
                     sender=SENDER, recipient=PHONE, status=status, event_id=str(uuid4()))
    arguments.update(changes)
    return store.receive_status(**arguments)


def dtmf(store, op, digit, **changes):
    arguments = dict(account_sid=ACCOUNT, provider_sid=op["providerSid"],
                     sender=SENDER, recipient=PHONE, digit=digit, event_id=str(uuid4()))
    arguments.update(changes)
    return store.receive_dtmf(**arguments)


def error(code, call, status=None):
    with pytest.raises(CommunicationsError) as result:
        call()
    assert result.value.code == code
    if status:
        assert result.value.status == status


def test_default_simulation_is_persistent_and_case_scoped(store):
    op = send(store)
    assert op["status"] == "simulated" and op["simulated"] is True
    assert op["providerSid"].startswith("SM") and op["caseId"] == "case-a"
    reopened = CommunicationsStore(store.path)
    view = reopened.case_view("practice-a", "case-a")
    assert view["operations"] == [op]
    assert view["mode"] == "simulated" and len(view["events"]) == 1
    assert store.path.stat().st_mode & 0o777 == 0o600
    error("communications_case_not_bound", lambda: reopened.case_view("practice-b", "case-a"), 404)


@pytest.mark.parametrize("gate", [field.name for field in fields(Eligibility)])
@pytest.mark.parametrize("value", [False, None, 1, "true"])
def test_all_server_gates_require_exact_true(store, gate, value):
    error("communications_policy_blocked", lambda: send(store, eligibility=replace(ALLOWED, **{gate: value})), 403)
    assert store.case_view("practice-a", "case-a")["operations"] == []


def test_request_approval_dictionary_cannot_be_used_as_policy(store):
    error("communications_policy_blocked", lambda: send(store, eligibility=vars(ALLOWED)), 403)


def test_idempotency_persists_and_rejects_changed_body_or_case(store):
    key = str(uuid4())
    original = send(store, operation_id=key)
    assert send(CommunicationsStore(store.path), operation_id=key) == original
    error("communications_idempotency_conflict", lambda: send(store, operation_id=key, text="Changed"), 409)
    store.bind_case("practice-a", "case-b", "synthetic-patient-b", "+12025550101", SENDER, ACCOUNT)
    error("communications_idempotency_conflict", lambda: send(store, operation_id=key, case_id="case-b"), 409)
    assert len(store.case_view("practice-a", "case-a")["operations"]) == 1


def test_concurrent_same_operation_reserves_only_one_row(store):
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(store, operation_id=key), range(2)))
    assert results[0] == results[1]
    assert len(store.case_view("practice-a", "case-a")["events"]) == 1


def test_same_uuid_in_separate_practices_does_not_collide(store):
    store.bind_case("practice-b", "case-a", "synthetic-other", "+12025550102", SENDER, ACCOUNT)
    key = str(uuid4())
    first = send(store, operation_id=key)
    second = send(store, operation_id=key, practice_id="practice-b")
    assert first["providerSid"] != second["providerSid"]
    assert len(store.case_view("practice-b", "case-a")["operations"]) == 1


def test_existing_binding_cannot_be_reassigned(store):
    store.bind_case("practice-a", "case-a", "synthetic-patient-a", PHONE, SENDER, ACCOUNT)
    error("communications_binding_conflict", lambda: store.bind_case(
        "practice-a", "case-a", "synthetic-new", PHONE, SENDER, ACCOUNT), 409)


@pytest.mark.parametrize("changes", [{"operation_id": []}, {"operation_id": "invalid"},
    {"channel": []}, {"channel": "email"}, {"text": ""}, {"text": "x" * 1601},
    {"text": "\ud800"}, {"text": "secret\x00"}, {"actor_id": {}}])
def test_malformed_send_is_a_safe_domain_error(store, changes):
    with pytest.raises(CommunicationsError) as result:
        send(store, **changes)
    assert result.value.status == 400


@pytest.mark.parametrize("changes", [{"account_sid": "AC" + "1" * 32},
    {"sender": "+12025550101"}, {"recipient": "+12025550198"},
    {"provider_sid": "SM" + "f" * 32}])
def test_callbacks_reject_unknown_or_mismatched_route_and_sid(store, changes):
    op = send(store)
    error("communications_callback_binding_mismatch", lambda: callback(store, op, **changes), 403)
    assert store.case_view("practice-a", "case-a")["operations"][0]["status"] == "simulated"


def test_delivery_events_are_deduplicated_and_projection_never_regresses(store):
    op = send(store)
    event_id = str(uuid4())
    first = callback(store, op, event_id=event_id)
    assert callback(store, op, event_id=event_id) == first
    error("communications_event_conflict", lambda: callback(store, op, "sent", event_id=event_id), 409)
    callback(store, op, "sent")
    callback(store, op, "failed")
    view = store.case_view("practice-a", "case-a")
    assert view["operations"][0]["status"] == "delivered"
    assert [e["status"] for e in view["events"]] == ["simulated", "delivered", "sent", "failed"]


def test_call_status_accepts_provider_hyphens_and_preserves_completion(store):
    op = send(store, channel="automated_voice")
    callback(store, op, "in-progress")
    assert store.case_view("practice-a", "case-a")["operations"][0]["status"] == "in_progress"
    callback(store, op, "completed")
    callback(store, op, "ringing")
    assert store.case_view("practice-a", "case-a")["operations"][0]["status"] == "completed"
    error("invalid_provider_status", lambda: callback(store, op, "delivered"), 400)


def test_inbound_replies_need_review_and_provider_sid_deduplicates_across_event_ids(store):
    sid, event_id = "SM" + uuid4().hex, str(uuid4())
    first = inbound(store, provider_sid=sid, event_id=event_id)
    assert first["needsStaffReview"] is True and first["held"] is False
    assert inbound(store, provider_sid=sid, event_id=event_id) == first
    assert inbound(store, provider_sid=sid) == first
    assert len(store.case_view("practice-a", "case-a")["events"]) == 1
    error("communications_provider_message_conflict", lambda: inbound(store, "Changed", provider_sid=sid), 409)
    error("communications_event_conflict", lambda: inbound(store, event_id=event_id), 409)


def test_same_event_id_with_changed_timestamp_conflicts(store):
    sid, event_id = "SM" + uuid4().hex, str(uuid4())
    inbound(store, provider_sid=sid, event_id=event_id, occurred_at="2026-09-26T12:00:00Z")
    error("communications_event_conflict", lambda: inbound(store, provider_sid=sid, event_id=event_id,
        occurred_at="2026-09-26T13:00:00Z"), 409)


def test_sms_stop_and_start_never_create_authoritative_consent(store):
    op = send(store)
    assert inbound(store, " stop ")["disposition"] == "sms_opt_out"
    error("communications_contact_suppressed", lambda: send(store), 409)
    # An exact replay does not send again and remains readable after opt-out.
    assert send(store, operation_id=op["operationId"]) == op
    assert inbound(store, "START")["disposition"] == "sms_opt_in_review"
    assert inbound(store, "HELP")["disposition"] == "help_requested"
    error("communications_contact_suppressed", lambda: send(store), 409)
    assert send(store, channel="automated_voice")["simulated"] is True
    assert store.case_view("practice-a", "case-a")["suppression"] == {"sms": True, "automatedVoice": False}


@pytest.mark.parametrize("digit,disposition", [("1", "callback_requested"),
    ("2", "reports_submitted"), ("9", "stop_calls")])
def test_keypad_intents_are_review_records_not_case_state_changes(store, digit, disposition):
    op = send(store, channel="automated_voice")
    event = dtmf(store, op, digit)
    assert event["disposition"] == disposition and event["needsStaffReview"] is True
    assert store.case_view("practice-a", "case-a")["operations"][0]["status"] == "simulated"
    if digit == "9":
        error("communications_contact_suppressed", lambda: send(store, channel="automated_voice"), 409)
        assert send(store)["simulated"] is True
    else:
        assert store.case_view("practice-a", "case-a")["suppression"]["automatedVoice"] is False


def test_sms_sid_cannot_be_used_for_voice_or_inbound_message(store):
    op = send(store)
    error("callback_channel_mismatch", lambda: dtmf(store, op, "9"), 400)
    error("communications_callback_binding_mismatch", lambda: inbound(store, provider_sid=op["providerSid"]), 403)


@pytest.mark.parametrize("other_practice", ["practice-a", "practice-b"])
def test_ambiguous_shared_contact_is_held_without_case_content_and_stop_applies_to_route(store, other_practice):
    store.bind_case(other_practice, "case-b", "synthetic-patient-b", PHONE, SENDER, ACCOUNT)
    event = inbound(store)
    assert event["held"] is True
    for practice, case in [("practice-a", "case-a"), (other_practice, "case-b")]:
        view = store.case_view(practice, case)
        assert view["heldInboundCount"] == 1 and view["events"] == []
    assert inbound(store, "STOP")["held"] is True
    for practice, case in [("practice-a", "case-a"), (other_practice, "case-b")]:
        error("communications_contact_suppressed", lambda: send(store, practice_id=practice, case_id=case), 409)


def test_unknown_inbound_route_does_not_create_event(store):
    error("communications_callback_binding_mismatch", lambda: inbound(store, sender="+12025550107"), 403)
    assert store.case_view("practice-a", "case-a")["events"] == []


@pytest.mark.parametrize("changes", [{"event_id": []}, {"occurred_at": "2026-09-26"},
    {"occurred_at": []}, {"text": {}}, {"provider_sid": "not-a-sid"}, {"sender": []}])
def test_malformed_inbound_callback_fails_without_mutating_store(store, changes):
    with pytest.raises(CommunicationsError) as result:
        inbound(store, **changes)
    assert result.value.status == 400
    assert store.case_view("practice-a", "case-a")["events"] == []


def test_append_only_database_events_cannot_be_updated_or_deleted(store):
    inbound(store)
    with sqlite3.connect(store.path) as db:
        for statement in ("UPDATE events SET text='changed'", "DELETE FROM events"):
            with pytest.raises(sqlite3.IntegrityError, match="events_are_append_only"):
                db.execute(statement)


def test_injected_submission_failure_is_never_retried_on_replay(store):
    calls = []
    def failing(op):
        calls.append(op)
        raise RuntimeError("Do not expose provider request content")
    key = str(uuid4())
    first = send(store, operation_id=key, submit=failing)
    assert first["status"] == "submission_unknown" and first["needsStaffReview"] is True
    assert send(CommunicationsStore(store.path), operation_id=key, submit=failing) == first
    assert len(calls) == 1
    assert "provider request content" not in str(store.case_view("practice-a", "case-a"))


def test_provider_sid_acceptance_binds_exact_operation_and_collision_stays_unresolved(store):
    sid = "SM" + uuid4().hex
    submit = lambda op: {"outcome": "accepted", "provider_id": sid, "provider_status": "queued"}
    first = send(store, submit=submit)
    callback(store, first)
    second = send(store, submit=submit)
    assert second["providerSid"] is None and second["status"] == "submission_unknown"
    view = store.case_view("practice-a", "case-a")
    assert view["operations"][0]["status"] == "delivered"
    assert view["mode"] == "adapter"


def test_provider_cannot_rebind_an_inbound_message_as_outbound(store):
    sid = "SM" + uuid4().hex
    inbound(store, provider_sid=sid)
    op = send(store, submit=lambda op: {"outcome": "accepted", "provider_id": sid, "provider_status": "queued"})
    assert op["providerSid"] is None and op["status"] == "submission_unknown"


def test_stop_committed_between_reservation_and_submission_prevents_transport(store, monkeypatch):
    other = CommunicationsStore(store.path)
    original_db, finished, calls = store._db, [], []
    @contextmanager
    def interleaved_db():
        with original_db() as db:
            yield db
        finished.append(True)
        if len(finished) == 1:
            inbound(other, "STOP")
    monkeypatch.setattr(store, "_db", interleaved_db)
    op = send(store, submit=lambda op: calls.append(op))
    assert op["status"] == "suppressed_before_submission" and calls == []


def test_interrupted_submission_reservation_is_not_resent_after_restart(store):
    class Interrupted(BaseException):
        pass
    calls, key = [], str(uuid4())
    def interrupted(op):
        calls.append(op)
        raise Interrupted()
    with pytest.raises(Interrupted):
        send(store, operation_id=key, submit=interrupted)
    op = send(CommunicationsStore(store.path), operation_id=key, submit=interrupted)
    assert op["status"] == "submission_pending" and op["needsStaffReview"] is True
    assert len(calls) == 1
