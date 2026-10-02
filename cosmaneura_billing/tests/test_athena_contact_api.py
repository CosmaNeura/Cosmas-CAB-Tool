"""Mounted contact-only API: actual auth, restricted SQL and fake read-only Athena."""
import base64
from dataclasses import replace
import json
import os
from uuid import uuid4
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
psycopg = pytest.importorskip("psycopg")

from athena_configured_fixture import CASE, app_settings, fake_client, prepare, prepare_schema
from cosmaneura_billing.api import create_app
from test_athena_review import apply_command, national_phone_source

DSN = os.environ.get("CAB_ATHENA_CONFIGURED_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit owned local Athena API database required")


@pytest.fixture(scope="module", autouse=True)
def schema():
    prepare_schema(DSN)



def prepare_api(*, role="Admin", register=True):
    # A practice still has its real Admin bootstrap; the test caller receives
    # only the requested membership, never a forged domain ReviewActor.
    owner, context, actor, service, audit = prepare(DSN, register=register)
    if role != "Admin":
        user = str(uuid4())
        with psycopg.connect(DSN) as db:
            db.execute("INSERT INTO users(tenant_id,user_id) VALUES(%s::uuid,%s::uuid)",
                       (actor.tenant_id, user))
            db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s::uuid,%s::uuid,%s)",
                       (owner.practice_id, actor.tenant_id, user, [role]))
        actor = SimpleNamespace(tenant_id=actor.tenant_id, user_id=user, role=role)
    return owner, context, actor, service, audit


def headers(actor, role=None, tenant=None):
    claims = {"auth_typ": "aad", "claims": [
        {"typ": "oid", "val": actor.user_id}, {"typ": "tid", "val": tenant or actor.tenant_id},
        {"typ": "roles", "val": role or actor.role}]}
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(claims).encode()).decode()}


def application(owner, context, tmp_path, calls, *, national=False):
    config = replace(app_settings(owner, context, tmp_path),
                     allowed_roles=("Admin", "BillingStaff", "Coder", "Provider"))
    app = create_app(settings=config, frontend_dist=tmp_path)
    # Only the HTTP adapter is synthetic. Keep actual configured API/store composition.
    def factory(scope):
        calls.append(scope)
        client = fake_client(scope)
        if national:
            national_phone_source(client)
        return client
    app.state.athena_review_service._factory = factory
    return app


@pytest.mark.parametrize("role", ["Admin", "BillingStaff"])
def test_staff_contact_api_applies_exact_selection_replays_and_keeps_billing_unavailable(tmp_path, role):
    owner, context, actor, _, _ = prepare_api(role=role)
    calls = []
    first = application(owner, context, tmp_path, calls)
    second = application(owner, context, tmp_path, calls)
    composed = first.state.chase_context
    with TestClient(first, headers=headers(actor)) as a, TestClient(second, headers=headers(actor)) as b:
        assert first.state.chase_context is composed
        assert a.get("/api/session").json()["capabilities"]["athenaReview"] is True
        task = a.post("/api/chase/task", json={"chaseId": CASE})
        assert task.status_code == 200, task.text
        assert task.json()["athenaReview"] == {"available": True, "mode": "configured", "billingAvailable": False}
        current = a.post("/api/athena/card-chase", json={"chaseId": CASE}).json()
        assert current["view"]["revision"] == 0 and current["connection"]["readAllowed"] and not calls
        command = {"chaseId": CASE, "operationId": str(uuid4()), "expectedRevision": 0,
                   "expectedBindingSha256": current["connection"]["binding"]["scopeSha256"]}
        response = a.post("/api/athena/card-chase/read", json=command)
        assert response.status_code == 200, response.text
        source = response.json()["view"]
        assert source["candidate"]["source"]["patientId"] == "60178"
        body = {"chaseId": CASE, **apply_command(source), "selectedEmail": False,
                "selectedPolicyIds": [], "confirmedPolicyReview": False}
        denied = a.post("/api/athena/card-chase/apply", json={**body, "confirmedPatientMatch": False})
        assert denied.status_code == 422, denied.text
        selected = a.post("/api/athena/card-chase/apply", json=body)
        assert selected.status_code == 200, selected.text
        saved = selected.json()
        assert [c["field"] for c in saved["view"]["applied"]["contacts"]] == ["mobile_phone"]
        assert all(c["consent"] == "unknown" for c in saved["view"]["applied"]["contacts"])
        source_calls = len(calls)
        retry = b.post("/api/athena/card-chase/apply", json=body)
        assert retry.status_code == 200 and retry.json()["view"] == saved["view"]
        assert retry.json()["operationReceipt"]["replayed"] and len(calls) == source_calls
        for path in ("/api/athena/billing/read", "/api/athena/billing/apply"):
            assert a.post(path, json={"chaseId": CASE}).status_code == 404
        assert not any("/api/athena/billing/" in path for path in first.openapi()["paths"])
        assert a.get("/api/ready").status_code == b.get("/api/ready").status_code == 200
    assert first.state.chase_context is composed
    with TestClient(first, headers=headers(actor)) as reopened:
        result = reopened.post("/api/athena/card-chase", json={"chaseId": CASE})
        assert result.status_code == 200 and result.json()["view"] == saved["view"]
        assert first.state.chase_context is composed and len(calls) == source_calls
    with psycopg.connect(DSN) as db:
        rows = db.execute("SELECT channel,consent_status FROM chase_contact_routes WHERE practice_id=%s",
                          (owner.practice_id,)).fetchall()
        assert sorted(rows) == [("phone", "unknown"), ("sms", "unknown")]
        assert db.execute("SELECT count(*) FROM athena_case_review_events WHERE practice_id=%s",
                          (owner.practice_id,)).fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM encounters WHERE practice_id=%s",
                          (owner.practice_id,)).fetchone()[0] == 0
        actions = db.execute("""SELECT a.action,c.outcome,c.status FROM access_audit_attempts a
            JOIN access_audit_completions c USING(practice_id,event_id)
            WHERE a.practice_id=%s AND a.action LIKE 'athena.%%'""", (owner.practice_id,)).fetchall()
        assert ("athena.refresh", "allowed", 200) in actions and ("athena.apply", "allowed", 200) in actions
        assert ("athena.apply", "error", 422) in actions


@pytest.mark.parametrize("role", ["Coder", "Provider"])
def test_clinical_members_have_no_contact_capability_or_read_authority(tmp_path, role):
    owner, context, actor, _, _ = prepare_api(role=role)
    calls = []
    app = application(owner, context, tmp_path, calls)
    with TestClient(app, headers=headers(actor)) as client:
        assert "athenaReview" not in client.get("/api/session").json()["capabilities"]
        for route, payload in (("/api/athena/card-chase", {"chaseId": CASE}),
            ("/api/athena/card-chase/read", {"chaseId": CASE, "operationId": str(uuid4()), "expectedRevision": 0})):
            assert client.post(route, json=payload).status_code == 403
        if role == "Coder":
            assert "athenaReview" not in client.post("/api/chase/task", json={"chaseId": CASE}).json()
    assert not calls


def test_unmapped_and_foreign_tenant_never_construct_source_client(tmp_path):
    owner, context, actor, _, _ = prepare_api(register=False)
    calls = []
    app = application(owner, context, tmp_path, calls)
    with TestClient(app, headers=headers(actor)) as client:
        current = client.post("/api/athena/card-chase", json={"chaseId": CASE})
        assert current.status_code == 200 and current.json()["connection"]["state"] == "connection_missing"
        assert current.json()["connection"]["binding"] is None and not current.json()["connection"]["readAllowed"]
        command = {"chaseId": CASE, "operationId": str(uuid4()), "expectedRevision": 0}
        assert client.post("/api/athena/card-chase/read", json=command).status_code == 409
        assert client.post("/api/athena/card-chase/read", json={**command, "sourcePatientId": None}).status_code == 409
        assert client.post("/api/athena/card-chase", json={"chaseId": CASE},
                           headers=headers(actor, tenant=str(uuid4()))).status_code == 401
        assert client.post("/api/athena/card-chase", json={"chaseId": "another-case"}).status_code == 404
    assert not calls


def test_national_phone_requires_explicit_country_in_actual_staff_api(tmp_path):
    owner, context, actor, _, _ = prepare_api(role="BillingStaff")
    app = application(owner, context, tmp_path, [], national=True)
    with TestClient(app, headers=headers(actor)) as client:
        current = client.post("/api/athena/card-chase", json={"chaseId": CASE}).json()
        response = client.post("/api/athena/card-chase/read", json={"chaseId": CASE, "operationId": str(uuid4()),
            "expectedRevision": 0, "expectedBindingSha256": current["connection"]["binding"]["scopeSha256"]})
        assert response.status_code == 200, response.text
        command = {"chaseId": CASE, **apply_command(response.json()["view"])}
        assert client.post("/api/athena/card-chase/apply", json=command).status_code == 422
        assert client.post("/api/athena/card-chase/apply", json={**command, "selectedPhoneCountry": "CA"}).status_code == 422
        result = client.post("/api/athena/card-chase/apply", json={**command, "selectedPhoneCountry": "US"})
        assert result.status_code == 200, result.text
        phone = result.json()["view"]["applied"]["contacts"][0]
        assert phone["value"] == "+12025550123" and phone["normalization"]["sourceValue"] == "2025550123"
        assert phone["consent"] == "unknown"


def test_disabled_mode_is_not_advertised_and_performs_no_source_read(tmp_path):
    owner, context, actor, _, _ = prepare_api()
    config = replace(app_settings(owner, context, tmp_path), athena_review_mode="disabled",
                     athena_client_id="", athena_client_secret="")
    app = create_app(settings=config, frontend_dist=tmp_path)
    assert app.state.athena_review_service is None
    with TestClient(app, headers=headers(actor)) as client:
        assert "athenaReview" not in client.get("/api/session").json()["capabilities"]
        assert "athenaReview" not in client.post("/api/chase/task", json={"chaseId": CASE}).json()
        assert client.post("/api/athena/card-chase", json={"chaseId": CASE}).status_code == 404
