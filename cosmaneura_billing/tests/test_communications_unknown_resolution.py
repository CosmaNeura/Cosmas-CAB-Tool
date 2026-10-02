"""Investigation is a separate immutable decision, never a retry or recall."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from test_communications_dispatch import (
    DSN, bind, prepare, ready, service, receive, lane, schema, foundation_schema, psycopg, fails,
)

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")


def resolve(item, op, **changes):
    args = dict(decision="confirmed_not_sent", vendor_reference="synthetic-vendor:case-1",
                actor_tenant_id=item.tenant, actor_id=item.actor)
    args.update(changes)
    return item.service.resolve_unknown(op["operationId"], **args)


def unknown(item):
    op = ready(item)
    item.vendor.error = TimeoutError()
    assert item.service.run_once()[0]["status"] == "unknown"
    item.vendor.error = None
    return op


def test_unknown_blocks_new_prepare_authorize_and_claim_across_channels(lane):
    sms = ready(lane)
    voice_binding = bind(lane, "automated_voice")
    lane.service.record_consent(decision_id=str(uuid4()), binding_id=voice_binding, granted=True,
        source_reference="synthetic:voice", actor_tenant_id=lane.tenant, actor_id=lane.actor)
    voice = prepare(lane, voice_binding, channel="automated_voice", text=lane.service.call_script)
    lane.vendor.error = TimeoutError()
    lane.service.run_once()
    lane.vendor.error = None
    for binding, channel, text in [(bind(lane), "sms", lane.policy.templates[0]["text"]),
                                    (voice_binding, "automated_voice", lane.service.call_script)]:
        fails("communications_unknown_requires_resolution", lambda: prepare(lane, binding, channel=channel, text=text))
    fails("communications_unknown_requires_resolution", lambda: lane.service.authorize(
        voice["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor))
    resolve(lane, sms)
    lane.service.authorize(voice["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    assert lane.service.run_once()[0]["status"] == "accepted"
    assert len(lane.vendor.requests) == 2
    assert service(lane).run_once() == []


def test_preexisting_authorization_is_held_until_unknown_investigated(lane):
    first = ready(lane)
    second = prepare(lane, first["bindingId"], text=lane.policy.templates[0]["text"])
    lane.service.authorize(second["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    lane.vendor.error = TimeoutError()
    results = lane.service.run_once()
    assert any(r["status"] == "unknown" for r in results)
    assert any(r.get("reason") == "communications_unknown_requires_resolution" for r in results)
    assert len(lane.vendor.requests) == 1
    lane.vendor.error = None
    resolve(lane, first)
    assert lane.service.run_once()[0]["status"] == "accepted"
    assert len(lane.vendor.requests) == 2


@pytest.mark.parametrize("decision", ["confirmed_delivered", "confirmed_not_sent", "confirmed_attempted"])
def test_resolution_preserves_original_unknown_and_first_actor_and_replay(lane, decision):
    op = unknown(lane)
    before = lane.service.case_view(lane.case)["operations"][0]
    assert before["canResolveUnknown"] is True and before["unknownResolution"] is None
    resolve(lane, op, decision=decision)
    actor = str(uuid4())
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (lane.tenant, actor))
        db.execute("""INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles)
            VALUES (%s,%s,%s,ARRAY['BillingStaff'])""", (lane.practice, lane.tenant, actor))
    resolve(lane, op, decision=decision, actor_id=actor)
    fails("communications_idempotency_conflict", lambda: resolve(lane, op, vendor_reference="synthetic:other"))
    view = service(lane).case_view(lane.case)["operations"][0]
    assert view["status"] == "unknown" and view["canResolveUnknown"] is False
    assert view["needsStaffReview"] is False
    assert view["unknownResolution"]["decision"] == decision
    assert view["unknownResolution"]["vendorReference"] == "synthetic-vendor:case-1"
    assert view["unknownResolution"]["resolvedAt"]
    with lane.store._db() as db:
        rows = db.execute("SELECT actor_id FROM chase_communication_unknown_resolutions WHERE practice_id=%s", (lane.practice,)).fetchall()
        assert len(rows) == 1 and str(rows[0]["actor_id"]) == lane.actor
    assert service(lane).run_once() == [] and len(lane.vendor.requests) == 1


def test_missing_receipt_resolution_wins_before_submit_and_can_never_resume(lane):
    op = ready(lane)
    request = lane.service._claim(op["operationId"])
    assert request is not None
    resolve(lane, op)
    assert lane.service._submit_started(op["operationId"], request) is None
    assert service(lane).run_once() == [] and lane.vendor.requests == []
    state = lane.service.case_view(lane.case)["operations"][0]
    assert state["status"] == "unknown" and state["unknownResolution"]["decision"] == "confirmed_not_sent"


def test_stalled_submit_cannot_be_resolved_and_signed_callback_does_not_deadlock(lane):
    op = ready(lane)
    began, release = Event(), Event()
    def pause(sid):
        began.set()
        assert release.wait(5)
    lane.vendor.before_receipt = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(lane.service.run_once)
        assert began.wait(3)
        try:
            fails("communications_dispatch_in_progress", lambda: resolve(lane, op))
            assert lane.service.case_view(lane.case)["operations"][0]["canResolveUnknown"] is False
            # Callback still commits while the send holds its execution lock.
            receive(lane, "sms_inbound", Body="Synthetic response while sending")
        finally:
            release.set()
        assert running.result(timeout=5)[0]["status"] == "accepted"
    fails("communications_outcome_not_unknown", lambda: resolve(lane, op))
    assert len(lane.vendor.requests) == 1


def test_resolution_failure_rolls_back_and_inactive_actor_cannot_resolve(lane):
    op = unknown(lane)
    def reject(db, action, case_id, operation_id):
        assert action == "communications_resolve_unknown" and operation_id == op["operationId"]
        raise RuntimeError("synthetic audit failure")
    failing = service(lane, transaction_audit=reject)
    with pytest.raises(RuntimeError):
        failing.resolve_unknown(op["operationId"], decision="confirmed_not_sent", vendor_reference="synthetic:1",
            actor_tenant_id=lane.tenant, actor_id=lane.actor)
    assert lane.service.case_view(lane.case)["operations"][0]["canResolveUnknown"] is True
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s", (lane.practice,))
    fails("communications_staff_required", lambda: resolve(lane, op))


def test_resolution_does_not_clear_stop_or_touch_history(lane):
    op = unknown(lane)
    receive(lane, "sms_inbound", Body="STOP")
    resolve(lane, op)
    view = lane.service.case_view(lane.case)
    assert view["suppression"]["sms"] is True
    fails("communications_contact_suppressed", lambda: prepare(lane, bind(lane)))
    with lane.store._db() as db:
        assert db.execute("SELECT count(*) AS n FROM chase_communication_dispatch_starts WHERE practice_id=%s",
                          (lane.practice,)).fetchone()["n"] == 1


@pytest.mark.parametrize("values", [
    {"decision": "probably_not_sent"}, {"vendor_reference": ""}, {"vendor_reference": "patient details here"},
])
def test_resolution_requires_concrete_bounded_investigation(lane, values):
    op = unknown(lane)
    fails("communications_invalid_unknown_resolution", lambda: resolve(lane, op, **values))


def test_resolution_is_tenant_scoped_and_append_only(lane):
    op = unknown(lane)
    resolve(lane, op)
    with psycopg.connect(lane.dsn) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id','other-synthetic-practice',true)")
        assert db.execute("SELECT count(*) FROM chase_communication_unknown_resolutions").fetchone()[0] == 0
    for query in ("UPDATE chase_communication_unknown_resolutions SET decision=decision",
                  "DELETE FROM chase_communication_unknown_resolutions", "TRUNCATE chase_communication_unknown_resolutions"):
        with pytest.raises(psycopg.Error):
            with psycopg.connect(DSN) as db:
                db.execute(query)
    with pytest.raises(psycopg.Error, match="leave expand rung dormant"):
        with psycopg.connect(DSN) as db:
            db.execute((Path(__file__).resolve().parents[1] / "migrations/016_chase_communications_unknown_resolution.down.sql").read_text(), prepare=False)


def test_database_guards_new_preparation_authorization_and_start(lane):
    first = ready(lane)
    second = prepare(lane, first["bindingId"], text=lane.policy.templates[0]["text"])
    lane.service.authorize(second["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    third = prepare(lane, first["bindingId"], text=lane.policy.templates[0]["text"])
    lane.vendor.error = TimeoutError()
    lane.service.run_once()
    inserts = [
        ("""INSERT INTO chase_communication_operations
            (practice_id,operation_id,case_id,binding_id,channel,text,actor_tenant_id,actor_id,body_hash)
            SELECT practice_id,%s,case_id,binding_id,channel,text,actor_tenant_id,actor_id,body_hash
            FROM chase_communication_operations WHERE practice_id=%s AND operation_id=%s""",
            (str(uuid4()), lane.practice, first["operationId"])),
        ("""INSERT INTO chase_communication_authorizations
            (practice_id,operation_id,policy_decision_id,consent_decision_id,binding_id,direct_contract_hash,actor_tenant_id,actor_id)
            SELECT practice_id,%s,policy_decision_id,consent_decision_id,binding_id,direct_contract_hash,actor_tenant_id,actor_id
            FROM chase_communication_authorizations WHERE practice_id=%s AND operation_id=%s""",
            (third["operationId"], lane.practice, first["operationId"])),
        ("INSERT INTO chase_communication_dispatch_starts(practice_id,operation_id) VALUES (%s,%s)",
            (lane.practice, second["operationId"])),
    ]
    for query, params in inserts:
        with pytest.raises(psycopg.errors.CheckViolation, match="unknown requires resolution"):
            with psycopg.connect(lane.dsn) as db:
                db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (lane.practice,))
                db.execute(query, params)


def test_unresolved_start_stays_visible_beyond_history_window(lane):
    first = ready(lane)
    for _ in range(201):
        prepare(lane, first["bindingId"], text=lane.policy.templates[0]["text"])
    lane.vendor.error = TimeoutError()
    lane.service.run_once()
    view = lane.service.case_view(lane.case)
    assert view["truncated"] and len(view["operations"]) == 200
    retained = next(o for o in view["operations"] if o["operationId"] == first["operationId"])
    assert retained["status"] == "unknown" and retained["canResolveUnknown"] is True


def test_missing_receipt_resolution_excludes_a_later_direct_receipt(lane):
    op = ready(lane)
    lane.service._claim(op["operationId"])
    resolve(lane, op)
    with pytest.raises(psycopg.errors.CheckViolation, match="already investigated"):
        with psycopg.connect(lane.dsn) as db:
            db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (lane.practice,))
            db.execute("""INSERT INTO chase_communication_dispatch_receipts
                (practice_id,operation_id,account_sid,outcome) VALUES (%s,%s,%s,'unknown')""",
                (lane.practice, op["operationId"], lane.account))
