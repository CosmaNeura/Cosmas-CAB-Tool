"""Actual durable claim ceiling on a disposable PostgreSQL; only a fake vendor."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest

from test_communications_dispatch import (DSN, lane, schema, foundation_schema,
    ready, seed_case, sms_only_service, psycopg)
from test_chase_communications_postgres import PHONE
from cosmaneura_billing.chase.communications.store import CommunicationsError

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")


def limited(lane):
    service = sms_only_service(lane)
    policy = replace(service.policy, test_mode=True, test_recipients=(PHONE,), test_max_dispatch_starts=1)
    from cosmaneura_billing.chase.communications.dispatch import CommunicationsDispatcher
    service = CommunicationsDispatcher(service.store, policy=policy, sender=service.sender,
        roster=service.roster, adapter=service.adapter)
    service.approve_policy(decision_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor)
    lane.service = service
    return service


def second_case(lane):
    lane.case = "case-b"
    lane.snapshot = seed_case(lane, case=lane.case, observed=datetime.now(timezone.utc).isoformat())
    return ready(lane)["operationId"]


def test_concurrent_cross_case_claim_reserves_one_and_unknown_never_refunds(lane):
    first = limited(lane)
    op_a = ready(lane)["operationId"]
    op_b = second_case(lane)
    other = type(first)(first.store, policy=first.policy, sender=first.sender,
        roster=first.roster, adapter=first.adapter)
    barrier = Barrier(2)
    def claim(service, operation):
        barrier.wait(timeout=5)
        try:
            return operation, service._claim(operation), None
        except CommunicationsError as exc:
            return operation, None, exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, service, op) for service, op in ((first, op_a), (other, op_b))]
        results = [future.result(timeout=10) for future in futures]
    won = [item for item in results if item[1] is not None]
    assert len(won) == 1
    assert [item[2] for item in results if item[1] is None] == ["communications_test_dispatch_ceiling_reached"]
    # Simulate uncertain egress after the permanent start; no provider request
    # is retried by this code and a later job/case cannot reclaim the allowance.
    lane.vendor.error = RuntimeError("synthetic uncertain transport")
    operation, request, _ = won[0]
    assert first._submit_started(operation, request).outcome == "unknown"
    loser = next(item[0] for item in results if item[1] is None)
    assert other.run_once(limit=1) == [{"operationId": loser, "status": "blocked",
        "reason": "communications_test_dispatch_ceiling_reached"}]
    with first.store._db() as db:
        assert first.test_dispatch_allowance(db) == {"maxStarts": 1, "reservedStarts": 1,
            "remainingStarts": 0, "singleSegmentRequired": True}
        assert db.execute("SELECT count(*) AS n FROM chase_communication_dispatch_receipts WHERE practice_id=%s",
            (lane.practice,)).fetchone()["n"] == 1
    assert len(lane.vendor.requests) == 1
    # Evidence-only resolution does not refund even a confirmed-not-sent start.
    first.resolve_unknown(operation, decision="confirmed_not_sent", vendor_reference="synthetic:no-send",
        actor_tenant_id=lane.tenant, actor_id=lane.actor)
    with first.store._db() as db:
        assert first.test_dispatch_allowance(db)["remainingStarts"] == 0
    assert other._claim(operation) is None
    assert len(lane.vendor.requests) == 1


def test_canceled_or_blocked_before_claim_does_not_consume_trial(lane):
    service = limited(lane)
    canceled = ready(lane)["operationId"]
    service.cancel(canceled, actor_tenant_id=lane.tenant, actor_id=lane.actor)
    assert service._claim(canceled) is None
    operation = second_case(lane)
    blocked = type(service)(service.store, policy=replace(service.policy, operationally_activated=False),
        sender=service.sender, roster=service.roster, adapter=service.adapter)
    with pytest.raises(CommunicationsError, match="communications_dispatch_disabled"):
        blocked._claim(operation)
    with service.store._db() as db:
        assert service.test_dispatch_allowance(db)["reservedStarts"] == 0
    assert service._claim(operation) is not None
    assert not lane.vendor.requests


def test_trial_rejects_non_single_segment_before_start(lane):
    service = limited(lane)
    from test_chase_communications_postgres import bind, prepare
    binding = bind(lane)
    service.record_consent(decision_id=str(uuid4()), binding_id=binding, granted=True,
        source_reference="synthetic-consent:1", actor_tenant_id=lane.tenant, actor_id=lane.actor)
    op = prepare(lane, binding, text=lane.policy.templates[0]["text"] + "😀")["operationId"]
    with pytest.raises(CommunicationsError, match="communications_test_message_requires_single_segment"):
        service.authorize(op, actor_tenant_id=lane.tenant, actor_id=lane.actor)
    with service.store._db() as db:
        assert service.test_dispatch_allowance(db)["reservedStarts"] == 0
    assert not lane.vendor.requests
