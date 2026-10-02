"""Authenticated API rehearsal using actual PG16 roles and two API replicas."""
import base64
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.api import create_app
from cosmaneura_billing.billing_header import create_draft
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.migrations import _provision_runtime_role
from cosmaneura_billing.persistence import PostgresContext, PostgresEncounterStore
from cosmaneura_billing.settings import Settings

DSN = os.environ.get("CAB_PLATFORM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit local CAB integration database is absent")


def test_two_authenticated_replicas_share_header_edits_and_audit_failure_rolls_back(tmp_path):
    with psycopg.connect(DSN, autocommit=True) as db:
        assert db.info.dbname == "cab_integration_test" and db.info.host.startswith("/private/tmp")
        migrations = Path(__file__).resolve().parents[1] / "migrations"
        if db.execute("SELECT to_regclass('public.billing_headers')").fetchone()[0] is None:
            for path in sorted(migrations.glob("*.up.sql")):
                if path.name.startswith("001_"):
                    continue  # Header/communications do not require pgvector.
                db.execute(path.read_text(), prepare=False)
    practice, tenant, provider, coder = "header-api-" + uuid4().hex, str(uuid4()), str(uuid4()), str(uuid4())
    role = "header_api_" + uuid4().hex[:20]
    settings = Settings(environment="test", auth_mode="container_apps", entra_tenant_id=tenant,
        practice_id=practice, state_store="postgres", chase_store="postgres",
        database_url=make_conninfo(DSN, user=role), database_runtime_role=role,
        billing_header_enabled=True, database_runtime_password="synthetic-local-test-only",
        review_context_signing_key="synthetic-header-rehearsal-signing-key-only")
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES (%s,%s)", (practice, tenant))
        for user, membership_role in ((provider, "Provider"), (coder, "Coder"), (str(uuid4()), "Admin")):
            db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (tenant, user))
            db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES (%s,%s,%s,%s)",
                       (practice, tenant, user, [membership_role]))
        _provision_runtime_role(db, settings)
    owner = PostgresContext(DSN, practice_id=practice)
    PostgresEncounterStore(owner).upsert(SAMPLE_VISIT, source="synthetic")

    def auth(user, assigned):
        data = {"auth_typ": "aad", "claims": [{"typ": "oid", "val": user},
            {"typ": "tid", "val": tenant}, {"typ": "roles", "val": assigned}]}
        return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(data).encode()).decode()}

    one = create_app(settings=settings, frontend_dist=tmp_path / "absent")
    two = create_app(settings=settings, frontend_dist=tmp_path / "absent")
    with TestClient(one, client=("127.0.0.1", 51001)) as p, TestClient(two, client=("127.0.0.1", 51002)) as c:
        p.headers.update(auth(provider, "Provider"))
        c.headers.update(auth(coder, "Coder"))
        detail = p.post("/api/encounters/detail", json={"encounterId": SAMPLE_VISIT.encounter_id})
        assert detail.status_code == 200, detail.text
        target = {"encounterId": SAMPLE_VISIT.encounter_id, "expectedSource": detail.json()["headerSource"]}
        generated = p.post("/api/encounters/header/generate", json={**target, "operationId": str(uuid4())})
        assert generated.status_code == 200, generated.text
        view = generated.json()["view"]
        draft = create_draft(view["header"])
        draft["note"] = "Synthetic coder review note"
        command = {**target, "operationId": str(uuid4()), "headerId": view["header"]["headerId"],
                   "expectedRevision": 0, "draft": draft, "reason": "Synthetic cross-replica test"}
        saved = c.post("/api/encounters/header/save", json=command)
        assert saved.status_code == 200, saved.text
        assert saved.json()["view"]["revision"]["actor"]["id"] == coder
        loaded = p.post("/api/encounters/header", json=target)
        assert loaded.status_code == 200 and loaded.json()["view"]["revisionNumber"] == 1
        assert loaded.json()["view"]["revision"]["draft"]["note"] == draft["note"]
        assert p.post("/api/encounters/header/save", json={**command, "operationId": str(uuid4())}).status_code == 409

        with psycopg.connect(DSN) as db:
            db.execute(sql.SQL("REVOKE INSERT ON access_audit_completions FROM {}").format(sql.Identifier(role)))
        draft["note"] = "Must roll back with failed audit"
        failed = c.post("/api/encounters/header/save", json={**command, "draft": draft,
            "expectedRevision": 1, "operationId": str(uuid4())})
        assert failed.status_code == 503
        with owner.connection() as db:
            assert db.execute("SELECT count(*) AS n FROM billing_header_revisions WHERE practice_id=%s", (practice,)).fetchone()["n"] == 1
        assert PostgresEncounterStore(owner).get(SAMPLE_VISIT.encounter_id).bundle.signed_note == SAMPLE_VISIT.signed_note
