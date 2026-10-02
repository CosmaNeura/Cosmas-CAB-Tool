"""Actual provisioned worker/callback authority; synthetic inputs, no network."""
from dataclasses import asdict
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from cosmaneura_billing.chase.communications import runtime, webhooks, worker
from cosmaneura_billing.chase.communications.twilio import TwilioAdapter, TwilioConfig
from cosmaneura_billing.migrations import _provision_chase_worker_role, _provision_runtime_role
from cosmaneura_billing.settings import Settings
from test_communications_dispatch import DSN, lane, schema, foundation_schema, ready, webhook, psycopg
from test_communications_settings import environment

pytestmark = pytest.mark.skipif(not DSN, reason="disposable SMS PostgreSQL is not configured")


def services(lane, tmp_path):
    worker_role = "sms_service_" + uuid4().hex[:18]
    provision = Settings(practice_id=lane.practice, database_url=DSN,
        chase_worker_database_role=worker_role,
        chase_worker_database_password="synthetic-service-test-only")
    with psycopg.connect(DSN) as db:
        _provision_chase_worker_role(db, provision)
    info = conninfo_to_dict(DSN)
    uri = f"postgresql://{worker_role}@{info['host']}:{info['port']}/{info['dbname']}"
    roster = tmp_path / "synthetic-roster.json"
    roster.write_text("[]")
    env = environment(COSMANEURA_PRACTICE_ID=lane.practice,
        COSMANEURA_TWILIO_ACCOUNT_SID=lane.account,
        COSMANEURA_TWILIO_CALLBACK_BASE_URL="https://synthetic.invalid",
        COSMANEURA_TWILIO_AUTH_TOKEN="synthetic-test-secret",
        COSMANEURA_CHASE_WORKER_DB_ROLE=worker_role,
        COSMANEURA_CHASE_WORKER_DATABASE_URL=uri,
        COSMANEURA_DIRECT_CONTRACT_LIST=str(roster))
    callback = worker.configured_dispatcher(environ=env)
    # No real outbound adapter is ever constructed in the PG proof.
    outbound = runtime.dispatcher_from_settings(Settings.from_env(env), dsn=uri,
        adapter=TwilioAdapter(TwilioConfig(**asdict(callback.sender),
            auth_token="synthetic-test-secret"), transport=lane.vendor))
    return callback, outbound, worker_role


def test_real_worker_role_accepts_signed_reply_and_dark_batch_does_not_send(lane, tmp_path):
    ready(lane)
    callback, outbound, role = services(lane, tmp_path)
    assert callback.adapter.transport is None and callback.policy.enabled_channels == ("sms",)
    lane.sender = callback.sender
    raw, signature = webhook(lane, "sms_inbound")
    with TestClient(webhooks.create_app(callback)) as client:
        assert client.get("/ready").status_code == 200
        path = runtime.CALLBACK_PATHS["sms_inbound"]
        headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature}
        assert client.post(path, content=raw, headers={**headers, "X-Twilio-Signature": "invalid"}).status_code == 403
        first = client.post(path, content=raw, headers=headers)
        assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
        assert client.post(path, content=raw, headers=headers).status_code == 200
    result = outbound.run_once()
    assert not outbound.policy.scheduler_enabled and not outbound.policy.patient_messaging_enabled
    assert not outbound.policy.operationally_activated
    # The old authorized policy cannot authorize this newly composed dark service.
    assert result[0]["reason"] == "communications_policy_approval_required"
    assert lane.vendor.requests == []
    with callback.store._db() as db:
        assert db.execute("SELECT current_user AS role").fetchone()["role"] == role
        assert db.execute("SELECT count(*) AS n FROM chase_communication_provider_events").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM chase_communication_dispatch_starts").fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM chase_communication_dispatch_receipts").fetchone()["n"] == 0


def test_worker_has_no_staff_or_clinical_authority_while_api_owns_staff_decisions(lane, tmp_path):
    callback, _, _ = services(lane, tmp_path)
    with callback.store._db() as db:
        for table in ("encounters", "users", "practice_memberships", "access_audit_attempts", "access_audit_completions", "coder_feedback"):
            assert not db.execute("SELECT has_table_privilege(current_user,%s,'SELECT') AS allowed", (table,)).fetchone()["allowed"]
        for suffix in ("senders", "bindings", "operations", "policy_decisions", "consent_decisions",
                       "authorizations", "cancellations", "unknown_resolutions", "event_reviews"):
            assert not db.execute("SELECT has_table_privilege(current_user,%s,'INSERT') AS allowed",
                ("chase_communication_" + suffix,)).fetchone()["allowed"]
    api_role = "sms_unmounted_" + uuid4().hex[:18]
    with psycopg.connect(DSN) as db:
        _provision_runtime_role(db, Settings(practice_id=lane.practice, database_url=DSN,
            database_runtime_role=api_role, database_runtime_password="synthetic-test-only"))
        assert db.execute("SELECT has_table_privilege(%s,'chase_communication_authorizations','INSERT')", (api_role,)).fetchone()[0]


def test_callback_readiness_detects_lost_insert_authority(lane, tmp_path):
    callback, _, role = services(lane, tmp_path)
    with TestClient(webhooks.create_app(callback)) as client:
        assert client.get("/ready").status_code == 200
        with psycopg.connect(DSN) as db:
            db.execute(sql.SQL("REVOKE INSERT ON chase_communication_provider_events FROM {}").format(sql.Identifier(role)))
        assert client.get("/ready").status_code == 503
        assert client.get("/live").status_code == 200
        with psycopg.connect(DSN) as db:
            db.execute(sql.SQL("GRANT INSERT ON chase_communication_provider_events TO {}").format(sql.Identifier(role)))
        assert client.get("/ready").status_code == 200
