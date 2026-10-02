"""Optional disposable PostgreSQL rehearsal with a restricted runtime login."""
import os
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import make_conninfo

from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.athena_client import AthenaStaffReadClient, MockAthenaStaffReadClient
from cosmaneura_billing.athena_review import AthenaReviewError, AthenaReviewService, ReviewActor, now
from cosmaneura_billing.athena_review_store import PostgresAthenaReviewStore
from cosmaneura_billing.persistence import PersistenceUnavailable, PostgresContext, PostgresEncounterStore
from test_athena_review import apply_command, billing_command, refresh_command

DSN = os.environ.get("CAB_ATHENA_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable Athena database absent")


@pytest.fixture
def setup():
    with psycopg.connect(DSN, autocommit=True) as db:
        assert (db.info.dbname.startswith("cab_athena_review_core_test_")
                and db.info.host == "127.0.0.1" and db.info.port == 32800)
        if db.execute("SELECT to_regclass('public.athena_case_review_events')").fetchone()[0] is None:
            for path in sorted((Path(__file__).resolve().parents[1]/"migrations").glob("*.up.sql")):
                number = int(path.name.split("_",1)[0])
                if 2 <= number <= 11 or number == 14:
                    db.execute(path.read_text(), prepare=False)
    practice, tenant, user = "athena-"+uuid4().hex, str(uuid4()), str(uuid4())
    role = "athena_review_"+uuid4().hex[:16]
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES(%s,%s)",(practice,tenant))
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES(%s,%s)",(tenant,user))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s,%s,ARRAY['BillingStaff'])",(practice,tenant,user))
        db.execute("INSERT INTO runtime_practice_bindings(role_name,practice_id) VALUES(%s,%s)",(role,practice))
        db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("GRANT SELECT,UPDATE ON chase_items TO {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("GRANT SELECT,INSERT ON athena_case_review_events,chase_contact_snapshots,chase_contact_routes,encounters,access_audit_attempts,access_audit_completions TO {}").format(sql.Identifier(role)))
        db.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_practice_allowed(text),cosmaneura_athena_reviewer_allowed(uuid,uuid,text) TO {}").format(sql.Identifier(role)))
        db.execute("INSERT INTO chase_items(practice_id,chase_id,patient_ref,trigger,detected_at,state,priority,bucket) VALUES(%s,'case','invented-patient','manual','2026-09-29','needs_review',40,'missing')",(practice,))
        resource = str(db.execute("SELECT chase_uid FROM chase_items WHERE practice_id=%s",(practice,)).fetchone()[0])
    context = PostgresContext(make_conninfo(DSN,user=role),practice_id=practice)
    service = AthenaReviewService(PostgresAthenaReviewStore(context),MockAthenaStaffReadClient())
    actor = ReviewActor(tenant,user,"BillingStaff")
    def audit(action):
        event = AccessAuditEvent(str(uuid4()),now(),practice,tenant,user,("BillingStaff",),
            action,resource,"/api/athena/rehearsal","POST",200,"allowed","127.0.0.1")
        with context.connection() as db:
            db.execute("INSERT INTO access_audit_attempts(practice_id,event_id,occurred_at,actor_tenant_id,actor_user_id,actor_roles,action,route,method,source_ip) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'POST','127.0.0.1')",
                (practice,event.event_id,event.occurred_at,tenant,user,["BillingStaff"],action,event.route))
        return event
    return service, actor, audit, role, context


@pytest.mark.parametrize("mode", ["mock", "preview"])
def test_two_replicas_review_contact_and_header_import_atomic_audit_and_rls(setup,mode):
    service, actor, audit, role, context = setup
    if mode == "preview":
        mock = MockAthenaStaffReadClient()
        service.client = AthenaStaffReadClient(token_provider=mock._provider, urlopen=mock._open)
    replica = AthenaReviewService(PostgresAthenaReviewStore(context),service.client)
    source = service.refresh("case",actor,refresh_command(),audit=audit("athena.refresh"))
    command = apply_command(source)
    applied = replica.apply("case",actor,command,audit=audit("athena.apply"))
    assert service.current("case",actor) == applied
    with context.connection() as db:
        rows = db.execute("SELECT channel,consent_status FROM chase_contact_routes").fetchall()
        assert {r["channel"] for r in rows} == {"sms","phone","email"}
        assert all(r["consent_status"] == "unknown" for r in rows)
        assert db.execute("SELECT state FROM chase_items").fetchone()["state"] == "needs_review"
    note = service.billing_read("case",actor,department_id="1",encounter_id="88001")["note"]
    billing = billing_command(applied,note)
    imported = service.billing_apply("case",actor,billing,audit=audit("athena.billing_apply"))
    encounter = PostgresEncounterStore(context).get(imported["billingImport"]["encounterId"])
    assert encounter.bundle.signed_note == note["plainText"] and encounter.source == ("athena_staff_review_mock" if mode == "mock" else "athena_staff_review_preview_synthetic")
    assert replica.billing_apply("case",actor,billing,audit=audit("athena.billing_apply")) == imported
    with pytest.raises(AthenaReviewError,match="stale_review"):
        replica.apply("case",actor,{**command,"operationId":str(uuid4())},audit=audit("athena.apply"))
    # Revoked audit INSERT prevents both a new evidence event and contact rows.
    event = audit("athena.refresh")
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("REVOKE INSERT ON access_audit_completions FROM {}").format(sql.Identifier(role)))
    with pytest.raises(PersistenceUnavailable):
        service.refresh("case",actor,refresh_command(3),audit=event)
    assert service.current("case",actor)["revision"] == 3
    other = AthenaReviewService(PostgresAthenaReviewStore(PostgresContext(context.dsn,practice_id="other")),MockAthenaStaffReadClient())
    with pytest.raises(AthenaReviewError,match="not_authorized"):
        other.current("case",actor)
    # Membership revocation is rechecked in the database, including reads.
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s",(context.practice_id,))
    with pytest.raises(AthenaReviewError,match="not_authorized"):
        service.current("case",actor)


@pytest.mark.parametrize("source", ["athena_staff_review_preview_synthetic", "athena_staff_review_mock"])
def test_reserved_source_cannot_be_inserted_directly_without_same_transaction_receipt(setup,source):
    _service, actor, _audit, _role, context = setup
    with pytest.raises(PersistenceUnavailable) as failure:
        with context.connection() as db:
            db.execute("INSERT INTO encounters(practice_id,external_id,patient_age,patient_sex,encounter_type,place_of_service,signed_note,source,ingested_by_tenant_id,ingested_by_user_id) VALUES(%s,'forgery',30,'F','office_established','11','invented',%s,%s,%s)",
                (context.practice_id,source,actor.tenant_id,actor.user_id))
    assert failure.value.__cause__.diag.message_primary == "reviewed Athena source requires its import receipt"
    assert PostgresEncounterStore(context).get("forgery") is None


def test_review_payload_immutable_even_owner_and_audit_identity_required(setup):
    service, actor, audit, _role, context = setup
    with pytest.raises(AthenaReviewError,match="audit_required"):
        service.refresh("case",actor,refresh_command())
    assert service.current("case",actor)["revision"] == 0
    service.refresh("case",actor,refresh_command(),audit=audit("athena.refresh"))
    with pytest.raises(psycopg.Error):
        with psycopg.connect(DSN) as db:
            db.execute("UPDATE athena_case_review_events SET revision=2 WHERE practice_id=%s",(context.practice_id,))


@pytest.mark.parametrize("tamper", ["note", "cohort"])
def test_source_guard_rejects_changed_note_or_unapproved_patient_and_rolls_back(setup,monkeypatch,tamper):
    from dataclasses import replace
    from cosmaneura_billing.athena_review_store import _PostgresUnit
    service, actor, audit, _role, context = setup
    source = service.refresh("case",actor,refresh_command(),audit=audit("athena.refresh"))
    applied = service.apply("case",actor,apply_command(source),audit=audit("athena.apply"))
    note = service.billing_read("case",actor,department_id="1",encounter_id="88001")["note"]
    original = _PostgresUnit.save
    def corrupted(unit,view,operation,intent,actor,event,action,contacts=None,encounter=None):
        if encounter:
            if tamper == "note":
                encounter = replace(encounter,signed_note="Different synthetic text")
            else:
                view["candidate"]["source"]["patientId"] = "999"
                view["billingImport"]["sourcePatientId"] = "999"
        return original(unit,view,operation,intent,actor,event,action,contacts,encounter)
    monkeypatch.setattr(_PostgresUnit,"save",corrupted)
    with pytest.raises(PersistenceUnavailable) as failure:
        service.billing_apply("case",actor,billing_command(applied,note),audit=audit("athena.billing_apply"))
    assert failure.value.__cause__.diag.message_primary == "reviewed Athena source requires its import receipt"
    assert service.current("case",actor)["revision"] == 2
    assert not PostgresEncounterStore(context).all()


@pytest.mark.parametrize("privilege", ["SELECT", "INSERT"])
def test_healthcheck_requires_both_read_and_write_grants(setup,privilege):
    service,_actor,_audit,role,_context=setup
    service.store.healthcheck()
    with psycopg.connect(DSN) as db:
        db.execute(sql.SQL("REVOKE {} ON athena_case_review_events FROM {}").format(sql.SQL(privilege),sql.Identifier(role)))
    with pytest.raises(AthenaReviewError,match="source_unavailable"):
        service.store.healthcheck()
