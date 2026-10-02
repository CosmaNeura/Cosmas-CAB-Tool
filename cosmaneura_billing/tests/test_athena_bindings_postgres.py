"""Opt-in, dedicated local PostgreSQL proof for operator-owned identity bindings."""
from dataclasses import replace
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.athena_bindings import AthenaBindingError, PostgresAthenaBindingStore
from cosmaneura_billing.athena_bindings_cli import apply
from cosmaneura_billing.athena_client import AthenaStaffReadClient, AthenaOperationUnavailable
from cosmaneura_billing.athena_review import ReviewActor
from cosmaneura_billing.athena_staff_contract import AthenaStaffProfile
from cosmaneura_billing.migrations import _provision_runtime_role, _provision_chase_worker_role
from cosmaneura_billing.persistence import PostgresContext, PersistenceUnavailable
from cosmaneura_billing.settings import Settings

DSN = os.environ.get("CAB_ATHENA_BINDINGS_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable Athena binding database required")
PACKAGE = Path(__file__).resolve().parents[1]
MIGRATION = Path(os.environ.get("CAB_ATHENA_BINDING_MIGRATION", str(PACKAGE / "migrations/021_athena_staff_bindings.up.sql")))
TABLES = ("athena_staff_connections", "athena_staff_case_bindings")
FUNCTION = "public.cosmaneura_athena_staff_scope(uuid,uuid,text,text)"


@pytest.fixture(scope="module", autouse=True)
def schema():
    with psycopg.connect(DSN, autocommit=True) as db:
        assert db.info.host == "127.0.0.1" and db.info.port == 32800
        assert db.info.dbname.startswith("cab_athena_configured_api_test_")
        assert db.execute("SELECT count(*) FROM cosmaneura_schema_migrations").fetchone()[0] == 17
        # Never validate an earlier function while final migration bytes changed.
        for name, body in re.findall(r"CREATE FUNCTION public\.(\w+)\(.*?AS \$\$(.*?)\$\$;", MIGRATION.read_text(), re.S):
            actual = db.execute("SELECT prosrc FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=%s", (name,)).fetchone()
            assert actual and actual[0].strip() == body.strip(), "rehearsal schema differs from final 021"


@pytest.fixture
def configured():
    practice, tenant, user = "binding-"+uuid4().hex, str(uuid4()), str(uuid4())
    role = "athena_binding_"+uuid4().hex[:16]
    owner = Settings(environment="test", database_url=DSN, practice_id=practice, entra_tenant_id=tenant,
        database_runtime_role=role, database_runtime_password="authored-local-test-only")
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES(%s,%s::uuid)", (practice,tenant))
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES(%s::uuid,%s::uuid)", (tenant,user))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s::uuid,%s::uuid,ARRAY['BillingStaff'])", (practice,tenant,user))
        db.execute("INSERT INTO chase_items(practice_id,chase_id,patient_ref,trigger,detected_at,state,bucket) VALUES(%s,'case-🧪','opaque-Café-🧪','manual','2026-09-30','needs_review','missing')", (practice,))
        _provision_runtime_role(db, owner)
    context = PostgresContext(make_conninfo(DSN,user=role), practice_id=practice)
    return owner, context, ReviewActor(tenant,user,"BillingStaff")


def manifest(owner, action="register-connection", **changes):
    fields = {"register-connection":{"environment":"production","vendorPracticeId":"112233","connectionRevision":1},
        "register-binding":{"caseId":"case-🧪","patientRef":"opaque-Café-🧪","sourcePatientId":"4455","connectionRevision":1,"bindingRevision":1},
        "revoke-connection":{"connectionRevision":1},"revoke-binding":{"caseId":"case-🧪","bindingRevision":1}}
    return {"schemaVersion":"athena-staff-registration.v1","serverPracticeId":owner.practice_id,
            "tenantId":owner.entra_tenant_id,"reviewRef":"synthetic-private-review",**fields[action],**changes}


def register(owner, action="register-connection", **changes):
    apply(owner,action,manifest(owner,action,**changes))


def resolve(context, actor):
    with PostgresAthenaBindingStore(context).scope("case-🧪",actor) as scope: return scope


def bound(configured):
    owner, context, actor = configured
    register(owner); register(owner,"register-binding")
    return owner, context, actor


def test_empty_then_exact_binding_replicas_and_production_remains_closed(configured):
    owner, context, actor = configured
    PostgresAthenaBindingStore(context).healthcheck()
    with pytest.raises(AthenaBindingError,match="connection_missing"): resolve(context,actor)
    register(owner)
    with pytest.raises(AthenaBindingError,match="binding_missing"): resolve(context,actor)
    register(owner,"register-binding")
    scope=resolve(context,actor)
    assert scope.source_patient_id=="4455" and scope.patient_ref=="opaque-Café-🧪"
    assert resolve(PostgresContext(context.dsn,practice_id=context.practice_id),actor)==scope
    assert scope.profile.server_practice_id==owner.practice_id and scope.profile.environment=="production"
    with pytest.raises(AthenaOperationUnavailable):
        AthenaStaffReadClient(scope=scope,credential_environment="production",token_provider=lambda:pytest.fail("no token access"))
    with psycopg.connect(DSN) as db:
        persisted=db.execute("SELECT profile_sha256,scope_sha256 FROM athena_staff_connections JOIN athena_staff_case_bindings USING(practice_id,connection_revision) WHERE practice_id=%s",(owner.practice_id,)).fetchone()
        assert persisted==(scope.profile.fingerprint,scope.fingerprint)


def test_runtime_and_worker_cannot_register_read_tables_or_mutate_history(configured):
    owner, context, actor=bound(configured)
    with pytest.raises(PermissionError): apply(replace(owner,database_url=context.dsn),"register-connection",manifest(owner,connectionRevision=2))
    with context.connection() as db:
        for table in TABLES:
            for permission in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE"):
                assert not db.execute("SELECT has_table_privilege(current_user,%s,%s) AS ok",(table,permission)).fetchone()["ok"]
        assert db.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE') AS ok",(FUNCTION,)).fetchone()["ok"]
    with pytest.raises(PersistenceUnavailable):
        with context.connection() as db: db.execute("SELECT * FROM athena_staff_case_bindings")
    worker="athena_binding_worker_"+uuid4().hex[:12]
    with psycopg.connect(DSN) as db:
        _provision_chase_worker_role(db,replace(owner,chase_worker_database_role=worker,chase_worker_database_password="authored-local-test-only"))
    with psycopg.connect(make_conninfo(DSN,user=worker)) as db:
        assert not db.execute("SELECT has_function_privilege(current_user,%s,'EXECUTE')",(FUNCTION,)).fetchone()[0]
        for table in TABLES: assert not db.execute("SELECT has_table_privilege(current_user,%s,'SELECT,INSERT,UPDATE,DELETE')",(table,)).fetchone()[0]


def test_current_membership_practice_and_case_are_required(configured):
    owner, context, actor=bound(configured)
    for invalid in (ReviewActor(str(uuid4()),actor.user_id,"BillingStaff"),ReviewActor(actor.tenant_id,str(uuid4()),"BillingStaff"),ReviewActor(actor.tenant_id,actor.user_id,"Admin")):
        with pytest.raises(AthenaBindingError,match="not_authorized"): resolve(context,invalid)
    with pytest.raises(AthenaBindingError,match="not_authorized"):
        resolve(PostgresContext(context.dsn,practice_id="another-practice"),actor)
    with pytest.raises(AthenaBindingError,match="not_found"):
        with PostgresAthenaBindingStore(context).scope("absent-case",actor): pass
    with psycopg.connect(DSN) as db: db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s",(owner.practice_id,))
    with pytest.raises(AthenaBindingError,match="not_authorized"): resolve(context,actor)


def test_monotonic_revisions_revocations_and_no_old_mapping_fallback(configured):
    owner, context, actor=bound(configured)
    original=resolve(context,actor)
    for revision in (1,3):
        with pytest.raises(psycopg.Error): register(owner,connectionRevision=revision)
    register(owner,connectionRevision=2,vendorPracticeId="223344")
    with pytest.raises(AthenaBindingError,match="binding_stale"): resolve(context,actor)
    with pytest.raises(ValueError): register(owner,"register-binding",bindingRevision=2)
    register(owner,"register-binding",connectionRevision=2,bindingRevision=2,sourcePatientId="4456")
    assert resolve(context,actor).fingerprint!=original.fingerprint
    register(owner,"revoke-binding",bindingRevision=2)
    with pytest.raises(AthenaBindingError,match="binding_revoked"): resolve(context,actor)
    with pytest.raises(ValueError): register(owner,"revoke-binding",bindingRevision=2)
    register(owner,"register-binding",connectionRevision=2,bindingRevision=3,sourcePatientId="4456")
    register(owner,"revoke-connection",connectionRevision=2)
    with pytest.raises(AthenaBindingError,match="connection_revoked"): resolve(context,actor)
    register(owner,connectionRevision=3)
    with pytest.raises(AthenaBindingError,match="binding_stale"): resolve(context,actor)


def test_changed_opaque_case_patient_needs_new_exact_operator_mapping(configured):
    owner,context,actor=bound(configured)
    with psycopg.connect(DSN) as db: db.execute("UPDATE chase_items SET patient_ref='new-opaque-patient' WHERE practice_id=%s",(owner.practice_id,))
    with pytest.raises(AthenaBindingError,match="binding_stale"): resolve(context,actor)
    with pytest.raises(psycopg.Error,match="patient mismatch"): register(owner,"register-binding",bindingRevision=2)
    register(owner,"register-binding",bindingRevision=2,patientRef="new-opaque-patient",sourcePatientId="4456")
    assert resolve(context,actor).source_patient_id=="4456"


def test_owner_cannot_rewrite_evidence_or_forge_fingerprints(configured):
    owner,context,actor=bound(configured)
    for table in TABLES:
        for statement in (f"UPDATE {table} SET review_ref='changed' WHERE practice_id=%s",f"DELETE FROM {table} WHERE practice_id=%s"):
            with pytest.raises(psycopg.Error,match="immutable"):
                with psycopg.connect(DSN) as db:
                    db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
                    db.execute(statement,(owner.practice_id,))
    with pytest.raises(psycopg.Error,match="immutable"):
        with psycopg.connect(DSN) as db: db.execute("TRUNCATE athena_staff_connections,athena_staff_case_bindings")
    with pytest.raises(psycopg.Error,match="fingerprint mismatch"):
        with psycopg.connect(DSN) as db:
            db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
            db.execute("""INSERT INTO athena_staff_connections(practice_id,tenant_id,connection_revision,environment,vendor_practice_id,profile_sha256,review_ref)
                VALUES(%s,%s,2,'production','112233',%s,'synthetic-review')""",(owner.practice_id,actor.tenant_id,"0"*64))
    with pytest.raises(psycopg.Error,match="fingerprint mismatch"):
        with psycopg.connect(DSN) as db:
            db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
            db.execute("""INSERT INTO athena_staff_case_bindings(practice_id,case_id,binding_revision,
                connection_revision,patient_ref,source_patient_id,scope_sha256,review_ref)
                VALUES(%s,'case-🧪',2,1,'opaque-Café-🧪','4456',%s,'synthetic-review')""",(owner.practice_id,"0"*64))
    assert resolve(context,actor).profile.connection_revision==1


def test_accidental_table_grant_still_cannot_register_and_readiness_detects_it(configured):
    owner,context,actor=bound(configured)
    role=sql.Identifier(owner.database_runtime_role)
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("GRANT INSERT ON athena_staff_connections TO {}").format(role))
    try:
        with pytest.raises(AthenaBindingError): PostgresAthenaBindingStore(context).healthcheck()
        with pytest.raises(PersistenceUnavailable) as failure:
            with context.connection() as db:
                db.execute("""INSERT INTO athena_staff_connections(practice_id,tenant_id,connection_revision,
                    environment,vendor_practice_id,profile_sha256,review_ref)
                    VALUES(%s,%s,2,'production','112233',%s,'synthetic-review')""",(owner.practice_id,actor.tenant_id,"0"*64))
        assert failure.value.__cause__.diag.message_primary=="Athena registration requires migration owner"
    finally:
        with psycopg.connect(DSN) as db:
            db.execute(sql.SQL("REVOKE INSERT ON athena_staff_connections FROM {}").format(role))
    PostgresAthenaBindingStore(context).healthcheck()


def test_scope_locks_new_revisions_revocation_case_and_membership_until_transaction_ends(configured):
    owner,context,actor=bound(configured)
    profile=AthenaStaffProfile(owner.practice_id,"production","112233",2)
    actions=[("UPDATE athena_staff_connections SET revocation_ref='synthetic-revoke' WHERE practice_id=%s",(owner.practice_id,)),
        ("UPDATE chase_items SET patient_ref='other' WHERE practice_id=%s",(owner.practice_id,)),
        ("UPDATE practice_memberships SET active=false WHERE practice_id=%s",(owner.practice_id,)),
        ("""INSERT INTO athena_staff_connections(practice_id,tenant_id,connection_revision,environment,vendor_practice_id,profile_sha256,review_ref)
             VALUES(%s,%s,2,'production','112233',%s,'synthetic-review')""",(owner.practice_id,actor.tenant_id,profile.fingerprint))]
    with PostgresAthenaBindingStore(context).scope("case-🧪",actor):
        for query,args in actions:
            with pytest.raises(psycopg.errors.LockNotAvailable):
                with psycopg.connect(DSN) as db:
                    db.execute("SET LOCAL lock_timeout='100ms'")
                    db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
                    db.execute(query,args)
    register(owner,connectionRevision=2)
    with pytest.raises(AthenaBindingError,match="binding_stale"): resolve(context,actor)


def test_snapshot_isolation_and_lost_execute_privilege_fail_closed(configured):
    owner,context,actor=bound(configured)
    with psycopg.connect(context.dsn, row_factory=context.dict_row) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
        with pytest.raises(AthenaBindingError,match="binding_invalid"):
            PostgresAthenaBindingStore(context).in_transaction(db,"case-🧪",actor)
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION public.cosmaneura_athena_staff_scope(uuid,uuid,text,text) FROM {}").format(sql.Identifier(owner.database_runtime_role)))
    with pytest.raises(AthenaBindingError): PostgresAthenaBindingStore(context).healthcheck()
