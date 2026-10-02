"""Real staff API -> restricted worker -> signed callback -> replica read.

All contacts are synthetic and the only outbound transport is FakeVendor.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from cosmaneura_billing import api
from cosmaneura_billing.chase.communications import runtime, webhooks
from cosmaneura_billing.chase.communications.twilio import TwilioAdapter, TwilioConfig
from cosmaneura_billing.migrations import _bootstrap_practice_admin, _provision_runtime_role, _provision_chase_worker_role
from cosmaneura_billing.persistence import PostgresAccessAuditStore
from cosmaneura_billing.settings import Settings
from test_chase_communications_postgres import seed_case, PHONE, SENDER
from test_communications_dispatch import FakeVendor, webhook
from test_security import _auth_headers

DSN = os.environ.get("COSMANEURA_RUNTIME_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="disposable runtime PostgreSQL is not configured")


@pytest.fixture(scope="module", autouse=True)
def schema():
    if not DSN:
        return
    from cosmaneura_billing.migrations import apply_migrations
    with psycopg.connect(DSN) as db:
        assert "test" in db.info.dbname
    # Validate checksums and apply the actual candidate sparse chain (including
    # pgvector), rather than using an unrelated Athena table as a schema marker.
    apply_migrations(Settings(database_url=DSN, cpt_auto_fetch=False))


@pytest.fixture
def lane(tmp_path, monkeypatch):
    suffix = uuid4().hex[:18]
    lane = SimpleNamespace(practice="runtime-" + suffix, tenant=str(uuid4()), actor=str(uuid4()),
        role="rt_api_" + suffix, worker_role="rt_worker_" + suffix,
        case="synthetic-case", account="AC" + uuid4().hex)
    connection = conninfo_to_dict(DSN)
    def uri(role):
        return (f"postgresql://{role}@{quote(connection.get('host', 'localhost'), safe='')}:"
                f"{connection.get('port', '5432')}/{quote(connection['dbname'], safe='')}")
    lane.dsn = uri(lane.role)
    lane.worker_dsn = uri(lane.worker_role)
    roster = tmp_path / "roster.json"
    roster.write_text("[]")
    lane.config = Settings(cpt_auto_fetch=False, environment="test", auth_mode="container_apps", entra_tenant_id=lane.tenant,
        practice_id=lane.practice, practice_name="Synthetic Practice", practice_timezone="UTC",
        state_store="postgres", chase_store="postgres", database_url=lane.dsn,
        database_runtime_role=lane.role, chase_worker_database_role=lane.worker_role,
        chase_worker_database_url=lane.worker_dsn, direct_contract_list=str(roster),
        communications_enabled=True, chase_transport="twilio", twilio_account_sid=lane.account,
        communications_enabled_channels=("sms", "automated_voice"),
        twilio_from_number=SENDER, twilio_callback_base_url="https://synthetic.invalid",
        review_context_signing_key="synthetic-only-review-signing-key-12345",
        feedback_path=str(tmp_path / "feedback.jsonl"))
    provision = replace(lane.config, database_runtime_password="synthetic-only-password",
        chase_worker_database_password="synthetic-only-password", bootstrap_admin_oid=lane.actor)
    with psycopg.connect(DSN) as db:
        _bootstrap_practice_admin(db, provision)
        _provision_runtime_role(db, provision)
        _provision_chase_worker_role(db, provision)
    monkeypatch.setattr("test_chase_communications_postgres.DSN", DSN)
    lane.snapshot = seed_case(lane, case=lane.case, observed=datetime.now(timezone.utc).isoformat(), consent="unknown")
    # Enable only the test policy; Settings remains dark. No real transport is
    # constructed, even if the developer shell contains vendor credentials.
    base_policy = runtime.dispatch_policy(lane.config)
    hour = datetime.now(timezone.utc).hour
    lane.policy = replace(base_policy, quiet_hours=((hour + 1) % 24, (hour + 2) % 24),
        scheduler_enabled=True, patient_messaging_enabled=True, operationally_activated=True)
    monkeypatch.setattr(runtime, "dispatch_policy", lambda config: lane.policy)
    lane.headers = _auth_headers(user_id=lane.actor, tenant_id=lane.tenant)
    lane.sender = runtime.public_sender(lane.config)
    lane.vendor = FakeVendor()
    lane.vendor.account = lane.account
    twilio = TwilioConfig(**asdict(lane.sender), auth_token="synthetic-test-secret",
                          enabled_channels=lane.config.communications_enabled_channels)
    lane.worker = runtime.dispatcher_from_settings(lane.config, dsn=lane.worker_dsn,
        adapter=TwilioAdapter(twilio, transport=lane.vendor))
    lane.callbacks = webhooks.create_app(runtime.dispatcher_from_settings(lane.config,
        dsn=lane.worker_dsn, adapter=TwilioAdapter(twilio)))
    lane.app = api.create_app(settings=lane.config, frontend_dist=tmp_path / "missing")
    return lane


def post(client, lane, path="", **body):
    return client.post("/api/chase/communications" + path,
        json={"chaseId": lane.case, **body}, headers=lane.headers)


def prepare(client, lane, channel="sms"):
    view = post(client, lane).json()
    policy = client.post("/api/chase/communications/policy/decision", headers=lane.headers,
        json={"actionId": str(uuid4()), "approved": True, "expectedPolicyHash": view["policy"]["hash"]})
    assert policy.status_code == 200, policy.text
    bound = post(client, lane, "/bind", channel=channel, contactSnapshotId=lane.snapshot, confirmedContact=True)
    assert bound.status_code == 200, bound.text
    binding = bound.json()["bindings"][channel]["bindingId"]
    consent = post(client, lane, "/consent", actionId=str(uuid4()), bindingId=binding,
        granted=True, sourceReference="synthetic-consent:reviewed", confirmed=True)
    assert consent.status_code == 200, consent.text
    op = str(uuid4())
    body = {"actionId": op, "channel": channel}
    if channel == "sms": body["text"] = lane.policy.templates[0]["text"]
    prepared = post(client, lane, "/action", **body)
    assert prepared.status_code == 200, prepared.text
    assert next(row for row in prepared.json()["operations"] if row["operationId"] == op)["status"] == "prepared"
    assert lane.vendor.requests == []
    approved = post(client, lane, "/authorize", operationId=op, confirmed=True)
    assert approved.status_code == 200, approved.text
    assert next(row for row in approved.json()["operations"] if row["operationId"] == op)["status"] == "authorized"
    return op


def callback(client, lane, kind, sid=None, **params):
    raw, signature = webhook(lane, kind, sid, **params)
    return client.post(runtime.CALLBACK_PATHS[kind], content=raw,
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature})


def test_texting_only_api_blocks_old_calls_at_each_mutation_boundary(lane, tmp_path):
    with TestClient(lane.app) as client:
        operation = prepare(client, lane, "automated_voice")
        binding = post(client, lane).json()["bindings"]["automated_voice"]["bindingId"]
    lane.config = replace(lane.config, communications_enabled_channels=("sms",))
    lane.policy = replace(lane.policy, enabled_channels=("sms",))
    sender = runtime.public_sender(lane.config)
    worker = runtime.dispatcher_from_settings(lane.config, dsn=lane.worker_dsn,
        adapter=TwilioAdapter(TwilioConfig(**asdict(sender), auth_token="synthetic-test-secret"), transport=lane.vendor))
    app = api.create_app(settings=lane.config, frontend_dist=tmp_path / "missing")
    with TestClient(app) as client:
        view = post(client, lane).json()
        assert view["enabledChannels"] == ["sms"] and view["operations"][0]["channel"] == "automated_voice"
        assert view["actions"]["automatedVoice"] == {"allowed": False, "reason": "communications_channel_disabled"}
        for path, body in (("/bind", {"channel": "automated_voice", "contactSnapshotId": lane.snapshot, "confirmedContact": True}),
                ("/action", {"actionId": str(uuid4()), "channel": "automated_voice"}),
                ("/authorize", {"operationId": operation, "confirmed": True}),
                ("/consent", {"actionId": str(uuid4()), "bindingId": binding, "granted": True,
                               "sourceReference": "synthetic-consent", "confirmed": True})):
            response = post(client, lane, path, **body)
            assert response.status_code == 403 and response.json()["detail"] == "communications_channel_disabled"
        assert worker.run_once()[0]["reason"] == "communications_channel_disabled"
        assert lane.vendor.requests == []
        assert post(client, lane, "/cancel", operationId=operation, confirmed=True).status_code == 200
        prepare(client, lane, "sms")
        assert worker.run_once()[0]["status"] == "accepted"
        assert [request.channel for request in lane.vendor.requests] == ["sms"]


@pytest.mark.parametrize("channel", ["sms", "automated_voice"])
def test_staff_to_worker_to_callback_survives_new_api_replica(lane, tmp_path, channel):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        assert client.get("/api/ready", headers=lane.headers).status_code == 200
        assert callbacks.get("/ready").status_code == 200
        operation = prepare(client, lane, channel)
        assert lane.worker.run_once()[0]["status"] == "accepted"
        view = post(client, lane).json()
        sid = view["operations"][0]["providerSid"]
        if channel == "sms":
            assert callback(callbacks, lane, "sms_status", sid).status_code == 200
            inbound = "SM" + uuid4().hex
            assert callback(callbacks, lane, "sms_inbound", inbound).status_code == 200
            assert callback(callbacks, lane, "sms_inbound", inbound).status_code == 200
        else:
            assert callback(callbacks, lane, "voice_gather", sid, Digits="1", CallStatus="in-progress").status_code == 200
            assert callback(callbacks, lane, "voice_status", sid).status_code == 200
    replica = api.create_app(settings=lane.config, frontend_dist=tmp_path / "missing")
    with TestClient(replica) as client:
        view = post(client, lane).json()
        assert view["operations"][0]["operationId"] == operation
        assert view["operations"][0]["status"] == ("delivered" if channel == "sms" else "completed")
        if channel == "sms": assert sum(e["kind"] == "sms_inbound" for e in view["events"]) == 1
        else: assert any(e["disposition"] == "callback_requested" for e in view["events"])
        assert lane.worker.run_once() == [] and len(lane.vendor.requests) == 1
    with psycopg.connect(DSN) as db:
        assert db.execute("""SELECT count(*) FROM access_audit_completions c
            JOIN access_audit_attempts a USING(practice_id,event_id)
            WHERE c.practice_id=%s AND a.action='chase.decide' AND c.outcome='allowed'""", (lane.practice,)).fetchone()[0] >= 4
        assert db.execute("SELECT state FROM chase_items WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == "needs_review"


def test_signed_stop_is_durable_before_ack_and_not_reactivated_by_start(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        prepare(client, lane)
        response = callback(callbacks, lane, "sms_inbound", Body="STOP")
        assert response.status_code == 200 and "xml" in response.headers["content-type"]
        assert callback(callbacks, lane, "sms_inbound", Body="START").status_code == 200
        assert lane.worker.run_once()[0]["reason"] == "communications_contact_suppressed"
        assert post(client, lane).json()["suppression"]["sms"] is True
        assert lane.vendor.requests == []


def test_staff_can_cancel_before_dispatch_but_cannot_recall_a_started_send(lane):
    with TestClient(lane.app) as client:
        operation = prepare(client, lane)
        canceled = post(client, lane, "/cancel", operationId=operation, confirmed=True)
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["operations"][0]["status"] == "canceled"
        assert canceled.json()["operations"][0]["cancelable"] is False
        assert post(client, lane, "/cancel", operationId=operation, confirmed=True).status_code == 200
        assert lane.worker.run_once() == [] and lane.vendor.requests == []
        other = prepare(client, lane, "automated_voice")
        assert lane.worker.run_once()[0]["status"] == "accepted"
        late = post(client, lane, "/cancel", operationId=other, confirmed=True)
        assert late.status_code == 409
        assert len(lane.vendor.requests) == 1
    with psycopg.connect(DSN) as db:
        assert db.execute("""SELECT count(*) FROM access_audit_completions c
            JOIN access_audit_attempts a USING(practice_id,event_id)
            WHERE c.practice_id=%s AND a.route='/api/chase/communications/cancel'
            AND a.action='chase.decide' AND c.outcome='allowed'""", (lane.practice,)).fetchone()[0] == 2


def test_audit_failure_rolls_back_contact_binding(lane, monkeypatch):
    def fail(*args, **kwargs):
        raise psycopg.errors.CheckViolation("synthetic audit failure")
    monkeypatch.setattr(PostgresAccessAuditStore, "_insert_completion", fail)
    with TestClient(lane.app, raise_server_exceptions=False) as client:
        response = post(client, lane, "/bind", channel="sms", contactSnapshotId=lane.snapshot, confirmedContact=True)
        assert response.status_code == 503
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_bindings WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


def test_role_case_confirmation_and_default_off_boundaries(lane):
    with TestClient(lane.app) as client:
        missing = post(client, lane, chaseId="not-this-case")
        assert missing.status_code == 404
        assert post(client, lane, "/bind", channel="sms", contactSnapshotId=lane.snapshot, confirmedContact=False).status_code == 422
        with psycopg.connect(DSN) as db:
            db.execute("UPDATE practice_memberships SET roles=ARRAY['Provider'] WHERE practice_id=%s", (lane.practice,))
        assert post(client, lane).status_code == 403
    assert lane.vendor.requests == []


@pytest.mark.parametrize("mutation,status", [("signature", 403), ("duplicate", 403), ("type", 400), ("query", 400), ("oversize", 413)])
def test_callback_boundary_rejects_before_storage(lane, mutation, status):
    raw, signature = webhook(lane, "sms_inbound", Body="STOP")
    headers = [("Content-Type", "application/x-www-form-urlencoded"), ("X-Twilio-Signature", signature)]
    path = runtime.CALLBACK_PATHS["sms_inbound"]
    if mutation == "signature": headers[-1] = ("X-Twilio-Signature", "invalid")
    elif mutation == "duplicate": headers.append(("X-Twilio-Signature", signature))
    elif mutation == "type": headers[0] = ("Content-Type", "application/json")
    elif mutation == "query": path += "?unexpected=true"
    else: raw = b"x" * 65537
    with TestClient(lane.callbacks) as client:
        response = client.post(path, content=raw, headers=headers)
        assert response.status_code == status
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_provider_events WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


@pytest.mark.parametrize("privilege,relation", [("INSERT", "chase_communication_provider_events"),
    ("USAGE", "SEQUENCE chase_communication_provider_events_sequence_no_seq")])
def test_callback_readiness_requires_worker_insert_and_sequence_permission(lane, privilege, relation):
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL(f"REVOKE {privilege} ON {relation} FROM {{}}").format(sql.Identifier(lane.worker_role)))
    with TestClient(lane.callbacks) as client:
        assert client.get("/live").status_code == 200
        assert client.get("/ready").status_code == 503


def test_slow_callback_body_is_bounded_and_never_enters_storage(lane, monkeypatch):
    import asyncio
    import httpx
    from cosmaneura_billing.chase.communications.dispatch import CommunicationsDispatcher
    completed = []
    monkeypatch.setattr(webhooks, "BODY_DEADLINE_SECONDS", 0.02)
    monkeypatch.setattr(CommunicationsDispatcher, "receive_webhook", lambda *args: pytest.fail("body not complete"))
    async def body():
        try:
            yield b"Body=S"
            await asyncio.sleep(60)
        finally:
            completed.append(True)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lane.callbacks), base_url="http://callback.test") as client:
            return await client.post(runtime.CALLBACK_PATHS["sms_inbound"], content=body(),
                headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "irrelevant"})
    result = asyncio.run(run())
    assert result.status_code == 408 and completed == [True]
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_provider_events WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


def test_actual_staff_api_json_matches_frontend_contract(lane, monkeypatch):
    import json
    import shutil
    import subprocess
    import sys
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required only for the cross-language DTO check")
    responses = []
    original = post
    def record(*args, **kwargs):
        response = original(*args, **kwargs)
        if response.status_code == 200:
            responses.append(response.json())
        return response
    monkeypatch.setattr(sys.modules[__name__], "post", record)
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        prepare(client, lane)
        lane.worker.run_once()
        sid = post(client, lane).json()["operations"][0]["providerSid"]
        assert callback(callbacks, lane, "sms_status", sid).status_code == 200
        post(client, lane)
        assert callback(callbacks, lane, "sms_inbound", Body="STOP").status_code == 200
        post(client, lane)
    bridge = subprocess.run([node, "--input-type=module", "-e", """
        import {readCommunications} from './src/caseCommunications.js';
        import {readFileSync} from 'node:fs';
        const {responses,caseId}=JSON.parse(readFileSync(0,'utf8'));
        for (const response of responses) {
            const projected=readCommunications(response,caseId);
            if (!projected || projected.chaseId!==caseId) throw Error('DTO rejected');
        }
        process.stdout.write(String(responses.length));
        """], input=json.dumps({"responses": responses, "caseId": lane.case}), text=True,
        capture_output=True, timeout=10, cwd=Path(__file__).resolve().parents[1] / "frontend")
    assert bridge.returncode == 0, bridge.stderr
    assert int(bridge.stdout) >= 8
@pytest.mark.parametrize("decision", ["confirmed_not_sent", "confirmed_attempted"])
def test_actual_api_unknown_investigation_is_audited_and_not_a_resend(lane, tmp_path, decision):
    import json
    import shutil
    import subprocess
    views = []
    with TestClient(lane.app) as client:
        operation = prepare(client, lane)
        lane.vendor.error = TimeoutError()
        assert lane.worker.run_once()[0]["status"] == "unknown"
        view = post(client, lane).json()
        views.append(view)
        assert view["actions"]["sms"] == {"allowed": False, "reason": "communications_unknown_requires_resolution",
            "authorization": {"allowed": False, "reason": "communications_unknown_requires_resolution"}}
        assert view["operations"][0]["canResolveUnknown"] is True
        next_id = str(uuid4())
        next_draft = dict(actionId=next_id, channel="sms", text=lane.policy.templates[0]["text"])
        assert post(client, lane, "/action", **next_draft).status_code == 409
        body = dict(operationId=operation, decision=decision, vendorReference="synthetic-vendor:investigation-1")
        assert post(client, lane, "/resolve-unknown", **body, confirmed=False).status_code == 422
        response = post(client, lane, "/resolve-unknown", **body, confirmed=True)
        assert response.status_code == 200, response.text
        views.append(response.json())
        investigated = response.json()["operations"][0]
        assert investigated["status"] == "unknown" and investigated["canResolveUnknown"] is False
        assert investigated["unknownResolution"]["decision"] == decision
        assert post(client, lane, "/resolve-unknown", **body, confirmed=True).status_code == 200
        assert post(client, lane, "/resolve-unknown", **{**body, "decision": "confirmed_delivered"}, confirmed=True).status_code == 409
        assert post(client, lane, "/action", **next_draft).status_code == 200
        assert len(lane.vendor.requests) == 1
    replica = api.create_app(settings=lane.config, frontend_dist=tmp_path / "missing")
    with TestClient(replica) as client:
        history = post(client, lane).json()
        assert next(o for o in history["operations"] if o["operationId"] == operation)["unknownResolution"]
        assert lane.worker.run_once() == []  # New draft has not been authorized.
    with psycopg.connect(DSN) as db:
        assert db.execute("""SELECT count(*) FROM access_audit_completions c
            JOIN access_audit_attempts a USING(practice_id,event_id)
            WHERE c.practice_id=%s AND a.route='/api/chase/communications/resolve-unknown'
              AND a.action='chase.decide' AND c.outcome='allowed'""", (lane.practice,)).fetchone()[0] == 2
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run([node, "--input-type=module", "-e", """
        import {readCommunications} from './src/caseCommunications.js';
        import {readFileSync} from 'node:fs';
        const views=JSON.parse(readFileSync(0,'utf8'));
        for (const view of views) {
            const op=readCommunications(view,view.chaseId).operations[0];
            if (op.status!=='unknown') throw Error('Original uncertainty was lost');
            if (!op.canResolveUnknown && !op.unknownResolution) throw Error('Resolution was lost');
        }
        """], input=json.dumps(views), text=True, capture_output=True, timeout=10,
        cwd=Path(__file__).resolve().parents[1] / "frontend")
    assert result.returncode == 0, result.stderr


def test_actual_event_review_api_and_worklist_counts_match_frontend_contract(lane):
    import json
    import subprocess
    views = []
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        prepare(client, lane)
        assert callback(callbacks, lane, "sms_inbound", Body="Synthetic staff callback\x7f requested").status_code == 200
        pending = post(client, lane, "/review-events")
        assert pending.status_code == 200, pending.text
        views.append(pending.json())
        item = pending.json()["items"][0]
        assert pending.json()["totalPending"] == 1
        queue = client.post("/api/chase/worklist", headers=lane.headers, json={})
        assert queue.status_code == 200, queue.text
        row = next(r for r in queue.json()["rows"] if r["chaseId"] == lane.case)
        assert row["communicationReview"] == {"pending": 1, "held": 0}
        assert item["text"] not in queue.text and PHONE not in queue.text
        payload = {"actionId": str(uuid4()), "eventRef": item["ref"], "sourceHash": item["sourceHash"],
                   "decision": "handled", "confirmed": False, "contactSnapshotId": None}
        assert post(client, lane, "/review-event", **payload).status_code == 422
        payload["confirmed"] = True
        done = post(client, lane, "/review-event", **payload)
        assert done.status_code == 200, done.text
        views.append(done.json())
        assert done.json()["totalPending"] == 0
        assert post(client, lane, "/review-event", **payload).status_code == 200
        history = post(client, lane).json()
        assert all(not e["needsStaffReview"] for e in history["events"])
        assert len(history["events"]) == 2  # preparation plus immutable reply
        queue = client.post("/api/chase/worklist", headers=lane.headers, json={}).json()
        assert queue["rows"][0]["communicationReview"]["pending"] == 0
        assert lane.vendor.requests == []
    with psycopg.connect(DSN) as db:
        assert db.execute("""SELECT count(*) FROM access_audit_completions c
            JOIN access_audit_attempts a USING(practice_id,event_id)
            WHERE c.practice_id=%s AND a.route='/api/chase/communications/review-event'
              AND a.action='chase.decide' AND c.outcome='allowed'""", (lane.practice,)).fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM chase_communication_event_reviews WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 1
    result = subprocess.run(["node", "--input-type=module", "-e", """
        import {readEventReview} from './src/communicationsEventReview.js';
        let input=''; for await (const chunk of process.stdin) input+=chunk;
        for (const view of JSON.parse(input)) readEventReview(view,view.chaseId);
        """], input=json.dumps(views), text=True, capture_output=True, timeout=10,
        cwd=Path(__file__).resolve().parents[1] / "frontend")
    assert result.returncode == 0, result.stderr


def test_event_review_api_cannot_clear_stop_and_worker_cannot_write_staff_decisions(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        prepare(client, lane)
        assert callback(callbacks, lane, "sms_inbound", Body="STOP").status_code == 200
        item = post(client, lane, "/review-events").json()["items"][0]
        assert post(client, lane, "/review-event", actionId=str(uuid4()), eventRef=item["ref"],
            sourceHash=item["sourceHash"], decision="handled", confirmed=True).status_code == 200
        assert post(client, lane).json()["suppression"]["sms"] is True
        assert lane.worker.run_once()[0]["reason"] == "communications_contact_suppressed"
    with psycopg.connect(lane.worker_dsn) as db:
        assert db.execute("SELECT has_table_privilege(current_user,'chase_communication_event_reviews','SELECT')").fetchone()[0] is True
        assert db.execute("SELECT has_table_privilege(current_user,'chase_communication_event_reviews','INSERT')").fetchone()[0] is False


def test_actual_held_reply_attribution_and_handle_contract(lane):
    import json
    import subprocess
    second = seed_case(lane, case="case-b", observed=datetime.now(timezone.utc).isoformat())
    views = []
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        for case, snapshot in [(lane.case,lane.snapshot),("case-b",second)]:
            response = post(client, lane, "/bind", chaseId=case, channel="sms", contactSnapshotId=snapshot, confirmedContact=True)
            assert response.status_code == 200, response.text
        assert callback(callbacks, lane, "sms_inbound", Body="Synthetic ambiguous reply").status_code == 200
        held = post(client, lane, "/review-events").json()
        views.append(held)
        assert held["heldPending"] == 1 and held["items"][0]["canAttribute"]
        item = held["items"][0]
        body = {"actionId": str(uuid4()), "eventRef": item["ref"], "sourceHash": item["sourceHash"],
            "decision": "attribute", "confirmed": True, "contactSnapshotId": item["contactSnapshotId"]}
        attributed = post(client, lane, "/review-event", **body)
        assert attributed.status_code == 200, attributed.text
        views.append(attributed.json())
        assert attributed.json()["heldPending"] == 0 and attributed.json()["totalPending"] == 1
        assert post(client, lane, "/review-events", chaseId="case-b").json()["totalPending"] == 0
        body.update(actionId=str(uuid4()), decision="handled", contactSnapshotId=None)
        handled = post(client, lane, "/review-event", **body)
        assert handled.status_code == 200, handled.text
        views.append(handled.json())
        assert handled.json()["totalPending"] == 0
        history = post(client, lane).json()
        assert len(history["events"]) == 1 and history["events"][0]["text"] == item["text"]
        assert not history["events"][0]["needsStaffReview"]
        assert lane.vendor.requests == []
    result = subprocess.run(["node", "--input-type=module", "-e", """
        import {readEventReview} from './src/communicationsEventReview.js';
        let input=''; for await (const chunk of process.stdin) input+=chunk;
        for (const view of JSON.parse(input)) readEventReview(view,view.chaseId);
        """], input=json.dumps(views), text=True, capture_output=True, timeout=10,
        cwd=Path(__file__).resolve().parents[1] / "frontend")
    assert result.returncode == 0, result.stderr


def test_empty_worklist_advertises_staff_communications_capability(lane):
    with TestClient(lane.app) as client:
        for role in ("Admin", "BillingStaff"):
            with psycopg.connect(DSN) as db:
                db.execute("UPDATE practice_memberships SET roles=%s WHERE practice_id=%s", ([role], lane.practice))
            headers = _auth_headers(user_id=lane.actor, tenant_id=lane.tenant, roles=(role,))
            response = client.post("/api/chase/worklist", headers=headers, json={"assignee": str(uuid4())})
            assert response.status_code == 200, response.text
            assert response.json()["rows"] == []
            assert response.json()["communications"] == {"available": True, "mode": "durable"}
    assert lane.vendor.requests == []


def test_worklist_communications_capability_absent_for_coder_and_disabled_app(lane, tmp_path):
    with TestClient(lane.app) as client:
        with psycopg.connect(DSN) as db:
            db.execute("UPDATE practice_memberships SET roles=ARRAY['Coder'] WHERE practice_id=%s", (lane.practice,))
        headers = _auth_headers(user_id=lane.actor, tenant_id=lane.tenant, roles=("Coder",))
        response = client.post("/api/chase/worklist", headers=headers, json={})
        assert response.status_code == 200, response.text
        assert "communications" not in response.json()
        assert all("communicationReview" not in row for row in response.json()["rows"])
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET roles=ARRAY['Admin'] WHERE practice_id=%s", (lane.practice,))
    disabled = replace(lane.config, communications_enabled=False, chase_transport="record_only")
    with TestClient(api.create_app(settings=disabled, frontend_dist=tmp_path / "missing")) as client:
        response = client.post("/api/chase/worklist", headers=lane.headers, json={})
        assert response.status_code == 200, response.text
        assert "communications" not in response.json()
        assert post(client, lane).status_code == 404
    assert lane.vendor.requests == []
