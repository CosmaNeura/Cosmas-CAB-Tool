"""Disposable PostgreSQL + signed synthetic callbacks; no outbound transport use."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from fastapi.testclient import TestClient
import pytest

from cosmaneura_billing.persistence import PostgresAccessAuditStore
from test_communications_runtime import DSN, lane, schema as foundation_schema, callback, post
from test_chase_communications_postgres import seed_case, PHONE
from test_security import _auth_headers

pytestmark = pytest.mark.skipif(not DSN, reason="disposable runtime PostgreSQL is not configured")
BASE = "/api/chase/communications"
UNKNOWN = "+12025550177"


@pytest.fixture(scope="module", autouse=True)
def schema(foundation_schema):
    with psycopg.connect(DSN) as db:
        assert "test" in db.info.dbname
        assert db.execute("SELECT to_regclass('public.chase_communication_unassigned_sources')").fetchone()[0]


def request(client, lane, path="/unassigned-texts", **body):
    return client.post(BASE + path, headers=lane.headers, json=body)


def bind(client, lane, case=None, snapshot=None):
    result = request(client, lane, "/bind", chaseId=case or lane.case, channel="sms",
        contactSnapshotId=snapshot or lane.snapshot, confirmedContact=True)
    assert result.status_code == 200, result.text


def inbound(client, callbacks, lane, **changes):
    bind(client, lane)
    result = callback(callbacks, lane, "sms_inbound", From=changes.pop("From", UNKNOWN), **changes)
    assert result.status_code == 200, result.text
    value = request(client, lane).json()
    assert value["schemaVersion"] == "communications-unassigned.v1", value
    return value["items"][-1]


def choices(client, lane, item, **changes):
    return request(client, lane, "/unassigned-text-cases", eventRef=item["ref"], sourceHash=item["sourceHash"], **changes)


def command(item, case=None, snapshot=None, **changes):
    return dict(actionId=str(uuid4()), eventRef=item["ref"], sourceHash=item["sourceHash"],
        decision="attribute" if case else "leave_unassigned", chaseId=case, contactSnapshotId=snapshot, confirmed=True, **changes)


def decide(client, lane, cmd):
    return request(client, lane, "/review-unassigned-text", **cmd)


def new_case(lane, case="target", phone=UNKNOWN):
    return seed_case(lane, case=case, phone=phone, consent="unknown", observed=datetime.now(timezone.utc).isoformat())


def test_unmatched_inbound_survives_review_and_replay_without_outbound_or_consent(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        assert client.get("/api/ready", headers=lane.headers).status_code == 200
        assert callbacks.get("/ready").status_code == 200
        item = inbound(client, callbacks, lane, Body="Synthetic unknown sender", NumMedia="1")
        assert item["senderHint"] == "Ending 0177" and item["mediaCount"] == 1
        assert choices(client, lane, item).json()["items"] == []
        cmd = command(item)
        receipt = decide(client, lane, cmd)
        assert receipt.status_code == 200, receipt.text
        assert decide(client, lane, cmd).json() == receipt.json()
        assert request(client, lane, "/unassigned-texts/count").json()["pendingCount"] == 0
        reviewed = request(client, lane, state="reviewed").json()["items"]
        assert len(reviewed) == 1 and reviewed[0]["reviewState"] == "left_unassigned"
        assert reviewed[0]["text"] == item["text"] and reviewed[0]["attributedCaseId"] is None
        assert set(reviewed[0]) == {"ref", "sourceHash", "kind", "channel", "text", "at", "status", "disposition",
            "mediaCount", "senderHint", "reviewState", "reviewedAt", "attributedCaseId"}
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT case_id,text FROM chase_communication_provider_events WHERE practice_id=%s", (lane.practice,)).fetchone() == (None, item["text"])
        assert db.execute("SELECT count(*) FROM chase_communication_event_reviews WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 1
        for table in ("chase_communication_operations", "chase_communication_consent_decisions", "chase_attempts"):
            assert db.execute(sql.SQL("SELECT count(*) FROM {} WHERE practice_id=%s").format(sql.Identifier(table)), (lane.practice,)).fetchone()[0] == 0
    assert lane.vendor.requests == []


def test_explicit_current_contact_attribution_needs_no_outbound_binding_and_preserves_stop(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        item = inbound(client, callbacks, lane, Body="STOP")
        snapshot = new_case(lane)
        new_case(lane, "second-target")
        available = choices(client, lane, item).json()
        assert {r["chaseId"] for r in available["items"]} == {"target", "second-target"}
        cmd = command(item, "target", snapshot)
        assert decide(client, lane, cmd).status_code == 200
        assert choices(client, lane, item).status_code == 409
        assert request(client, lane, state="reviewed").json()["items"][0]["attributedCaseId"] == "target"
        detail = request(client, lane, "/review-events", chaseId="target").json()
        assert detail["totalPending"] == 1 and detail["heldPending"] == 0
        assert detail["items"][0]["reviewState"] == "attributed"
        bind(client, lane, "target", snapshot)
        view = request(client, lane, "", chaseId="target").json()
        assert view["suppression"]["sms"] is True and view["events"][0]["text"] == "STOP"
        assert view["bindings"]["sms"]["consentGranted"] is False
        assert request(client, lane, "/review-events", chaseId="second-target").json()["totalPending"] == 0
    assert lane.vendor.requests == []


def test_legacy_case_leave_stays_global_pending_but_is_not_offered_as_target(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        bind(client, lane)
        snap = new_case(lane, "other", PHONE)
        bind(client, lane, "other", snap)
        item = inbound(client, callbacks, lane, From=PHONE)
        local = command(item); local["chaseId"] = lane.case
        response = request(client, lane, "/review-event", **local)
        assert response.status_code == 200, response.text
        assert request(client, lane).json()["pendingCount"] == 1
        offered = choices(client, lane, item).json()["items"]
        assert [v["chaseId"] for v in offered] == ["other"]
        assert decide(client, lane, command(item, lane.case, lane.snapshot)).status_code == 409
        assert decide(client, lane, command(item, "other", snap)).status_code == 200


def test_global_leave_completes_candidate_views_and_never_clears_stop(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        bind(client, lane); snap = new_case(lane, "other", PHONE); bind(client, lane, "other", snap)
        item = inbound(client, callbacks, lane, From=PHONE, Body="STOP")
        assert post(client, lane, "/review-events").json()["heldPending"] == 1
        assert decide(client, lane, command(item)).status_code == 200
        for case in (lane.case, "other"):
            assert request(client, lane, "/review-events", chaseId=case).json()["totalPending"] == 0
            assert request(client, lane, "", chaseId=case).json()["suppression"]["sms"] is True


def test_stale_source_contact_and_cross_practice_case_rejected(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        item = inbound(client, callbacks, lane); snapshot = new_case(lane)
        new_case(lane, "target", "+12025550178")
        assert choices(client, lane, item).json()["items"] == []
        assert decide(client, lane, command(item, "target", snapshot)).status_code == 409
        wrong = command(item); wrong["sourceHash"] = "f" * 64
        assert decide(client, lane, wrong).status_code == 409
        assert decide(client, lane, command(item, "other-practice-case", snapshot)).status_code == 404
        cmd = command(item); assert decide(client, lane, cmd).status_code == 200
        changed = {**cmd, "decision": "attribute", "chaseId": "target", "contactSnapshotId": snapshot}
        assert decide(client, lane, changed).status_code == 409


def test_actor_bound_idempotency_current_roles_and_audit(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        item = inbound(client, callbacks, lane); cmd = command(item)
        assert decide(client, lane, cmd).status_code == 200
        other = str(uuid4())
        with psycopg.connect(DSN) as db:
            db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (lane.tenant, other))
            db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES (%s,%s,%s,ARRAY['BillingStaff'])", (lane.practice,lane.tenant,other))
        headers = _auth_headers(user_id=other, tenant_id=lane.tenant, roles=("BillingStaff",))
        assert client.post(BASE + "/review-unassigned-text", headers=headers, json=cmd).status_code == 409
        for roles in (("Provider",), ("Coder",)):
            headers = _auth_headers(user_id=lane.actor, tenant_id=lane.tenant, roles=roles)
            assert client.post(BASE + "/unassigned-texts", headers=headers, json={}).status_code == 403
        with psycopg.connect(DSN) as db:
            db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s AND user_id=%s", (lane.practice,lane.actor))
        assert decide(client, lane, cmd).status_code == 403
    with psycopg.connect(DSN) as db:
        row = db.execute("SELECT actor_tenant_id,actor_id FROM chase_communication_event_reviews WHERE practice_id=%s", (lane.practice,)).fetchone()
        assert tuple(map(str,row)) == (lane.tenant,lane.actor)
        audits = db.execute("""SELECT a.action,a.actor_user_id FROM access_audit_attempts a
            JOIN access_audit_completions c USING(practice_id,event_id)
            WHERE a.practice_id=%s AND a.route=%s AND c.outcome='allowed'""",
            (lane.practice,BASE+"/review-unassigned-text")).fetchall()
        assert [(action,str(actor)) for action,actor in audits] == [("chase.decide",lane.actor)]


def test_audit_failure_rolls_back_and_competing_replicas_choose_only_one(lane, monkeypatch):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        item = inbound(client, callbacks, lane); snap = new_case(lane)
        original = PostgresAccessAuditStore._insert_completion
        def fail(*args, **kwargs): raise RuntimeError("synthetic audit unavailable")
        monkeypatch.setattr(PostgresAccessAuditStore, "_insert_completion", staticmethod(fail))
        with TestClient(lane.app, raise_server_exceptions=False) as failed:
            assert decide(failed, lane, command(item)).status_code >= 500
        monkeypatch.setattr(PostgresAccessAuditStore, "_insert_completion", staticmethod(original))
        assert request(client, lane).json()["pendingCount"] == 1
        def apply(cmd):
            with TestClient(lane.app) as replica: return decide(replica, lane, cmd).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(apply, [command(item), command(item,"target",snap)]))
        assert sorted(outcomes) == [200,409]


def test_pages_counts_case_choices_and_actual_json_frontend_contract(lane):
    node = shutil.which("node")
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        bind(client,lane)
        for i in range(53):
            assert callback(callbacks,lane,"sms_inbound",From=UNKNOWN,Body=f"Synthetic unknown {i}").status_code == 200
        first = request(client,lane).json(); second = request(client,lane,cursor=first["nextCursor"]).json()
        assert first["pendingCount"] == 53 and len(first["items"]) == 50 and first["hasMore"]
        assert len(second["items"]) == 3 and not second["hasMore"]
        assert len({v["ref"] for v in first["items"]+second["items"]}) == 53
        for i in range(21): new_case(lane,f"case-{i:02}")
        item = first["items"][0]; available = choices(client,lane,item).json()
        rest = choices(client,lane,item,cursor=available["nextCursor"]).json()
        assert len(available["items"]) == 20 and len(rest["items"]) == 1
        target = available["items"][0]; cmd = command(item,target["chaseId"],target["contactSnapshotId"])
        receipt = decide(client,lane,cmd).json(); reviewed = request(client,lane,state="reviewed").json()
        if node:
            bridge = subprocess.run([node,"--input-type=module","-e", """
                import {readFileSync} from 'node:fs';
                import {readUnassignedTexts,readUnassignedCases,readUnassignedReceipt} from './src/unassignedTexts.js';
                const v=JSON.parse(readFileSync(0,'utf8'));
                readUnassignedTexts(v.first,'pending'); readUnassignedTexts(v.second,'pending');
                readUnassignedCases(v.available,v.item); readUnassignedCases(v.rest,v.item);
                readUnassignedReceipt(v.receipt,v.cmd); readUnassignedTexts(v.reviewed,'reviewed');
                console.log('6 real API DTOs accepted');
            """], cwd=Path(__file__).resolve().parents[1]/"frontend",input=json.dumps({key: value for key,value in
                [("first",first),("second",second),("available",available),("rest",rest),("item",item),
                 ("receipt",receipt),("cmd",cmd),("reviewed",reviewed)]}),text=True,capture_output=True,check=False)
            assert bridge.returncode == 0, bridge.stderr
        assert request(client,lane,state="reviewed",cursor=first["nextCursor"]).status_code == 422
        assert choices(client,lane,second["items"][0],cursor=available["nextCursor"]).status_code == 422
    assert lane.vendor.requests == []


def test_locked_case_rechecks_contact_after_wait_and_fk_serializes_new_snapshot(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        item = inbound(client,callbacks,lane); snapshot = new_case(lane)
        cmd = command(item,"target",snapshot)
        with psycopg.connect(DSN) as owner, ThreadPoolExecutor(max_workers=1) as pool:
            owner.execute("SELECT 1 FROM chase_items WHERE practice_id=%s AND chase_id='target' FOR UPDATE",(lane.practice,))
            future = pool.submit(decide,client,lane,cmd)
            # Observe the review INSERT waiting on the same case lock held by a
            # verified contact refresh, then commit a new snapshot under that lock.
            for _ in range(100):
                with psycopg.connect(DSN,autocommit=True) as observer:
                    waiting = observer.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename=%s AND wait_event_type='Lock' AND query LIKE 'INSERT INTO chase_communication_event_reviews%%'",(lane.role,)).fetchone()[0]
                if waiting: break
                time.sleep(.01)
            assert waiting == 1
            latest = str(uuid4())
            owner.execute("""INSERT INTO chase_contact_snapshots(practice_id,contact_snapshot_id,chase_id,source_system,
                source_version,observed_at,verified_at,verification_method)
                VALUES (%s,%s,'target','synthetic-test','2',now(),now(),'authoritative_source')""",(lane.practice,latest))
            owner.execute("""INSERT INTO chase_contact_routes(practice_id,contact_snapshot_id,channel,destination,consent_status)
                VALUES (%s,%s,'sms','+12025550178','unknown')""",(lane.practice,latest))
            owner.commit()
            assert future.result(timeout=5).status_code == 503  # no SQL details escape; retry rechecks
        assert decide(client,lane,cmd).status_code == 409
        assert request(client,lane).json()["pendingCount"] == 1
        with psycopg.connect(DSN) as owner, ThreadPoolExecutor(max_workers=1) as pool:
            owner.execute("SELECT 1 FROM chase_items WHERE practice_id=%s AND chase_id='target' FOR UPDATE",(lane.practice,))
            future = pool.submit(new_case,lane,"target")
            for _ in range(100):
                with psycopg.connect(DSN,autocommit=True) as observer:
                    waiting = observer.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE 'INSERT INTO chase_contact_snapshots%%'").fetchone()[0]
                if waiting: break
                time.sleep(.01)
            assert waiting == 1
            owner.commit()
            assert future.result(timeout=5)
        assert decide(client,lane,cmd).status_code == 409


@pytest.mark.parametrize("path,body", [("/unassigned-texts/count",{"practiceId":"forged"}),
    ("/unassigned-texts",{"limit":51}), ("/unassigned-texts",{"limit":True}),
    ("/unassigned-texts",{"state":"all"}), ("/unassigned-texts",{"cursor":"not-a-cursor"}),
    ("/unassigned-text-cases",{"eventRef":"a"*64,"sourceHash":"b"*64,"limit":21}),
    ("/review-unassigned-text",{"actionId":str(uuid4()),"eventRef":"a"*64,"sourceHash":"b"*64,"decision":"leave_unassigned","confirmed":False}),
    ("/review-unassigned-text",{"actionId":str(uuid4()),"eventRef":"a"*64,"sourceHash":"b"*64,"decision":"attribute","confirmed":True})])
def test_closed_requests(lane,path,body):
    with TestClient(lane.app) as client:
        assert request(client,lane,path,**body).status_code == 422


def test_runtime_only_view_grant_rls_immutability_and_refused_destructive_down(lane):
    with TestClient(lane.app) as client, TestClient(lane.callbacks) as callbacks:
        item = inbound(client,callbacks,lane)
        assert decide(client,lane,command(item)).status_code == 200
    with psycopg.connect(lane.dsn) as db:
        assert db.execute("SELECT has_table_privilege(current_user,'chase_communication_unassigned_sources','SELECT')").fetchone()[0]
        db.execute("SELECT set_config('cosmaneura.practice_id','not-my-practice',true)")
        assert db.execute("SELECT count(*) FROM chase_communication_unassigned_sources").fetchone()[0] == 0
    with psycopg.connect(lane.worker_dsn) as db:
        for table, privilege in [("chase_communication_unassigned_sources","SELECT"),("chase_communication_event_reviews","INSERT")]:
            assert not db.execute("SELECT has_table_privilege(current_user,%s,%s)",(table,privilege)).fetchone()[0]
    with psycopg.connect(DSN,autocommit=True) as db:
        with pytest.raises(psycopg.Error):
            db.execute((Path(__file__).resolve().parents[1]/"migrations/018_chase_unassigned_sms_review.down.sql").read_text(),prepare=False)
        db.execute("ROLLBACK")
        assert db.execute("SELECT to_regclass('public.chase_communication_unassigned_sources')").fetchone()[0]
