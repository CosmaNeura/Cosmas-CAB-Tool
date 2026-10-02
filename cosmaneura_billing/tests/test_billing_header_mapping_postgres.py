"""Opt-in, existing disposable PG16 schema: human mapping survives two API replicas."""
import base64
import json
import os
import re
from pathlib import Path
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.api import create_app
from cosmaneura_billing import billing_header as h
from cosmaneura_billing.billing_header_mapping import mapping_candidates
from cosmaneura_billing.migrations import _provision_runtime_role
from cosmaneura_billing.persistence import PostgresContext, PostgresEncounterStore
from header_mapping_fixture import QUOTE, VISIT, MappingExtraction
from cosmaneura_billing.settings import Settings

DSN = os.environ.get("CAB_PLATFORM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable local database is absent")


def test_mapping_two_replicas_actor_audit_signoff_and_export(tmp_path, monkeypatch):
    with psycopg.connect(DSN) as db:
        socket_dir = Path(db.info.host).resolve()
        assert re.fullmatch(r"cab_header_test_[0-9a-f]{8}", db.info.dbname)
        assert socket_dir.parent.parent == Path("/private/tmp")
        assert socket_dir.parent.name.startswith("cab-header-human-map-")
        assert db.info.user == "cab_header_owner"
        assert db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state<>'idle'").fetchone()[0] == 0
        assert [row[0][:3] for row in db.execute("SELECT name FROM cosmaneura_schema_migrations ORDER BY name").fetchall()] == [f"{n:03}" for n in range(1, 10)] + ["011", "013"]
    practice, tenant, provider, coder = "human-map-" + uuid4().hex, str(uuid4()), str(uuid4()), str(uuid4())
    role = "human_map_" + uuid4().hex[:20]
    settings = Settings(environment="test", auth_mode="container_apps", entra_tenant_id=tenant,
        practice_id=practice, state_store="postgres", chase_store="postgres",
        database_url=make_conninfo(DSN, user=role), database_runtime_role=role,
        billing_header_enabled=True, database_runtime_password="synthetic-local-test-only",
        azure_openai_enabled=False, azure_language_enabled=False,
        review_context_signing_key="synthetic-local-human-map-review-key-only")
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES (%s,%s)", (practice, tenant))
        for user, member_role in [(provider, "Provider"), (coder, "Coder"), (str(uuid4()), "Admin")]:
            db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (tenant, user))
            db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES (%s,%s,%s,%s)", (practice, tenant, user, [member_role]))
        _provision_runtime_role(db, settings)
        assert db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=%s", (role,)).fetchone() == (False, False, False, False)
    owner = PostgresContext(DSN, practice_id=practice)
    quote, visit = QUOTE, VISIT
    PostgresEncounterStore(owner).upsert(visit, source="synthetic")
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
        one.state.orchestrator.coding.llm = MappingExtraction(); two.state.orchestrator.coding.llm = MappingExtraction()
        p.headers.update(auth(provider, "Provider")); c.headers.update(auth(coder, "Coder"))
        detail = post(p, "detail", {"encounterId": visit.encounter_id})
        target = {"encounterId": visit.encounter_id, "expectedSource": detail["headerSource"]}
        view = post(p, "header/generate", {**target, "operationId": str(uuid4())})["view"]
        original = json.loads(json.dumps(view["header"])); candidate, = mapping_candidates(original)
        catalog = post(p, "header/diagnosis-catalog", {**target, "headerId": original["headerId"], "expectedRevision": 0, "candidateId": candidate["id"], "query": "G43.009"})
        choice = next(row for row in catalog["matches"] if row["code"] == "G43.009")
        row = {"id": "human-mapping", "kind": "ICD10", "code": choice["code"], "label": choice["label"], "evidenceIds": candidate["evidenceIds"],
               "mapping": {"candidateId": candidate["id"], "catalogVersion": catalog["catalogVersion"]}}
        draft = h.create_draft(original); draft["codes"] = [row]; draft["diagnosisOrderConfirmed"] = True
        save = {**target, "headerId": original["headerId"], "expectedRevision": 0, "operationId": str(uuid4()), "draft": draft, "reason": "Provider reviewed catalog against source"}
        saved = post(p, "header/save", save)["view"]
        assert post(c, "header", target)["view"] == saved
        assert saved["revision"]["actor"]["id"] == provider
        assert c.post("/api/encounters/header/save", json={**save, "operationId": str(uuid4())}).status_code == 409
        engine = post(c, "coder-view", {"encounterId": visit.encounter_id})
        assert not engine["em"] and not engine["dx"]
        feedback = {"encounter_id": visit.encounter_id, "encounter_version": engine["encounterVersion"], "review_id": str(uuid4()), "review_context": engine["reviewContext"], "final_em": None,
            "decisions": [{"kind": "ICD10", "engine_code": None, "action": "add", "final_code": "G43.009", "reason_code": "dx_missed_by_engine", "note": "Rationale: Reviewed full attributes.\nEvidence: " + quote}]}
        command = {**target, "headerId": original["headerId"], "expectedRevision": 1, "feedback": feedback, "acknowledged": True,
            "humanProposalDecisions": [{"id": row["id"], "decision": "accept", "reason": "Mapping reviewed"}, {"id": h.DIAGNOSIS_ORDER_DECISION, "decision": "accept", "reason": "Primary reviewed"}]}
        assert p.post("/api/encounters/header/signoff", json=command).status_code == 403
        recorded = post(c, "header/signoff", command)
        loaded = post(p, "header", target)["view"]
        assert loaded == recorded["view"] and loaded["header"] == original
        assert loaded["signoff"]["actor"]["id"] == coder
        # Receipt replay/history do not depend on a still-available catalog;
        # changed intent does, and must not append either revision or signoff.
        from cosmaneura_billing import billing_header_mapping as mapping
        with monkeypatch.context() as patch:
            patch.setattr(mapping, "catalog_snapshot", lambda: (_ for _ in ()).throw(ValueError("unavailable")))
            assert post(p, "header", target)["view"] == loaded
            assert post(p, "header/save", save)["created"] is False
            assert post(c, "header/signoff", command)["replayed"] is True
            changed = {**save, "expectedRevision": 1, "operationId": str(uuid4()),
                       "draft": {**draft, "note": "New review requires current catalog"}}
            assert p.post("/api/encounters/header/save", json=changed).status_code == 503
        js = """import fs from 'node:fs'; import {readBillingHeader,exportBillingHeader} from './frontend/src/billingHeader.js';
const {view,encounter}=JSON.parse(fs.readFileSync(0,'utf8')); readBillingHeader(view,encounter); process.stdout.write(exportBillingHeader(view,encounter));"""
        exported = subprocess.run(["node", "--input-type=module", "-e", js], input=json.dumps({"view": loaded, "encounter": {**detail, "encounterId": visit.encounter_id}}), text=True, capture_output=True, check=True, cwd=Path(__file__).resolve().parents[1]).stdout
        assert "Coder-confirmed primary: G43.009" in exported and "Coder accepted this human mapping" in exported
        assert quote in exported and "episodic migraine disorder — held" in exported
    with owner.connection() as db:
        revisions = db.execute("SELECT revision_payload FROM billing_header_revisions WHERE practice_id=%s", (practice,)).fetchall()
        assert len(revisions) == 1
        revision = revisions[0]["revision_payload"]
        assert revision["actor"]["id"] == provider and revision["draft"]["codes"][0]["mapping"] == row["mapping"]
        assert revision["changes"][0]["after"]["mapping"] == row["mapping"]
        assert db.execute("SELECT count(*) AS n FROM billing_header_signoffs WHERE practice_id=%s", (practice,)).fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM access_audit_completions c JOIN access_audit_attempts a USING(practice_id,event_id) WHERE c.practice_id=%s AND a.action='billing_header.catalog' AND c.outcome='allowed'", (practice,)).fetchone()["n"] == 1
    assert PostgresEncounterStore(owner).get(visit.encounter_id).bundle == visit
