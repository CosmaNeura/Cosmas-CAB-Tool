"""Reusable, local-only registered Preview fixture; no credential/environment reads."""
from dataclasses import replace
from pathlib import Path
import re
from uuid import uuid4
import psycopg
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.athena_bindings_cli import apply
from cosmaneura_billing.athena_client import AthenaStaffReadClient, MockAthenaStaffReadClient
from cosmaneura_billing.athena_review import ReviewActor, now
from cosmaneura_billing.athena_review_routes import make_athena_service
from cosmaneura_billing.migrations import _provision_runtime_role
from cosmaneura_billing.persistence import PostgresContext
from cosmaneura_billing.settings import Settings

CASE = "configured-synthetic-case"
PACKAGE = Path(__file__).resolve().parents[1]


def prepare_schema(dsn):
    with psycopg.connect(dsn, autocommit=True) as db:
        assert db.info.host == "127.0.0.1" and db.info.port == 32800
        assert db.info.dbname.startswith("cab_athena_configured_api_test_")
        # The locked runner applies the complete sparse chain before tests.
        assert db.execute("SELECT count(*) FROM cosmaneura_schema_migrations").fetchone()[0] == 17
        expected = {}
        for number in (21,22,23):
            path = next((PACKAGE / "migrations").glob(f"{number:03}*.up.sql"))
            for name,body in re.findall(r"CREATE (?:OR REPLACE )?FUNCTION public\.(\w+)\(.*?AS \$\$(.*?)\$\$;",path.read_text(),re.S):
                expected[name] = body
        for name,body in expected.items():
            actual=db.execute("SELECT prosrc FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=%s",(name,)).fetchone()
            assert actual and actual[0].strip()==body.strip(), "dedicated schema differs from current migration"



def registration(owner, action="register-connection", **values):
    base={"schemaVersion":"athena-staff-registration.v1","serverPracticeId":owner.practice_id,
          "tenantId":owner.entra_tenant_id,"reviewRef":"authored-local-synthetic-fixture"}
    defaults={"register-connection":{"environment":"preview","vendorPracticeId":"195900","connectionRevision":1},
        "register-binding":{"caseId":CASE,"patientRef":"opaque-synthetic-case","sourcePatientId":"60178","connectionRevision":1,"bindingRevision":1},
        "revoke-connection":{"connectionRevision":1},"revoke-binding":{"caseId":CASE,"bindingRevision":1}}
    apply(owner,action,{**base,**defaults[action],**values})


def fake_client(scope):
    mock=MockAthenaStaffReadClient()
    return AthenaStaffReadClient(scope=scope,credential_environment="preview",
                                 token_provider=mock._provider,urlopen=mock._open)


def prepare(dsn, *, role="Admin", register=True, practice_suffix=""):
    practice,tenant,user="configured-"+uuid4().hex+practice_suffix,str(uuid4()),str(uuid4())
    runtime="athena_configured_"+uuid4().hex[:14]
    owner=Settings(environment="test",database_url=dsn,practice_id=practice,entra_tenant_id=tenant,
        database_runtime_role=runtime,database_runtime_password="authored-local-test-only")
    with psycopg.connect(dsn) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES(%s,%s::uuid)",(practice,tenant))
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES(%s::uuid,%s::uuid)",(tenant,user))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s::uuid,%s::uuid,%s)",(practice,tenant,user,[role]))
        db.execute("INSERT INTO chase_items(practice_id,chase_id,patient_ref,trigger,detected_at,state,bucket) VALUES(%s,%s,'opaque-synthetic-case','manual','2026-09-30','needs_review','missing')",(practice,CASE))
        resource=str(db.execute("SELECT chase_uid FROM chase_items WHERE practice_id=%s AND chase_id=%s",(practice,CASE)).fetchone()[0])
        _provision_runtime_role(db,owner)
    context=PostgresContext(make_conninfo(dsn,user=runtime),practice_id=practice)
    actor=ReviewActor(tenant,user,role)
    service=make_athena_service(mode="configured",practice_id=practice,chase_store=None,database_context=context,
        credential_environment="preview",configured_client_factory=fake_client,signing_key=b"local-synthetic-discovery-key-0001")
    if register:
        registration(owner); registration(owner,"register-binding")
    def audit(action):
        event=AccessAuditEvent(str(uuid4()),now(),practice,tenant,user,(role,),action,resource,
            "/api/athena/rehearsal","POST",200,"allowed","127.0.0.1")
        with context.connection() as db:
            db.execute("INSERT INTO access_audit_attempts(practice_id,event_id,occurred_at,actor_tenant_id,actor_user_id,actor_roles,action,route,method,source_ip) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'POST','127.0.0.1')",
                (practice,event.event_id,event.occurred_at,tenant,user,[role],action,event.route))
        return event
    return owner,context,actor,service,audit


def app_settings(owner,context,frontend):
    return replace(owner,state_store="postgres",database_url=context.dsn,chase_database_url=context.dsn,
        chase_store="postgres",billing_header_enabled=True,athena_review_mode="configured",
        athena_credential_environment="preview",athena_client_id="injected-fixture-only",athena_client_secret="injected-fixture-only",
        auth_mode="container_apps",allowed_roles=("Admin",),frontend_dist=str(frontend),
        review_context_signing_key="local-synthetic-review-key-0000000000000000000000000000000000000000",
        azure_openai_enabled=False,azure_language_enabled=False,telemetry_enabled=False)
