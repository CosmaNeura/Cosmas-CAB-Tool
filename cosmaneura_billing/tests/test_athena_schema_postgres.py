"""Dormant canonical Athena schema on an explicitly empty disposable database.

No vendor/client/API path is invoked. Test-created practices, roles and receipts
are retained for inspection; never point CAB_ATHENA_SCHEMA_TEST_DSN at a shared
application database. All passwords are ephemeral, local-only fixture values.
"""
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.types.json import Jsonb

from cosmaneura_billing.athena_staff_contract import AthenaStaffProfile, AthenaStaffScope
from cosmaneura_billing.migrations import (
    _bootstrap_practice_admin, _provision_runtime_role, apply_migrations, discover_migrations,
)
from cosmaneura_billing.settings import Settings

DSN = os.environ.get("CAB_ATHENA_SCHEMA_TEST_DSN", "")
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
NEW = {14, 19, 20, 21, 22, 23}
pytestmark = pytest.mark.skipif(not DSN, reason="dedicated Athena schema test database absent")


def digest(value, *, ascii=True):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), ensure_ascii=ascii, sort_keys=True).encode()).hexdigest()


def practice(db, value):
    db.execute("SELECT set_config('cosmaneura.practice_id', %s, true)", (value,))


def snapshot(db):
    return {name: db.execute(f"SELECT to_jsonb(t) FROM {name} t ORDER BY 1").fetchall()
            for name in ("practices", "encounters", "chase_items")}


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    with psycopg.connect(DSN) as db:
        assert db.info.dbname.startswith("cab_athena_schema_test_")
        assert db.execute("SELECT to_regclass('public.practices')").fetchone()[0] is None
    baseline = tmp_path_factory.mktemp("athena-baseline")
    for path in discover_migrations(MIGRATIONS):
        if int(path.name[:3]) not in NEW:
            (baseline / path.name).write_bytes(path.read_bytes())
    config = replace(Settings(), database_url=DSN, migrations_dir=str(baseline))
    assert apply_migrations(config) == [p.name for p in discover_migrations(baseline)]
    with psycopg.connect(DSN) as db:
        practice(db, "athena-baseline")
        db.execute("INSERT INTO practices(practice_id,display_name) VALUES('athena-baseline','Synthetic baseline')")
        db.execute("""INSERT INTO encounters(practice_id,external_id,patient_age,patient_sex,
            encounter_type,place_of_service,signed_note,source)
            VALUES('athena-baseline','baseline-note',44,'unknown','office_established','11',
            'Unchanged synthetic baseline.','manual')""")
        db.execute("""INSERT INTO chase_items(practice_id,chase_id,patient_ref,trigger,detected_at,state,bucket)
            VALUES('athena-baseline','baseline-case','baseline-patient','manual','2026-10-02','needs_review','missing')""")
        before = snapshot(db)
        old_ledger = db.execute("SELECT * FROM cosmaneura_schema_migrations ORDER BY name").fetchall()
    config = replace(config, migrations_dir=str(MIGRATIONS))
    assert apply_migrations(config) == [p.name for p in discover_migrations(MIGRATIONS) if int(p.name[:3]) in NEW]
    with psycopg.connect(DSN) as db:
        assert snapshot(db) == before
        ledger = db.execute("SELECT * FROM cosmaneura_schema_migrations ORDER BY name").fetchall()
        assert [row for row in ledger if int(row[0][:3]) not in NEW] == old_ledger
        assert {row[0]: row[1] for row in ledger} == {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in discover_migrations(MIGRATIONS)}
        # Last definition wins for functions intentionally replaced by later rungs.
        bodies = {}
        for p in discover_migrations(MIGRATIONS):
            if int(p.name[:3]) in NEW:
                bodies.update(re.findall(r"CREATE(?: OR REPLACE)? FUNCTION public\.(\w+)\(.*?AS \$\$(.*?)\$\$;",
                                         p.read_text(), re.S))
        table_names = ["athena_case_review_events", "athena_staff_connections",
                       "athena_staff_case_bindings", "athena_staff_read_admissions",
                       "provider_handoff_heads", "provider_handoff_operations",
                       "provider_source_connections", "provider_source_admissions",
                       "provider_source_bindings", "provider_source_acceptances"]
        for name in table_names:
            assert db.execute("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(%s)",
                              (name,)).fetchone() == (True, True)
        original = (MIGRATIONS / "019_provider_note_handoff.up.sql").read_text()
        bodies["cosmaneura_commit_provider_handoff_019"] = dict(re.findall(
            r"CREATE FUNCTION public\.(\w+)\(.*?AS \$\$(.*?)\$\$;", original, re.S))["cosmaneura_commit_provider_handoff"]
        for name, body in bodies.items():
            actual = db.execute("SELECT prosrc FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=%s",
                                (name,)).fetchone()
            assert actual and actual[0].strip() == body.strip()
    assert apply_migrations(config) == []
    with psycopg.connect(DSN) as db:
        assert snapshot(db) == before
        assert db.execute("SELECT * FROM cosmaneura_schema_migrations ORDER BY name").fetchall() == ledger
    return config, before, ledger


def test_exact_migration_ledger_replay_and_changed_checksum_rejection(schema, tmp_path):
    config, before, ledger = schema
    for path in discover_migrations(MIGRATIONS):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    altered = tmp_path / "014_athena_staff_review.up.sql"
    altered.write_bytes(altered.read_bytes() + b"\n-- deliberate local checksum drift\n")
    with pytest.raises(RuntimeError, match="applied migration checksum changed"):
        apply_migrations(replace(config, migrations_dir=str(tmp_path)))
    with psycopg.connect(DSN) as db:
        assert snapshot(db) == before
        assert db.execute("SELECT * FROM cosmaneura_schema_migrations ORDER BY name").fetchall() == ledger


@pytest.fixture
def scope(schema):
    suffix = uuid4().hex[:12]
    cfg = replace(Settings(), database_url=DSN, practice_id="athena-schema-é-" + suffix,
                  entra_tenant_id=str(uuid4()), bootstrap_admin_oid=str(uuid4()),
                  database_runtime_role="athena_schema_" + suffix,
                  database_runtime_password=uuid4().hex)
    with psycopg.connect(DSN) as db:
        _bootstrap_practice_admin(db, cfg)
        _provision_runtime_role(db, cfg)
        practice(db, cfg.practice_id)
        db.execute("""INSERT INTO chase_items(practice_id,chase_id,patient_ref,trigger,detected_at,state,bucket)
            VALUES(%s,'case','synthetic-patient-漢😀','manual','2026-10-02','needs_review','missing')""",
                   (cfg.practice_id,))
    return cfg


@contextmanager
def connection(cfg, *, runtime=False):
    kwargs = {"user": cfg.database_runtime_role, "password": cfg.database_runtime_password} if runtime else {}
    with psycopg.connect(DSN, **kwargs) as db:
        practice(db, cfg.practice_id)
        yield db


def reject(db, query, args=(), message=None):
    with pytest.raises(psycopg.Error, match=message):
        with db.transaction():
            db.execute(query, args)


def profile(cfg, revision=1, environment="production"):
    return AthenaStaffProfile(cfg.practice_id, environment, "195900" if environment == "preview" else "112233", revision)


def register_connection(db, cfg, p, fingerprint=None):
    db.execute("""INSERT INTO athena_staff_connections(practice_id,tenant_id,connection_revision,
        environment,vendor_practice_id,profile_sha256,review_ref) VALUES(%s,%s,%s,%s,%s,%s,'synthetic-test')""",
        (cfg.practice_id, cfg.entra_tenant_id, p.connection_revision, p.environment,
         p.vendor_practice_id, fingerprint or p.fingerprint))


def register_binding(db, cfg, p, revision=1, patient_ref="synthetic-patient-漢😀"):
    s = AthenaStaffScope(p, "case", patient_ref, "4455" if p.environment == "production" else "60178", revision)
    db.execute("""INSERT INTO athena_staff_case_bindings(practice_id,case_id,binding_revision,
        connection_revision,patient_ref,source_patient_id,scope_sha256,review_ref)
        VALUES(%s,%s,%s,%s,%s,%s,%s,'synthetic-test')""",
        (cfg.practice_id, s.case_id, revision, p.connection_revision, patient_ref, s.source_patient_id, s.fingerprint))
    return s


def grant_reader(db, cfg, *, review=False):
    role = sql.Identifier(cfg.database_runtime_role)
    for signature in ("cosmaneura_athena_staff_scope(uuid,uuid,text,text)",
                      "cosmaneura_athena_read_admission(uuid,uuid,text,text,text,text)",
                      "cosmaneura_athena_reviewer_allowed(uuid,uuid,text)"):
        db.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), role))
    if review:
        db.execute(sql.SQL("GRANT SELECT,INSERT ON athena_case_review_events TO {}").format(role))


def current_scope(db, cfg, role="Admin"):
    return db.execute("SELECT cosmaneura_athena_staff_scope(%s,%s,%s,'case')",
                      (cfg.entra_tenant_id, cfg.bootstrap_admin_oid, role)).fetchone()[0]


def admission(db, cfg, deployment="test-deployment"):
    return db.execute("SELECT cosmaneura_athena_read_admission(%s,%s,'Admin','case',%s,'test-storage')",
                      (cfg.entra_tenant_id, cfg.bootstrap_admin_oid, deployment)).fetchone()[0]


def prepare(cfg):
    with connection(cfg) as db:
        p = profile(cfg)
        register_connection(db, cfg, p)
        s = register_binding(db, cfg, p)
        grant_reader(db, cfg, review=True)
    return s


def registration(db, cfg, s, revision=1, duration="1 hour"):
    start, end = db.execute("SELECT clock_timestamp()-interval '1 minute',clock_timestamp()+%s::interval",
                            (duration,)).fetchone()
    return {"schemaVersion": "athena-staff-registration.v1", "serverPracticeId": cfg.practice_id,
            "tenantId": cfg.entra_tenant_id, "reviewRef": "synthetic-local-test", "admissionId": str(uuid4()),
            "admissionRevision": revision, "caseId": "case", "scopeSha256": s.fingerprint,
            "runtimeRole": cfg.database_runtime_role, "deploymentId": "test-deployment",
            "storageId": "test-storage", "notBefore": start.isoformat(), "expiresAt": end.isoformat()}


def register_admission(db, cfg, doc):
    db.execute("""INSERT INTO athena_staff_read_admissions(practice_id,case_id,admission_id,
        admission_revision,document) VALUES(%s,'case',%s,%s,%s)""",
        (cfg.practice_id, doc["admissionId"], doc["admissionRevision"], Jsonb(doc)))


def review_payload(cfg, s, proof, revision=1):
    binding = {"schemaVersion": "athena-source-binding.v1", "serverPracticeId": cfg.practice_id,
               "chaseId": "case", "environment": s.profile.environment, "vendorPracticeId": s.profile.vendor_practice_id,
               "sourcePatientId": s.source_patient_id, "connectionRevision": s.profile.connection_revision,
               "bindingRevision": s.binding_revision, "profileSha256": s.profile.fingerprint, "scopeSha256": s.fingerprint}
    source = {"system": "athena_production", "vendorPracticeId": s.profile.vendor_practice_id,
              "serverPracticeId": cfg.practice_id, "syntheticOnly": False,
              "dataClass": "operational", "patientId": s.source_patient_id}
    return {"schemaVersion": "athena-case-review.v3", "revision": revision, "chaseId": "case",
            "caseBinding": digest([cfg.practice_id, s.patient_ref], ascii=False), "mode": "production",
            "reviewId": str(uuid4()), "sourceBinding": binding, "sourceAdmission": proof,
            "candidate": {"sourceBinding": binding, "sourceAdmission": proof, "source": source, "patient": {}},
            "applied": None, "billingImport": None}


def insert_review(db, cfg, payload, *, audit=True):
    event = uuid4()
    if audit:
        db.execute("""INSERT INTO access_audit_attempts(practice_id,event_id,occurred_at,actor_tenant_id,
            actor_user_id,actor_roles,action,route,method,source_ip)
            VALUES(%s,%s,now(),%s,%s,ARRAY['Admin'],'athena.refresh','/synthetic-test','POST','local')""",
            (cfg.practice_id, event, cfg.entra_tenant_id, cfg.bootstrap_admin_oid))
        db.execute("""INSERT INTO access_audit_completions(practice_id,event_id,completed_at,status,outcome,resource_id)
            SELECT %s,%s,now(),200,'allowed',chase_uid::text FROM chase_items WHERE practice_id=%s AND chase_id='case'""",
            (cfg.practice_id, event, cfg.practice_id))
    db.execute("""INSERT INTO athena_case_review_events(practice_id,chase_id,revision,operation_id,request_sha256,
        payload,actor_tenant_id,actor_user_id,actor_role,access_event_id,action)
        VALUES(%s,'case',%s,%s,%s,%s,%s,%s,'Admin',%s,'athena.refresh')""",
        (cfg.practice_id, payload["revision"], payload["reviewId"], "a"*64, Jsonb(payload),
         cfg.entra_tenant_id, cfg.bootstrap_admin_oid, event))


def test_existing_runtime_has_no_new_grants_and_cannot_forge_provider_source(scope):
    with connection(scope, runtime=True) as db:
        assert db.execute("SELECT current_user=session_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone() == (True, False, False)
        for table in ("athena_case_review_events", "athena_staff_connections", "athena_staff_case_bindings",
                      "athena_staff_read_admissions", "provider_source_connections", "provider_source_admissions"):
            assert db.execute("SELECT has_table_privilege(current_user,%s,'SELECT,INSERT,UPDATE,DELETE')", (table,)).fetchone()[0] is False
        for signature in ("cosmaneura_athena_staff_scope(uuid,uuid,text,text)",
                          "cosmaneura_commit_provider_handoff(jsonb)", "cosmaneura_commit_provider_handoff_019(jsonb)"):
            assert db.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')", (signature,)).fetchone()[0] is False
        query = """INSERT INTO encounters(practice_id,external_id,patient_age,patient_sex,encounter_type,place_of_service,source)
            VALUES(%s,%s,44,'unknown','office_established','11',%s)"""
        db.execute(query, (scope.practice_id, "ordinary", "manual"))
        reject(db, query, (scope.practice_id, "forged", "provider_completed_note"), "provider source requires scoped receiver")
        reject(db, "UPDATE encounters SET source='provider_completed_note' WHERE external_id='ordinary'",
               message="provider source requires scoped receiver")


def test_registration_fingerprints_role_isolation_and_latest_binding(scope):
    with connection(scope) as db:
        p = profile(scope)
        with pytest.raises(psycopg.Error, match="fingerprint mismatch"):
            with db.transaction():
                register_connection(db, scope, p, "0"*64)
        register_connection(db, scope, p)
        with pytest.raises(psycopg.Error, match="case patient mismatch"):
            with db.transaction():
                register_binding(db, scope, p, patient_ref="different-patient")
        s = register_binding(db, scope, p)
        grant_reader(db, scope)
        # Even accidental registration table authority cannot replace owner checks.
        db.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE ON athena_staff_connections TO {}").format(sql.Identifier(scope.database_runtime_role)))
    with connection(scope, runtime=True) as db:
        assert current_scope(db, scope)["scopeSha256"] == s.fingerprint
        assert current_scope(db, scope, "Coder") == {"error": "not_authorized"}
        with pytest.raises(psycopg.Error, match="migration owner"):
            with db.transaction():
                register_connection(db, scope, profile(scope, 2))
        practice(db, "athena-baseline")
        assert current_scope(db, scope) == {"error": "not_authorized"}
        assert db.execute("SELECT count(*) FROM athena_staff_connections").fetchone()[0] == 0
    with connection(scope) as db:
        p2 = profile(scope, 2)
        register_connection(db, scope, p2)
    with connection(scope, runtime=True) as db:
        assert current_scope(db, scope) == {"error": "binding_stale"}
    with connection(scope) as db:
        register_binding(db, scope, p2, revision=2)
        db.execute("UPDATE athena_staff_case_bindings SET revocation_ref='synthetic-revoke' WHERE practice_id=%s AND binding_revision=2", (scope.practice_id,))
    with connection(scope, runtime=True) as db:
        assert current_scope(db, scope) == {"error": "binding_revoked"}


def test_admission_is_closed_bound_to_session_and_revocation_serializes(scope):
    s = prepare(scope)
    with connection(scope) as db:
        doc = registration(db, scope, s)
        for key in ("scopeSha256", "runtimeRole", "notBefore", "expiresAt"):
            bad = {k: v for k, v in doc.items() if k != key}
            with pytest.raises(psycopg.Error, match="registration invalid"):
                with db.transaction():
                    register_admission(db, scope, bad)
        register_admission(db, scope, doc)
        row = db.execute("SELECT database_name,grant_sha256 FROM athena_staff_read_admissions WHERE practice_id=%s", (scope.practice_id,)).fetchone()
        assert row == (db.info.dbname, digest([doc, db.info.dbname]))
        assert admission(db, scope) == {"error": "read_not_admitted"}  # owner is not runtime session
    with connection(scope, runtime=True) as db:
        proof = admission(db, scope)
        assert proof["scopeSha256"] == s.fingerprint and proof["grantSha256"] == row[1]
        assert admission(db, scope, "different-deployment") == {"error": "read_not_admitted"}
        with connection(scope) as owner:
            owner.execute("SET LOCAL lock_timeout='100ms'")
            reject(owner, "UPDATE athena_staff_read_admissions SET revocation_ref='synthetic-revoke' WHERE practice_id=%s",
                   (scope.practice_id,), "lock timeout")
    with connection(scope) as db:
        db.execute("UPDATE athena_staff_read_admissions SET revocation_ref='synthetic-revoke' WHERE practice_id=%s", (scope.practice_id,))
    with connection(scope, runtime=True) as db:
        assert admission(db, scope) == {"error": "read_not_admitted"}


@pytest.mark.parametrize("defect", ["missing_audit", "source_binding", "source_admission", "revision"])
def test_review_rejects_missing_audit_or_contradictory_provenance(scope, defect):
    s = prepare(scope)
    with connection(scope) as db:
        register_admission(db, scope, registration(db, scope, s))
    with pytest.raises(psycopg.Error):
        with connection(scope, runtime=True) as db:
            payload = review_payload(scope, s, admission(db, scope))
            if defect == "source_binding":
                payload["sourceBinding"] = {**payload["sourceBinding"], "sourcePatientId": "999"}
            elif defect == "source_admission":
                payload["sourceAdmission"] = None
            elif defect == "revision":
                payload["revision"] = "1"
            insert_review(db, scope, payload, audit=defect != "missing_audit")
    with connection(scope) as db:
        assert db.execute("SELECT count(*) FROM athena_case_review_events WHERE practice_id=%s", (scope.practice_id,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM access_audit_completions WHERE practice_id=%s", (scope.practice_id,)).fetchone()[0] == 0


def test_review_commits_with_audit_then_rejects_replay_mutation_and_downgrade(scope):
    s = prepare(scope)
    with connection(scope) as db:
        register_admission(db, scope, registration(db, scope, s))
    with connection(scope, runtime=True) as db:
        payload = review_payload(scope, s, admission(db, scope))
        insert_review(db, scope, payload)
    with connection(scope, runtime=True) as db:
        assert db.execute("SELECT payload FROM athena_case_review_events").fetchone()[0] == payload
        with pytest.raises(psycopg.Error):
            with db.transaction():
                insert_review(db, scope, payload)
        downgrade = {**payload, "schemaVersion": "athena-case-review.v2", "revision": 2, "reviewId": str(uuid4())}
        with pytest.raises(psycopg.Error, match="cannot downgrade"):
            with db.transaction():
                insert_review(db, scope, downgrade)
    with connection(scope) as db:
        reject(db, "UPDATE athena_case_review_events SET request_sha256=%s WHERE practice_id=%s", ("b"*64, scope.practice_id), "immutable")
        reject(db, "DELETE FROM athena_case_review_events WHERE practice_id=%s", (scope.practice_id,), "immutable")


def test_admission_expiry_before_commit_rolls_back_review_and_audit(scope):
    s = prepare(scope)
    with connection(scope) as db:
        register_admission(db, scope, registration(db, scope, s, duration="2 seconds"))
    with pytest.raises(psycopg.Error, match="expired before persistence"):
        with connection(scope, runtime=True) as db:
            proof = admission(db, scope)
            assert "error" not in proof
            insert_review(db, scope, review_payload(scope, s, proof))
            db.execute("SELECT pg_sleep(2.1)")
    with connection(scope) as db:
        assert db.execute("SELECT count(*) FROM athena_case_review_events WHERE practice_id=%s", (scope.practice_id,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM access_audit_completions WHERE practice_id=%s", (scope.practice_id,)).fetchone()[0] == 0
