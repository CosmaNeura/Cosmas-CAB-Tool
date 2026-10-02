"""Opt-in synthetic-only PG16/API/JS export rehearsal under restricted roles."""
import base64
import json
import os
import re
from copy import deepcopy
from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.api import create_app
from cosmaneura_billing.billing_header import DIAGNOSIS_ORDER_DECISION, create_draft
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.migrations import _provision_runtime_role
from cosmaneura_billing.persistence import PostgresContext, PostgresEncounterStore
from cosmaneura_billing.settings import Settings

DSN = os.environ.get("CAB_PLATFORM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable integration database is absent")


def test_restricted_replicas_persist_human_primary_with_actor_audit_and_export(tmp_path, monkeypatch):
    with psycopg.connect(DSN, autocommit=True) as db:
        socket_dir = Path(db.info.host).resolve()
        assert re.fullmatch(r"cab_header_test_[0-9a-f]{8}", db.info.dbname)
        assert socket_dir.parent.parent == Path("/private/tmp")
        assert socket_dir.parent.name.startswith("cab-header-human-order-")
        assert db.info.user == "cab_header_owner"
        assert db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state<>'idle'").fetchone()[0] == 0
        assert [row[0][:3] for row in db.execute("SELECT name FROM cosmaneura_schema_migrations ORDER BY name").fetchall()] == [f"{n:03}" for n in range(1, 10)] + ["011", "013"]
    practice, tenant, provider, coder = "human-primary-" + uuid4().hex, str(uuid4()), str(uuid4()), str(uuid4())
    role = "human_order_" + uuid4().hex[:20]
    settings = Settings(environment="test", auth_mode="container_apps", entra_tenant_id=tenant,
        practice_id=practice, state_store="postgres", chase_store="postgres",
        database_url=make_conninfo(DSN, user=role), database_runtime_role=role,
        billing_header_enabled=True, database_runtime_password="synthetic-local-test-only",
        azure_openai_enabled=False, azure_language_enabled=False,
        review_context_signing_key="synthetic-local-primary-review-test-key-only")
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES (%s,%s)", (practice, tenant))
        for user, membership_role in ((provider, "Provider"), (coder, "Coder"), (str(uuid4()), "Admin")):
            db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (tenant, user))
            db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES (%s,%s,%s,%s)", (practice, tenant, user, [membership_role]))
        _provision_runtime_role(db, settings)
        assert db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=%s", (role,)).fetchone() == (False, False, False, False)
    owner = PostgresContext(DSN, practice_id=practice)
    PostgresEncounterStore(owner).upsert(SAMPLE_VISIT, source="synthetic")

    def auth(user, assigned):
        data = {"auth_typ": "aad", "claims": [{"typ": "oid", "val": user}, {"typ": "tid", "val": tenant}, {"typ": "roles", "val": assigned}]}
        return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(data).encode()).decode()}

    def post(client, suffix, payload):
        response = client.post("/api/encounters/" + suffix, json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    one = create_app(settings=settings, frontend_dist=tmp_path / "absent")
    two = create_app(settings=settings, frontend_dist=tmp_path / "absent")
    with TestClient(one, client=("127.0.0.1", 51001)) as p, TestClient(two, client=("127.0.0.1", 51002)) as c:
        assert type(one.state.orchestrator.coding.llm).__name__ == "MockLLM"
        assert type(two.state.orchestrator.coding.llm).__name__ == "MockLLM"
        p.headers.update(auth(provider, "Provider")); c.headers.update(auth(coder, "Coder"))
        detail = post(p, "detail", {"encounterId": SAMPLE_VISIT.encounter_id})
        target = {"encounterId": SAMPLE_VISIT.encounter_id, "expectedSource": detail["headerSource"]}
        view = post(p, "header/generate", {**target, "operationId": str(uuid4())})["view"]
        original = json.loads(json.dumps(view["header"]))
        draft = create_draft(view["header"]); draft["codes"].reverse(); draft["diagnosisOrderConfirmed"] = True
        primary = next(row["code"] for row in draft["codes"] if row["kind"] == "ICD10")
        save = {**target, "headerId": view["header"]["headerId"], "expectedRevision": 0,
                "operationId": str(uuid4()), "draft": draft, "reason": "Provider source-confirmed primary and order"}
        saved = post(p, "header/save", save)["view"]
        assert saved["revision"]["actor"]["id"] == provider
        assert [row["path"] for row in saved["revision"]["changes"]] == ["diagnosisOrder"]
        assert post(c, "header", target)["view"] == saved
        invalid_draft = deepcopy(draft)
        next(row for row in invalid_draft["codes"] if row["kind"] == "ICD10")["code"] = "R05"
        invalid_save = {**save, "expectedRevision": 1, "operationId": str(uuid4()), "draft": invalid_draft}
        assert p.post("/api/encounters/header/save", json=invalid_save).status_code == 422
        assert post(c, "header", target)["view"] == saved
        engine = post(c, "coder-view", {"encounterId": SAMPLE_VISIT.encounter_id})
        rows = ([engine["em"]] if engine["em"] else []) + engine["dx"]
        feedback = {"encounter_id": SAMPLE_VISIT.encounter_id, "encounter_version": engine["encounterVersion"],
                    "review_id": str(uuid4()), "review_context": engine["reviewContext"],
                    "final_em": engine["em"]["code"] if engine["em"] else None,
                    "decisions": [{"kind": row["kind"], "engine_code": row["code"], "action": "accept", "final_code": row["code"]} for row in rows]}
        command = {**target, "headerId": view["header"]["headerId"], "expectedRevision": 1,
                   "feedback": feedback, "acknowledged": True, "humanProposalDecisions": [
                       {"id": DIAGNOSIS_ORDER_DECISION, "decision": "accept", "reason": "Coder reviewed source and accepted primary"}]}
        assert p.post("/api/encounters/header/signoff", json=command).status_code == 403
        recorded = post(c, "header/signoff", command)
        loaded = post(p, "header", target)["view"]
        assert loaded == recorded["view"] and loaded["header"] == original
        assert loaded["signoff"]["actor"]["id"] == coder and loaded["signoff"]["headerRevision"] == 1
        assert post(c, "header/signoff", command)["replayed"] is True
        # Catalog changes cannot invalidate an exact historical receipt, but
        # they must prevent any new final ICD decision in the same real store.
        from cosmaneura_billing import coder_feedback
        from cosmaneura_billing.persistence import PostgresFeedbackStore
        store = PostgresFeedbackStore(PostgresContext(settings.database_url, practice_id=practice))
        [historical] = store.load()
        with monkeypatch.context() as patch:
            patch.setattr(coder_feedback, "valid_human_icd10", lambda _: False)
            assert post(c, "header/signoff", command)["replayed"] is True
            assert store.append(historical) is False
            new_payload = historical.to_dict(); new_payload["review_id"] = str(uuid4())
            with pytest.raises(coder_feedback.InvalidCurrentICD10):
                store.append(coder_feedback.EncounterFeedback.from_dict(new_payload))
        with owner.connection() as db:
            assert db.execute("SELECT count(*) AS n FROM coder_feedback WHERE practice_id=%s", (practice,)).fetchone()["n"] == 1
        js = """import fs from 'node:fs'; import {readBillingHeader,exportBillingHeader} from './frontend/src/billingHeader.js';
const {view,encounter}=JSON.parse(fs.readFileSync(0,'utf8')); readBillingHeader(view,encounter); process.stdout.write(exportBillingHeader(view,encounter));"""
        result = subprocess.run(["node", "--input-type=module", "-e", js], input=json.dumps({"view": loaded, "encounter": {**detail, "encounterId": SAMPLE_VISIT.encounter_id}}), text=True, capture_output=True, check=True, cwd=Path(__file__).resolve().parents[1])
        assert f"Coder-confirmed primary: {primary}" in result.stdout
        assert original["evidence"][0]["text"] in result.stdout and "ORIGINAL PIPELINE" in result.stdout
        updated = post(p, "header/save", {**save, "expectedRevision": 1, "operationId": str(uuid4()), "draft": {**draft, "note": "Fresh review required"}})["view"]
        assert updated["signoff"] is None and updated["previousSignoff"]["headerRevision"] == 1
        assert c.post("/api/encounters/header/signoff", json=command).status_code == 409
    with owner.connection() as db:
        revisions = db.execute("SELECT revision_payload FROM billing_header_revisions WHERE practice_id=%s ORDER BY revision", (practice,)).fetchall()
        assert len(revisions) == 2
        assert revisions[0]["revision_payload"]["actor"]["id"] == provider
        assert revisions[0]["revision_payload"]["changes"][0]["after"]["confirmed"] is True
        assert db.execute("SELECT count(*) AS n FROM billing_header_signoffs WHERE practice_id=%s", (practice,)).fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM access_audit_completions c JOIN access_audit_attempts a USING(practice_id,event_id) WHERE c.practice_id=%s AND a.action='billing_header.signoff' AND c.outcome='allowed'", (practice,)).fetchone()["n"] >= 1
    assert PostgresEncounterStore(owner).get(SAMPLE_VISIT.encounter_id).bundle == SAMPLE_VISIT
