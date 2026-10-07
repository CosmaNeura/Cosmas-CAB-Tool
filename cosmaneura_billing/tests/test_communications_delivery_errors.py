"""Delivery diagnostics through real restricted PostgreSQL; fake vendor only."""
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

import pytest

from test_communications_dispatch import (
    DSN, PHONE, SENDER, lane, schema, foundation_schema, psycopg,
    ready, receive, service, webhook, fails,
)
from cosmaneura_billing.chase.communications.store import _hash
from cosmaneura_billing.chase.communications.twilio import HttpResponse, TwilioAdapter

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")


def sent(lane):
    ready(lane)
    lane.service.run_once()
    return lane.service.case_view(lane.case)["operations"][0]["providerSid"]


def test_callback_error_is_durable_replay_stable_and_has_no_raw_vendor_text(lane):
    sid = sent(lane)
    args = dict(MessageStatus="undelivered", ErrorCode="30034", ErrorMessage="PRIVATE-CANARY",
                MoreInfo="https://private.invalid/PRIVATE-CANARY", SequenceNumber="1")
    first = receive(lane, "sms_status", sid, **args)
    assert receive(lane, "sms_status", sid, **args) == first
    view = service(lane).case_view(lane.case)
    issue = {"code": "30034", "source": "callback"}
    assert view["operations"][0]["status"] == "undelivered"
    assert view["operations"][0]["deliveryError"] == issue
    assert next(e for e in view["events"] if e["kind"] == "sms_status")["deliveryError"] == issue
    assert "PRIVATE-CANARY" not in json.dumps(view)
    with psycopg.connect(DSN) as db:
        rows = db.execute("SELECT to_jsonb(e) FROM chase_communication_provider_events e WHERE practice_id=%s", (lane.practice,)).fetchall()
    assert len(rows) == 1 and rows[0][0]["error_code"] == "30034"
    assert "PRIVATE-CANARY" not in json.dumps(rows, default=str)
    fails("communications_event_conflict", lambda: receive(lane, "sms_status", sid, **{**args, "ErrorCode": "30007"}))
    assert len(lane.vendor.requests) == 1


def test_effective_status_selects_its_code_without_regression_or_history_loss(lane):
    sid = sent(lane)
    receive(lane, "sms_status", sid, MessageStatus="failed", ErrorCode="30007")
    receive(lane, "sms_status", sid, MessageStatus="undelivered", ErrorCode="30034")
    # A code-less repeat keeps the known code for that same status.
    receive(lane, "sms_status", sid, MessageStatus="undelivered")
    receive(lane, "sms_status", sid, MessageStatus="queued", ErrorCode="30003")
    with psycopg.connect(DSN) as db:
        for _ in range(201):
            db.execute("""INSERT INTO chase_communication_provider_events
                (practice_id,account_sid,event_id,provider_sid,kind,channel,sender,recipient,
                 case_id,text,status,disposition,body_hash) VALUES (%s,%s,%s,%s,
                 'sms_inbound','sms',%s,%s,%s,'Synthetic reply','received','reply_received',%s)""",
                (lane.practice, lane.account, _hash(str(uuid4())), "SM" + uuid4().hex,
                 PHONE, SENDER, lane.case, _hash("synthetic")))
    view = service(lane).case_view(lane.case)
    assert view["truncated"] and not any(e["kind"] == "sms_status" for e in view["events"])
    assert view["operations"][0]["status"] == "undelivered"
    assert view["operations"][0]["deliveryError"] == {"code": "30034", "source": "callback"}
    receive(lane, "sms_status", sid, MessageStatus="delivered")
    receive(lane, "sms_status", sid, MessageStatus="failed", ErrorCode="30007")
    op = service(lane).case_view(lane.case)["operations"][0]
    assert op["status"] == "delivered" and op["deliveryError"] is None


@pytest.mark.parametrize("code", ["30034", "999999", None])
def test_submission_rejection_exposes_only_bounded_code_and_never_resends(lane, code):
    calls = []
    def reject(request):
        calls.append(request)
        return HttpResponse(400, json.dumps({"code": code, "message": "PRIVATE-CANARY",
                                            "more_info": "https://private.invalid/PRIVATE-CANARY"}).encode())
    lane.service = service(lane, adapter=TwilioAdapter(lane.adapter.config, transport=reject))
    ready(lane)
    assert lane.service.run_once()[0]["status"] == "rejected"
    op = service(lane).case_view(lane.case)["operations"][0]
    assert op["deliveryError"] == {"code": code or "provider_rejected", "source": "submission"}
    assert "PRIVATE-CANARY" not in json.dumps(op)
    assert lane.service.run_once() == [] and len(calls) == 1


def test_unknown_and_pending_remain_distinct_and_error_does_not_grant_retry(lane):
    ready(lane)
    assert lane.service.case_view(lane.case)["operations"][0]["deliveryError"] is None
    lane.vendor.error = TimeoutError("PRIVATE-CANARY")
    lane.service.run_once()
    op = service(lane).case_view(lane.case)["operations"][0]
    assert op["status"] == "unknown" and not op["cancelable"]
    assert op["deliveryError"] == {"code": "transport_uncertain", "source": "submission"}
    assert lane.service.run_once() == [] and len(lane.vendor.requests) == 1


def test_same_status_callback_without_code_keeps_relevant_submission_code(lane):
    def failed_receipt(request):
        payload = json.loads(lane.vendor(request).body)
        payload.update(status="failed", error_code="30034")
        return HttpResponse(201, json.dumps(payload).encode())
    lane.service = service(lane, adapter=TwilioAdapter(lane.adapter.config, transport=failed_receipt))
    sid = sent(lane)
    receive(lane, "sms_status", sid, MessageStatus="failed")
    op = service(lane).case_view(lane.case)["operations"][0]
    assert op["status"] == "failed"
    assert op["deliveryError"] == {"code": "30034", "source": "submission"}
    receive(lane, "sms_status", sid, MessageStatus="undelivered")
    op = service(lane).case_view(lane.case)["operations"][0]
    assert op["status"] == "undelivered" and op["deliveryError"] is None


def test_old_callback_hash_replays_without_backfill_or_evidence_rewrite(lane):
    sid = sent(lane)
    raw, signature = webhook(lane, "sms_status", sid, MessageStatus="undelivered", ErrorCode="30034", SequenceNumber="1")
    event = lane.adapter.parse_webhook("sms_status", raw, signature)
    event_id, body_hash = _hash(["sms_status", sid, 1]), _hash(asdict(event))
    # This is the old insert shape: hash already includes the code; no column did.
    with psycopg.connect(DSN) as db:
        db.execute("""INSERT INTO chase_communication_provider_events
            (practice_id,account_sid,event_id,provider_sid,kind,channel,sender,recipient,text,status,
             provider_sequence,body_hash) VALUES (%s,%s,%s,%s,'sms_status','sms',%s,%s,'','undelivered',1,%s)""",
            (lane.practice, lane.account, event_id, sid, SENDER, PHONE, body_hash))
        before = db.execute("SELECT to_jsonb(e) FROM chase_communication_provider_events e WHERE practice_id=%s", (lane.practice,)).fetchone()[0]
    assert lane.service.receive_webhook("sms_status", raw, signature)["eventId"] == event_id
    with psycopg.connect(DSN) as db:
        after = db.execute("SELECT to_jsonb(e) FROM chase_communication_provider_events e WHERE practice_id=%s", (lane.practice,)).fetchall()
    assert after == [(before,)] and before["error_code"] is None
    assert service(lane).case_view(lane.case)["operations"][0]["deliveryError"] is None


def test_callback_error_still_rolls_back_with_failed_audit(lane):
    sid = sent(lane)
    def broken(*_):
        raise RuntimeError("synthetic audit unavailable")
    with pytest.raises(RuntimeError):
        service(lane, transaction_audit=broken).receive_webhook("sms_status", *webhook(
            lane, "sms_status", sid, MessageStatus="failed", ErrorCode="30034"))
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_provider_events WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


def test_wrong_route_callback_cannot_attach_error_to_operation(lane):
    sid = sent(lane)
    receive(lane, "sms_status", sid, To="+12025550111", MessageStatus="undelivered", ErrorCode="30034")
    op = service(lane).case_view(lane.case)["operations"][0]
    assert op["status"] == "queued" and op["deliveryError"] is None


def test_error_column_keeps_forced_rls_append_only_and_retain_on_rollback(lane):
    sid = sent(lane)
    receive(lane, "sms_status", sid, MessageStatus="undelivered", ErrorCode="30034")
    with psycopg.connect(lane.dsn) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id','different-practice',true)")
        assert db.execute("SELECT error_code FROM chase_communication_provider_events").fetchall() == []
    for statement in ["UPDATE chase_communication_provider_events SET error_code='30007' WHERE practice_id=%s",
                      "DELETE FROM chase_communication_provider_events WHERE practice_id=%s"]:
        with psycopg.connect(DSN) as db, pytest.raises(psycopg.errors.CheckViolation):
            db.execute(statement, (lane.practice,))
    with psycopg.connect(DSN, autocommit=True) as db, pytest.raises(psycopg.errors.RaiseException, match="retain delivery error evidence"):
        db.execute((Path(__file__).resolve().parents[1] / "migrations/027_communications_delivery_errors.down.sql").read_text(), prepare=False)


@pytest.mark.parametrize("code", ["", "1234567", "30034\n", "private_url", "https://private.invalid"])
def test_callback_error_database_constraint_rejects_non_codes(lane, code):
    with psycopg.connect(DSN) as db, pytest.raises(psycopg.errors.CheckViolation):
        db.execute("""INSERT INTO chase_communication_provider_events
            (practice_id,account_sid,event_id,provider_sid,kind,channel,text,status,body_hash,error_code)
            VALUES (%s,%s,%s,%s,'sms_status','sms','','failed',%s,%s)""",
            (lane.practice, lane.account, _hash(str(uuid4())), "SM" + uuid4().hex, _hash("synthetic"), code))
