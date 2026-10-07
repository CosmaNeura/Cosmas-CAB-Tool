"""Restricted PostgreSQL contact projections after synthetic fake dispatch only."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg import sql

from test_communications_dispatch import (
    DSN, ready, receive, lane, schema, foundation_schema, psycopg, prepare,
)
from test_chase_communications_postgres import new_lane, seed_case
from test_communications_settings import environment
from cosmaneura_billing import api
from cosmaneura_billing.chase.communications.activity import CommunicationsActivity
from cosmaneura_billing.chase.communications.postgres import PostgresCommunicationsStore
from cosmaneura_billing.chase.communications.store import CommunicationsError
from cosmaneura_billing.chase.communications.twilio import HttpResponse
from cosmaneura_billing.chase.store import PostgresChaseStore
from cosmaneura_billing.settings import Settings

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")


def activity(lane):
    return CommunicationsActivity(lane.store).read(lane.case)


@pytest.mark.parametrize("channel,callback,status", [
    ("sms", "sms_status", "delivered"), ("automated_voice", "voice_status", "completed"),
])
def test_dispatch_once_callbacks_never_multiply_or_regress_activity(lane, channel, callback, status):
    op = ready(lane, channel)
    assert activity(lane)["totalAttempts"] == 0  # Authorized is still unsent.
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    receive(lane, callback, sid)
    receive(lane, callback, sid)  # Duplicate callback, no extra attempt.
    receive(lane, callback, sid, **({"MessageStatus": "queued"} if channel == "sms" else {"CallStatus": "ringing"}))
    if channel == "sms":
        receive(lane, "sms_inbound", Body="PRIVATE-BODY-CANARY")
    else:
        receive(lane, "voice_gather", sid, Digits="1", CallStatus="in-progress")
    result = activity(lane)
    assert result["totalAttempts"] == 1 and len(result["history"]) == 1
    assert result["lastAttempt"]["outcome"] == status
    assert result["lastAttempt"]["channel"] == ("sms" if channel == "sms" else "phone")
    assert result["lastAttempt"]["performer"] == "system" and result["staffMinutes"] == 0
    assert len(lane.vendor.requests) == 1
    serialized = json.dumps(result)
    assert all(secret not in serialized for secret in [sid, op["operationId"], lane.actor, "+1202555", "PRIVATE"])
    with lane.store._db() as db:
        assert db.execute("SELECT current_user AS name").fetchone()["name"] == lane.role
        assert not db.execute("SELECT rolsuper OR rolbypassrls AS elevated FROM pg_roles WHERE rolname=current_user").fetchone()["elevated"]
        assert db.execute("SELECT count(*) AS n FROM chase_attempts").fetchone()["n"] == 0


def test_drafts_cancellations_and_other_case_are_excluded(lane):
    canceled = ready(lane)
    lane.service.cancel(canceled["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    draft = ready(lane)
    assert activity(lane)["totalAttempts"] == 0
    lane.service.run_once()
    seed_case(lane, case="other-local-case")
    projection = CommunicationsActivity(lane.store)
    assert projection.counts([lane.case, "other-local-case"]) == {lane.case: 1, "other-local-case": 0}
    assert projection.read("other-local-case")["totalAttempts"] == 0
    assert draft["operationId"] != canceled["operationId"] and len(lane.vendor.requests) == 1


@pytest.mark.parametrize("outcome", ["rejected", "unknown", "missing_receipt"])
def test_uncertain_and_rejected_starts_count_without_delivery_claim(lane, outcome):
    op = ready(lane)
    if outcome == "missing_receipt":
        lane.service._claim(op["operationId"])  # Crash boundary before provider I/O.
    elif outcome == "unknown":
        lane.vendor.error = TimeoutError()
        lane.service.run_once()
    else:
        lane.adapter.transport = lambda request: HttpResponse(400, b'{"code":21211}')
        lane.service.run_once()
    result = activity(lane)
    assert result["totalAttempts"] == 1
    assert result["lastAttempt"]["outcome"] == ("unknown" if outcome == "missing_receipt" else outcome)


@pytest.mark.parametrize("decision", ["confirmed_delivered", "confirmed_not_sent", "confirmed_attempted"])
def test_staff_investigation_stays_distinct_from_provider_delivery(lane, decision):
    op = ready(lane)
    lane.service._claim(op["operationId"])
    lane.service.resolve_unknown(op["operationId"], decision=decision, vendor_reference="synthetic-investigation-1",
        actor_tenant_id=lane.tenant, actor_id=lane.actor)
    result = activity(lane)
    assert result["totalAttempts"] == 1 and result["lastAttempt"]["outcome"] == decision
    assert lane.vendor.requests == []


def test_projection_obeys_runtime_practice_binding(lane):
    ready(lane)
    lane.service.run_once()
    other = new_lane()
    forged = CommunicationsActivity(PostgresCommunicationsStore(lane.dsn, practice_id=other.practice))
    assert forged.counts([lane.case]) == {lane.case: 0}
    with pytest.raises(CommunicationsError) as denied:
        forged.read(lane.case)
    assert denied.value.status == 404


def test_full_totals_and_latest_fifty_share_one_bounded_projection(lane):
    first = ready(lane)
    binding = first["bindingId"]
    operations = [first["operationId"]]
    for _ in range(52):
        op = prepare(lane, binding, text=lane.policy.templates[0]["text"])
        lane.service.authorize(op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
        operations.append(op["operationId"])
    # Synthetic historical accepted attempts, with no external sends or disabled
    # constraints. Each receipt resolves the previous start before the next one.
    now = datetime.now(timezone.utc)
    with lane.store._db() as db:
        for index, operation in enumerate(operations):
            db.execute("INSERT INTO chase_communication_dispatch_starts(practice_id,operation_id,started_at) VALUES (%s,%s,%s)",
                (lane.practice, operation, now - timedelta(minutes=index)))
            db.execute("""INSERT INTO chase_communication_dispatch_receipts
                (practice_id,operation_id,account_sid,outcome,provider_sid,status)
                VALUES (%s,%s,%s,'accepted',%s,'queued')""",
                (lane.practice, operation, lane.account, "SM" + uuid4().hex))
    result = activity(lane)
    assert result["totalAttempts"] == 53 and result["countsByChannel"]["sms"] == 53
    assert len(result["history"]) == 50 and result["truncated"] is True
    assert result["lastAttempt"]["at"] == now.isoformat(timespec="microseconds")
    assert all(row["outcome"] == "queued" for row in result["history"])
    assert lane.vendor.requests == []


def test_staff_api_task_and_worklist_merge_real_ledgers_without_mutation(lane, monkeypatch):
    op = ready(lane)
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    receive(lane, "sms_status", sid)
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("GRANT SELECT ON chase_pending_tasks,chase_resolutions TO {}").format(sql.Identifier(lane.role)))
        db.execute("""INSERT INTO chase_attempts(practice_id,chase_id,attempt_no,channel,template_id,attempted_at,actor,outcome,minutes_spent,note)
            VALUES (%s,%s,1,'phone','private','2026-01-01T12:00:00Z','staff','no_answer',2,'PRIVATE-NOTE')""", (lane.practice, lane.case))
    store = PostgresChaseStore(lane.dsn, practice_id=lane.practice)
    context = {"store": store, "policy": SimpleNamespace(to_dict=lambda: {})}
    settings = Settings.from_env(environment())
    principal = SimpleNamespace(has_any_role=lambda roles: True)
    request = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(api, "_require_view", lambda *args: principal)
    monkeypatch.setattr(api, "_request_principal", lambda *args: principal)
    monkeypatch.setattr(api, "_request_chase_context", lambda *args: context)
    monkeypatch.setattr(api, "_request_settings", lambda *args: settings)
    monkeypatch.setattr("cosmaneura_billing.chase.communications.runtime.dispatcher_from_settings", lambda config: lane.service)
    result = api.chase_task(request, api.ChaseTargetRequest(chaseId=lane.case))
    worklist = api.chase_worklist(request, api.ChaseWorklistRequest())
    assert result["touches"] == result["activity"]["totalAttempts"] == worklist["rows"][0]["touches"] == 2
    assert result["activity"]["countsByChannel"] == {"sms": 1, "phone": 1, "email": 0, "portal": 0}
    assert result["activity"]["lastAttempt"]["outcome"] == "delivered"
    assert result["activity"]["staffMinutes"] == 2
    assert store.get(lane.case).touches == 1  # Presentation never advances the scheduler.
    assert "PRIVATE" not in json.dumps(result["activity"])
    assert all(value not in json.dumps(worklist) for value in [sid, op["operationId"], "+1202555", "PRIVATE"])
    principal.has_any_role = lambda roles: False
    assert api.chase_task(request, api.ChaseTargetRequest(chaseId=lane.case))["touches"] == 1
