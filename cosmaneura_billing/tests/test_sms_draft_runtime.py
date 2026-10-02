"""Focused real local PostgreSQL API/replica proof, with a fake vendor only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient

from cosmaneura_billing import api
from cosmaneura_billing.chase.communications import runtime
from test_communications_runtime import DSN, lane, post, schema

pytestmark = pytest.mark.skipif(not DSN, reason="disposable runtime PostgreSQL is not configured")


@pytest.mark.parametrize("paused", ["activation", "quiet_hours"])
def test_sms_draft_survives_pause_and_replica_without_authority_or_transport(lane, monkeypatch, tmp_path, paused):
    original_policy = lane.policy
    offset = [1 if paused == "quiet_hours" else 0]
    build = runtime.dispatcher_from_settings
    def dispatcher(*args, **kwargs):
        result = build(*args, **kwargs)
        result.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=offset[0])
        return result
    monkeypatch.setattr(runtime, "dispatcher_from_settings", dispatcher)
    if paused == "activation": lane.policy = replace(original_policy, operationally_activated=False)
    lane.worker = dispatcher(lane.config, dsn=lane.worker_dsn, adapter=lane.worker.adapter)
    with TestClient(lane.app) as client:
        view = post(client, lane).json()
        assert client.post("/api/chase/communications/policy/decision", headers=lane.headers, json={
            "actionId": str(uuid4()), "approved": True, "expectedPolicyHash": view["policy"]["hash"]}).status_code == 200
        bound = post(client, lane, "/bind", channel="sms", contactSnapshotId=lane.snapshot, confirmedContact=True)
        binding = bound.json()["bindings"]["sms"]["bindingId"]
        consent = post(client, lane, "/consent", actionId=str(uuid4()), bindingId=binding,
            granted=True, sourceReference="synthetic-reviewed-permission", confirmed=True)
        reason = "communications_dispatch_disabled" if paused == "activation" else "communications_quiet_hours"
        assert consent.json()["actions"]["sms"] == {"allowed": True, "reason": "",
            "authorization": {"allowed": False, "reason": reason}}
        body = {"actionId": str(uuid4()), "channel": "sms", "text": lane.policy.templates[0]["text"]}
        first = post(client, lane, "/action", **body)
        assert first.status_code == 200, first.text
        op = first.json()["operations"][0]
        assert op["status"] == "prepared" and op["providerSid"] is None
        assert post(client, lane, "/action", **body).json()["operations"] == first.json()["operations"]
        assert post(client, lane, "/action", **{**body, "text": lane.policy.templates[1]["text"]}).status_code == 409
        authorization = post(client, lane, "/authorize", operationId=body["actionId"], confirmed=True)
        assert authorization.status_code in (403, 409) and authorization.json()["detail"] == reason
        assert lane.worker.run_once() == [] and lane.vendor.requests == []
        replica = api.create_app(settings=lane.config, frontend_dist=tmp_path / "missing-replica")
        with TestClient(replica) as second:
            assert post(second, lane).json()["operations"] == first.json()["operations"]
        with psycopg.connect(DSN) as db:
            for table in ("chase_communication_authorizations", "chase_communication_dispatch_starts",
                          "chase_communication_dispatch_receipts", "chase_attempts", "chase_delivery_outbox"):
                assert db.execute(sql.SQL("SELECT count(*) FROM {} WHERE practice_id=%s").format(sql.Identifier(table)),
                    (lane.practice,)).fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM chase_communication_operations WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM chase_communication_events WHERE practice_id=%s AND kind='outbound_prepared'", (lane.practice,)).fetchone()[0] == 1
            assert db.execute("""SELECT count(*) FROM access_audit_completions c JOIN access_audit_attempts a USING(practice_id,event_id)
                WHERE c.practice_id=%s AND a.route='/api/chase/communications/action' AND c.outcome='allowed'""", (lane.practice,)).fetchone()[0] == 2
            db.execute(sql.SQL("REVOKE INSERT ON access_audit_completions FROM {}").format(sql.Identifier(lane.role)))
        try:
            failed = post(client, lane, "/action", **{**body, "actionId": str(uuid4())})
            assert failed.status_code == 503
        finally:
            with psycopg.connect(DSN) as db:
                db.execute(sql.SQL("GRANT INSERT ON access_audit_completions TO {}").format(sql.Identifier(lane.role)))
        with psycopg.connect(DSN) as db:
            assert db.execute("SELECT count(*) FROM chase_communication_operations WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 1
        # Becoming available does not authorize or send the saved preparation.
        lane.policy = original_policy
        offset[0] = 2 if paused == "quiet_hours" else 0
        lane.worker = dispatcher(lane.config, dsn=lane.worker_dsn, adapter=lane.worker.adapter)
        assert lane.worker.run_once() == []
        assert post(client, lane, "/authorize", operationId=body["actionId"], confirmed=True).status_code == 200
        assert lane.worker.run_once()[0]["status"] == "accepted"
        assert lane.worker.run_once() == [] and len(lane.vendor.requests) == 1
