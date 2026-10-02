"""Local PostgreSQL cancellation safety; no real vendor or contact data."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from test_communications_dispatch import (
    DSN, bind, prepare, ready, service, lane, schema, foundation_schema, psycopg, fails,
)

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")


def cancel(item, op):
    return item.service.cancel(op["operationId"], actor_tenant_id=item.tenant, actor_id=item.actor)


def current(item):
    return item.service.case_view(item.case)["operations"][0]


@pytest.mark.parametrize("authorized", [False, True])
def test_cancel_before_start_is_durable_idempotent_and_never_dispatches(lane, authorized):
    op = ready(lane) if authorized else prepare(lane, bind(lane))
    assert current(lane)["cancelable"] is True
    result = cancel(lane, op)
    assert result == {"operationId": op["operationId"], "status": "canceled", "sent": False}
    assert cancel(lane, op) == result
    view = service(lane).case_view(lane.case)["operations"][0]
    assert view["status"] == "canceled" and view["cancelable"] is False and view["canceledAt"]
    assert view["text"] == op["text"] and view["blockedReason"] is None
    assert service(lane).run_once() == [] and lane.vendor.requests == []
    fails("communications_operation_canceled", lambda: lane.service.authorize(
        op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor))
    with lane.store._db() as db:
        row = db.execute("SELECT * FROM chase_communication_cancellations WHERE practice_id=%s", (lane.practice,)).fetchone()
        assert str(row["actor_id"]) == lane.actor
        assert db.execute("SELECT count(*) AS n FROM chase_communication_cancellations WHERE practice_id=%s", (lane.practice,)).fetchone()["n"] == 1


def test_cancel_held_operation_preserves_check_evidence(lane):
    from dataclasses import replace
    op = ready(lane)
    held = service(lane, policy=replace(lane.policy, operationally_activated=False))
    assert held.run_once()[0]["status"] == "blocked"
    assert current(lane)["blockedReason"] == "communications_dispatch_disabled"
    cancel(lane, op)
    assert current(lane)["status"] == "canceled"
    assert current(lane)["blockedReason"] is None and current(lane)["lastCheckedAt"] is None
    with lane.store._db() as db:
        assert db.execute("SELECT count(*) AS n FROM chase_communication_dispatch_checks WHERE practice_id=%s", (lane.practice,)).fetchone()["n"] == 1


def test_second_active_staff_replay_does_not_replace_original_canceler(lane):
    op = ready(lane)
    cancel(lane, op)
    actor = str(uuid4())
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (lane.tenant, actor))
        db.execute("""INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles)
            VALUES (%s,%s,%s,ARRAY['BillingStaff'])""", (lane.practice, lane.tenant, actor))
    lane.service.cancel(op["operationId"], actor_tenant_id=lane.tenant, actor_id=actor)
    with lane.store._db() as db:
        row = db.execute("SELECT actor_id FROM chase_communication_cancellations WHERE practice_id=%s", (lane.practice,)).fetchone()
        assert str(row["actor_id"]) == lane.actor


@pytest.mark.parametrize("outcome", ["accepted", "unknown", "crash"])
def test_started_dispatch_can_never_be_recalled_even_without_receipt(lane, outcome):
    op = ready(lane)
    if outcome == "unknown":
        lane.vendor.error = TimeoutError()
    elif outcome == "crash":
        lane.vendor.error = KeyboardInterrupt()
    if outcome == "crash":
        with pytest.raises(KeyboardInterrupt):
            lane.service.run_once()
    else:
        lane.service.run_once()
    fails("communications_dispatch_already_started", lambda: cancel(lane, op))
    assert current(lane)["cancelable"] is False and current(lane)["canceledAt"] is None
    assert service(lane).run_once() == [] and len(lane.vendor.requests) == 1


def test_cancel_audit_and_record_commit_together_and_membership_is_current(lane):
    op = ready(lane)
    def reject(db, action, case_id, operation_id):
        assert action == "communications_cancel" and case_id == lane.case and operation_id == op["operationId"]
        raise RuntimeError("synthetic audit failure")
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        service(lane, transaction_audit=reject).cancel(op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    assert current(lane)["cancelable"] is True
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s", (lane.practice,))
    fails("communications_staff_required", lambda: cancel(lane, op))
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET active=true WHERE practice_id=%s", (lane.practice,))
    assert cancel(lane, op)["status"] == "canceled"


@pytest.mark.parametrize("_", range(3))
def test_cancel_racing_dispatch_has_only_one_outcome(lane, _):
    from cosmaneura_billing.chase.communications.store import CommunicationsError
    op = ready(lane)
    start = Barrier(2)
    def staff():
        start.wait()
        try:
            return cancel(lane, op)["status"]
        except CommunicationsError as exc:
            return exc.code
    def worker():
        start.wait()
        return service(lane).run_once()
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_result, dispatch_result = pool.submit(staff), pool.submit(worker)
        result, _ = cancel_result.result(), dispatch_result.result()
    view = current(lane)
    if result == "canceled":
        assert view["status"] == "canceled" and lane.vendor.requests == []
    else:
        assert result == "communications_dispatch_already_started"
        assert view["status"] == "queued" and len(lane.vendor.requests) == 1
    assert service(lane).run_once() == []


def raw_write(lane, op, kind):
    """Bypass the application advisory lock to exercise the SQL case lock."""
    with psycopg.connect(lane.dsn) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (lane.practice,))
        if kind == "cancel":
            db.execute("""INSERT INTO chase_communication_cancellations
                (practice_id,operation_id,actor_tenant_id,actor_id) VALUES (%s,%s,%s,%s)""",
                (lane.practice, op["operationId"], lane.tenant, lane.actor))
        else:
            db.execute("INSERT INTO chase_communication_dispatch_starts(practice_id,operation_id) VALUES (%s,%s)",
                       (lane.practice, op["operationId"]))


@pytest.mark.parametrize("first", ["cancel", "start"])
def test_database_rejects_conflicting_ledger_writes_without_application_lock(lane, first):
    op = ready(lane)
    raw_write(lane, op, first)
    with pytest.raises(psycopg.errors.CheckViolation):
        raw_write(lane, op, "start" if first == "cancel" else "cancel")


def test_database_serializes_direct_concurrent_cancellation_and_start(lane):
    op = ready(lane)
    barrier = Barrier(2)
    def write(kind):
        barrier.wait()
        try:
            raw_write(lane, op, kind)
            return "saved"
        except psycopg.errors.CheckViolation:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ["start", "cancel"])) == ["conflict", "saved"]
    assert lane.vendor.requests == []


def test_cancellation_is_tenant_scoped_and_immutable(lane):
    op = ready(lane)
    cancel(lane, op)
    with psycopg.connect(lane.dsn) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", ("other-synthetic-practice",))
        assert db.execute("SELECT count(*) FROM chase_communication_cancellations").fetchone()[0] == 0
    for query in ("UPDATE chase_communication_cancellations SET actor_id=actor_id",
                  "DELETE FROM chase_communication_cancellations",
                  "TRUNCATE chase_communication_cancellations"):
        with pytest.raises(psycopg.Error):
            with psycopg.connect(DSN) as db:
                db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (lane.practice,))
                db.execute(query)
    with pytest.raises(psycopg.Error, match="leave expand rung dormant"):
        with psycopg.connect(DSN) as db:
            db.execute((Path(__file__).resolve().parents[1] / "migrations/015_chase_communications_cancellation.down.sql").read_text(), prepare=False)
