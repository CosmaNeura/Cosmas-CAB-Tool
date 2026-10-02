"""Approved-recipient boundaries without the deferred API/runtime configuration."""
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

import pytest

from test_communications_dispatch import (
    DSN, PHONE, lane, schema, foundation_schema, ready, service, sms_only_service, fails,
)
from cosmaneura_billing.chase.communications.dispatch import DispatchPolicy
from cosmaneura_billing.chase.communications.twilio import TwilioError

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")
OTHER = "+12025550101"


def scoped(item, recipients=(PHONE,)):
    current = sms_only_service(item)
    item.store, item.adapter = current.store, current.adapter
    item.policy = replace(current.policy, test_mode=True, test_recipients=recipients)
    item.service = service(item)
    item.service.approve_policy(decision_id=str(uuid4()), actor_tenant_id=item.tenant, actor_id=item.actor)


@pytest.mark.parametrize("recipients,reason", [
    ((), "communications_test_recipients_unconfigured"),
    ((OTHER,), "communications_test_recipient_not_allowed"),
])
@pytest.mark.parametrize("phase", ["authorize", "dispatch"])
def test_scope_denies_before_authorization_or_start(lane, recipients, reason, phase):
    scoped(lane, (PHONE,) if phase == "dispatch" else recipients)
    if phase == "dispatch":
        ready(lane)
        lane.service = service(lane, policy=replace(lane.policy, test_recipients=recipients))
        lane.service.approve_policy(decision_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor)
        assert lane.service.run_once()[0]["reason"] == reason
    else:
        fails(reason, lambda: ready(lane))
    with lane.store._db() as db:
        for table in ("dispatch_starts", "dispatch_receipts"):
            assert db.execute("SELECT count(*) AS n FROM chase_communication_" + table).fetchone()["n"] == 0
    assert lane.vendor.requests == []


def test_allowed_recipient_sends_once_and_scope_change_invalidates_approval(lane):
    scoped(lane)
    op = ready(lane)["operationId"]
    changed = service(lane, policy=replace(lane.policy, test_recipients=(PHONE, OTHER)))
    assert changed.policy_hash != lane.service.policy_hash
    assert changed.run_once()[0]["reason"] == "communications_policy_approval_required"
    assert lane.vendor.requests == []
    assert lane.service.run_once() == [{"operationId": op, "status": "accepted"}]
    assert parse_qs(lane.vendor.requests[0].body.decode())["To"] == [PHONE]
    assert lane.service.run_once() == [] and len(lane.vendor.requests) == 1


def test_claimed_start_cannot_bypass_final_scope_or_forge_body_destination(lane):
    scoped(lane)
    op = ready(lane)["operationId"]
    request = lane.service._claim(op)
    changed = service(lane, policy=replace(lane.policy, test_recipients=(OTHER,)))
    fails("communications_test_recipient_not_allowed", lambda: changed._submit_started(op, request))
    pairs = parse_qs(request.body.decode())
    pairs["To"] = [OTHER]
    with pytest.raises(TwilioError):
        lane.service._submit_started(op, replace(request, body=urlencode(pairs, doseq=True).encode()))
    with lane.store._db() as db:
        assert db.execute("SELECT count(*) AS n FROM chase_communication_dispatch_starts").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM chase_communication_dispatch_receipts").fetchone()["n"] == 0
    assert lane.service.run_once() == [] and lane.vendor.requests == []


@pytest.mark.parametrize("mode,recipients", [
    (1, (PHONE,)), (True, [PHONE]), (True, (42,)), (False, (PHONE,)),
    (True, (PHONE, PHONE)), (True, tuple(f"+1202555{i:04}" for i in range(21))),
])
def test_direct_policy_rejects_malformed_or_unapproved_test_scope(mode, recipients):
    with pytest.raises(ValueError):
        DispatchPolicy("Synthetic Practice", "UTC", test_mode=mode, test_recipients=recipients)

@pytest.mark.parametrize("hours", [-25, 1])
def test_default_clock_uses_database_and_rejects_stale_or_future_approval(lane, hours):
    op = ready(lane)["operationId"]
    assert lane.service.clock is None
    with lane.store._db() as db:
        before = db.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        observed = lane.service._now(db)
        after = db.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
        assert before <= observed <= after
        authorization = dict(db.execute("SELECT * FROM chase_communication_authorizations WHERE operation_id=%s", (op,)).fetchone())
        authorization["created_at"] = after + timedelta(hours=hours)
        fails("communications_approval_expired", lambda: lane.service._gates(
            db, lane.service._operation(db, op), phase="dispatch", authorization=authorization))
    assert lane.vendor.requests == []
