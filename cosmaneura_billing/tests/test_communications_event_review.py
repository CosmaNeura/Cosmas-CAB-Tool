"""Real PostgreSQL event review; synthetic callbacks, no external requests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql

from test_communications_dispatch import (
    DSN, bind, ready, service, receive, lane, schema, foundation_schema, psycopg, fails,
)
from test_chase_communications_postgres import seed_case, receive as legacy_receive
from cosmaneura_billing.chase.communications.event_review import CommunicationsEventReview, review_ref
from cosmaneura_billing.chase.communications.store import CommunicationsError
from cosmaneura_billing.chase.communications.twilio import HttpResponse

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")


def review(lane):
    return CommunicationsEventReview(lane.service)


def decide(lane, item, decision="handled", **changes):
    args = dict(action_id=str(uuid4()), event_ref=item["ref"], source_hash=item["sourceHash"],
        decision=decision, actor_tenant_id=lane.tenant, actor_id=lane.actor,
        contact_snapshot_id=item["contactSnapshotId"] if decision == "attribute" else None)
    case = changes.pop("case_id", lane.case)
    args.update(changes)
    return review(lane).decide(case, **args)


def held_reply(lane, **values):
    bind(lane)
    snap = seed_case(lane, case="case-b", observed=datetime.now(timezone.utc).isoformat())
    bind(lane, case_id="case-b", contact_snapshot_id=snap)
    receive(lane, "sms_inbound", **values)
    return review(lane).read(lane.case)["items"][0], review(lane).read("case-b")["items"][0]


def test_reply_handling_is_immutable_replayable_attributed_and_visible_in_history(lane):
    bind(lane)
    receive(lane, "sms_inbound", Body="Synthetic callback requested")
    item = review(lane).read(lane.case)["items"][0]
    assert item["kind"] == "sms_reply" and item["held"] is False
    assert item["canAttribute"] is False and item["contactSnapshotId"] is None
    action = str(uuid4())
    assert decide(lane, item, action_id=action)["totalPending"] == 0
    assert decide(lane, item, action_id=action)["totalPending"] == 0
    fails("communications_idempotency_conflict", lambda: decide(lane, item, action_id=action, source_hash="f" * 64))
    view = service(lane).case_view(lane.case)
    assert len(view["events"]) == 1 and view["events"][0]["text"] == item["text"]
    assert view["events"][0]["needsStaffReview"] is False and view["reviewPendingCount"] == 0
    with lane.store._db() as db:
        row = db.execute("SELECT actor_id FROM chase_communication_event_reviews WHERE practice_id=%s", (lane.practice,)).fetchone()
        assert str(row["actor_id"]) == lane.actor
        assert db.execute("SELECT count(*) AS n FROM chase_communication_provider_events WHERE practice_id=%s", (lane.practice,)).fetchone()["n"] == 1


def test_held_leave_is_case_scoped_and_explicit_attribution_does_not_handle_or_clear_stop(lane):
    first, second = held_reply(lane, Body="STOP")
    assert first["held"] and first["canAttribute"] and first["ref"] == second["ref"]
    fails("communications_review_decision_unavailable", lambda: decide(lane, first))
    assert decide(lane, first, "leave_unassigned")["totalPending"] == 0
    assert review(lane).read("case-b")["heldPending"] == 1
    result = decide(lane, second, "attribute", case_id="case-b")
    assert result["totalPending"] == 1 and result["heldPending"] == 0
    assigned = result["items"][0]
    assert assigned["reviewState"] == "attributed" and not assigned["held"]
    assert assigned["contactSnapshotId"] is None and not assigned["canAttribute"]
    assert decide(lane, assigned, case_id="case-b")["totalPending"] == 0
    for case in [lane.case, "case-b"]:
        state = lane.service.case_view(case)
        assert state["suppression"]["sms"] is True and state["heldInboundCount"] == 0
    state = lane.service.case_view("case-b")
    assert len(state["events"]) == 1 and not state["events"][0]["needsStaffReview"]
    assert state["events"][0]["text"] == "STOP"
    assert lane.service.case_view(lane.case)["events"] == []
    with lane.store._db() as db:
        assert db.execute("SELECT case_id FROM chase_communication_provider_events WHERE practice_id=%s", (lane.practice,)).fetchone()["case_id"] is None
        assert db.execute("SELECT state FROM chase_items WHERE practice_id=%s LIMIT 1", (lane.practice,)).fetchone()["state"] == "needs_review"


def test_historical_contact_can_be_left_unassigned_but_not_misattached(lane):
    bind(lane)
    seed_case(lane, phone="+12025550101", observed=datetime.now(timezone.utc).isoformat())
    receive(lane, "sms_inbound")
    item = review(lane).read(lane.case)["items"][0]
    assert item["held"] and not item["canAttribute"] and item["contactSnapshotId"] is None
    fails("communications_contact_stale", lambda: decide(lane, item, "attribute", contact_snapshot_id=lane.snapshot))
    assert decide(lane, item, "leave_unassigned")["totalPending"] == 0


def test_new_contact_snapshot_invalidates_pending_attribution(lane):
    first, _ = held_reply(lane)
    seed_case(lane, observed=datetime.now(timezone.utc).isoformat())
    fails("communications_contact_stale", lambda: decide(lane, first, "attribute"))
    assert review(lane).read(lane.case)["totalPending"] == 1


def test_concurrent_attribution_can_assign_only_one_case(lane):
    first, second = held_reply(lane)
    def attempt(case, item):
        try:
            decide(lane, item, "attribute", case_id=case)
            return True
        except CommunicationsError as exc:
            assert exc.code in {"communications_review_source_stale", "communications_review_decision_unavailable"}
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: attempt(*pair), [(lane.case, first), ("case-b", second)]))
    assert outcomes.count(True) == 1
    counts = review(lane).counts([lane.case, "case-b"])
    assert sum(c["pending"] for c in counts.values()) == 1
    assert sum(c["held"] for c in counts.values()) == 0


def test_failed_outcome_acknowledgment_binds_current_status_and_does_not_resend(lane):
    ready(lane)
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    receive(lane, "sms_status", sid, MessageStatus="failed")
    first = review(lane).read(lane.case)["items"][0]
    assert first["kind"] == "delivery_failure" and first["text"] == ""
    receive(lane, "sms_status", sid, MessageStatus="undelivered")
    fails("communications_review_source_stale", lambda: decide(lane, first))
    current = review(lane).read(lane.case)["items"][0]
    assert current["sourceHash"] != first["sourceHash"]
    assert decide(lane, current)["totalPending"] == 0
    assert lane.service.case_view(lane.case)["operations"][0]["needsStaffReview"] is False
    receive(lane, "sms_status", sid, MessageStatus="delivered")
    assert review(lane).read(lane.case)["totalPending"] == 0
    assert lane.service.run_once() == [] and len(lane.vendor.requests) == 1


def test_keypad_requires_staff_handling_but_does_not_close_case(lane):
    ready(lane, "automated_voice")
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    receive(lane, "voice_gather", sid, Digits="2")
    item = review(lane).read(lane.case)["items"][0]
    assert item["kind"] == "call_keypad" and item["disposition"] == "reports_submitted"
    decide(lane, item)
    view = lane.service.case_view(lane.case)
    assert all(not e["needsStaffReview"] for e in view["events"])
    with lane.store._db() as db:
        assert db.execute("SELECT state FROM chase_items WHERE practice_id=%s", (lane.practice,)).fetchone()["state"] == "needs_review"


def test_unknown_is_exclusively_investigated_and_prepared_history_is_informational(lane):
    op = ready(lane)
    assert review(lane).read(lane.case)["totalPending"] == 0
    assert all(not e["needsStaffReview"] for e in lane.service.case_view(lane.case)["events"])
    lane.vendor.error = TimeoutError()
    lane.service.run_once()
    assert review(lane).read(lane.case)["totalPending"] == 0
    fails("communications_review_source_stale", lambda: decide(lane,
        {"ref": review_ref("operation", None, op["operationId"]), "sourceHash": "a" * 64}))
    assert lane.service.case_view(lane.case)["operations"][0]["needsStaffReview"] is True


@pytest.mark.parametrize("total", [65,205])
def test_counts_include_pending_sources_outside_both_display_windows(lane, total):
    bind(lane)
    for index in range(total):
        receive(lane, "sms_inbound", Body=f"Synthetic response {index}")
    result = review(lane).read(lane.case)
    assert len(result["items"]) == 50 and result["totalPending"] == total and result["truncated"]
    assert result["items"][0]["text"] == "Synthetic response 0"
    history = lane.service.case_view(lane.case)
    assert len(history["events"]) == min(total,200) and history["reviewPendingCount"] == total
    assert decide(lane, result["items"][0])["totalPending"] == total-1
    assert review(lane).counts([lane.case])[lane.case] == {"pending": total-1, "held": 0}


def test_legacy_inbound_is_reviewable_and_mms_is_not_silently_discarded(lane):
    bind(lane)
    legacy_receive(lane, "Legacy synthetic reply")
    receive(lane, "sms_inbound", Body="", NumMedia="1")
    items = review(lane).read(lane.case)["items"]
    assert len(items) == 2 and sum(i["mediaCount"] for i in items) == 1
    for item in items:
        decide(lane, item)
    assert all(not e["needsStaffReview"] for e in lane.service.case_view(lane.case)["events"])


def test_mandatory_host_audit_failure_rolls_back_decision(lane):
    bind(lane)
    receive(lane, "sms_inbound")
    item = review(lane).read(lane.case)["items"][0]
    def reject(db, action, case_id, operation_id):
        assert action == "communications_review_event" and case_id == lane.case
        raise RuntimeError("synthetic audit failure")
    lane.service = service(lane, transaction_audit=reject)
    with pytest.raises(RuntimeError):
        decide(lane, item)
    assert review(lane).read(lane.case)["totalPending"] == 1


def test_current_membership_required_even_for_replay(lane):
    bind(lane)
    receive(lane, "sms_inbound")
    item, action = review(lane).read(lane.case)["items"][0], str(uuid4())
    decide(lane, item, action_id=action)
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s", (lane.practice,))
    fails("communications_staff_required", lambda: decide(lane, item, action_id=action))


def test_tenant_rls_and_immutable_history(lane):
    bind(lane)
    receive(lane, "sms_inbound")
    decide(lane, review(lane).read(lane.case)["items"][0])
    with psycopg.connect(lane.dsn) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id','other-practice',true)")
        assert db.execute("SELECT count(*) FROM chase_communication_event_reviews").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM chase_communication_review_sources").fetchone()[0] == 0
    for statement in ["UPDATE chase_communication_event_reviews SET decision=decision", "DELETE FROM chase_communication_event_reviews", "TRUNCATE chase_communication_event_reviews"]:
        with pytest.raises(psycopg.Error):
            with psycopg.connect(DSN) as db:
                db.execute(statement)
    with pytest.raises(psycopg.Error, match="leave expand rung dormant"):
        with psycopg.connect(DSN) as db:
            db.execute((Path(__file__).resolve().parents[1]/"migrations/017_chase_communications_event_review.down.sql").read_text(), prepare=False)


def test_direct_sql_cannot_fabricate_source_or_handle_held(lane):
    first, _ = held_reply(lane)
    for source, decision, snapshot in [("f" * 64, "handled", None), (first["ref"], "handled", None),
                                       (first["ref"], "attribute", str(uuid4()))]:
        with pytest.raises(psycopg.Error):
            with psycopg.connect(lane.dsn) as db:
                db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (lane.practice,))
                db.execute("""INSERT INTO chase_communication_event_reviews
                    (practice_id,action_id,case_id,source_ref,source_hash,decision,contact_snapshot_id,actor_tenant_id,actor_id,body_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (lane.practice,str(uuid4()),lane.case,source,first["sourceHash"],decision,snapshot,lane.tenant,lane.actor,"a"*64))
    assert review(lane).read(lane.case)["totalPending"] == 1


def test_replay_by_another_current_staff_member_preserves_first_actor(lane):
    bind(lane)
    receive(lane, "sms_inbound")
    item, action = review(lane).read(lane.case)["items"][0], str(uuid4())
    decide(lane, item, action_id=action)
    second_actor = str(uuid4())
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (lane.tenant, second_actor))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES (%s,%s,%s,ARRAY['BillingStaff'])",
                   (lane.practice, lane.tenant, second_actor))
    decide(lane, item, action_id=action, actor_id=second_actor)
    with lane.store._db() as db:
        row = db.execute("SELECT actor_id FROM chase_communication_event_reviews WHERE practice_id=%s", (lane.practice,)).fetchone()
        assert str(row["actor_id"]) == lane.actor


def test_rejected_provider_request_is_reviewable_without_delivery_claim(lane):
    ready(lane)
    lane.adapter.transport = lambda request: HttpResponse(400, b'{"code":21211}')
    assert lane.service.run_once()[0]["status"] == "rejected"
    item = review(lane).read(lane.case)["items"][0]
    assert item["status"] == "rejected" and item["kind"] == "delivery_failure"
    decide(lane, item)
    assert lane.service.case_view(lane.case)["operations"][0]["status"] == "rejected"
    assert lane.service.run_once() == []


def test_populated_down_rung_fails_closed_for_rls_restricted_table_owner(lane):
    bind(lane)
    receive(lane, "sms_inbound")
    decide(lane, review(lane).read(lane.case)["items"][0])
    owner = "review_owner_" + uuid4().hex[:16]
    # Every DDL change, including temporary ownership, rolls back on the expected
    # RLS error. This simulates a non-superuser migration owner with unset scope.
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
        with psycopg.connect(DSN) as db:
            db.execute(sql.SQL("CREATE ROLE {} NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(owner)))
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(owner)))
            db.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text) TO {}").format(sql.Identifier(owner)))
            db.execute(sql.SQL("ALTER TABLE chase_communication_event_reviews OWNER TO {}").format(sql.Identifier(owner)))
            db.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(owner)))
            db.execute("SELECT set_config('cosmaneura.practice_id','',true)")
            assert db.execute("SELECT count(*) FROM chase_communication_event_reviews").fetchone()[0] == 0
            db.execute((Path(__file__).resolve().parents[1]/"migrations/017_chase_communications_event_review.down.sql").read_text(), prepare=False)
    assert review(lane).read(lane.case)["totalPending"] == 0
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_event_reviews WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 1
