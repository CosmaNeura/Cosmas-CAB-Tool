"""Opt-in rehearsal on the named disposable PG; no deployed database access."""
from copy import deepcopy
from dataclasses import asdict, replace
import os
from pathlib import Path
from uuid import uuid4

import pytest
psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.billing_header import HeaderActor, canonical, digest, source_snapshot, utc_now
from cosmaneura_billing.migrations import _provision_runtime_role
from cosmaneura_billing.persistence import PostgresAccessAuditStore, PostgresContext, PostgresEncounterStore, PersistenceUnavailable
from cosmaneura_billing.provider_handoff import HandoffError, prepare_handoff, HandoffRequest
from cosmaneura_billing.provider_handoff_store import PostgresHandoffStore
from cosmaneura_billing.settings import Settings
from test_provider_handoff import envelope
from provider_admission_support import source_for, registration
from cosmaneura_billing.provider_admission_cli import register

DSN = os.environ.get("CAB_PLATFORM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable integration database is absent")


@pytest.fixture
def context():
    with psycopg.connect(DSN, autocommit=True) as db:
        assert db.info.dbname in {"cab_provider_receiver_test_20261002_r1", "cab_provider_model_test_20261002_r1"} and db.info.host == "127.0.0.1" and db.info.port == 32800
        # Full sparse chain is applied by the locked runner before the suite.
        import hashlib
        paths = sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.up.sql"))
        assert len(paths) == 19
        assert dict(db.execute("SELECT name,checksum_sha256 FROM cosmaneura_schema_migrations").fetchall()) == {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        import re
        source = (Path(__file__).resolve().parents[1] / "migrations/019_provider_note_handoff.up.sql").read_text().replace("FUNCTION public.cosmaneura_commit_provider_handoff(", "FUNCTION public.cosmaneura_commit_provider_handoff_019(")
        source += (Path(__file__).resolve().parents[1] / "migrations/020_provider_source_admission.up.sql").read_text()
        source += (Path(__file__).resolve().parents[1] / "migrations/024_header_visit_context.up.sql").read_text()
        bodies = dict(re.findall(r"CREATE(?: OR REPLACE)? FUNCTION public\.(\w+)\(.*?AS \$\$(.*?)\$\$;",source,re.S))
        for name, body in bodies.items():
            actual = db.execute("SELECT prosrc FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=%s",(name,)).fetchone()
            assert actual and actual[0].strip() == body.strip(), "rehearsal must use current migration functions"
    practice, tenant, user = "handoff-" + uuid4().hex, str(uuid4()), str(uuid4())
    role = "handoff_" + uuid4().hex[:20]
    settings = Settings(environment="test", practice_id=practice, entra_tenant_id=tenant,
        database_runtime_role=role, database_runtime_password="synthetic-disposable-only",
        database_url=make_conninfo(DSN, user=role), billing_header_enabled=True, provider_handoff_enabled=True)
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES (%s,%s)", (practice,tenant))
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)", (tenant,user))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES (%s,%s,%s,%s)", (practice,tenant,user,["Provider"]))
        _provision_runtime_role(db, settings)
    ctx = PostgresContext(settings.database_url, practice_id=practice)
    return ctx, HeaderActor(tenant,user,"Provider synthetic","Provider")


def register_pg(ctx, actor, p):
    source = source_for(actor)
    config = Settings(environment="test", practice_id=ctx.practice_id, entra_tenant_id=actor.tenant_id, database_url=DSN)
    with psycopg.connect(DSN) as db:
        has_source = db.execute("SELECT 1 FROM provider_source_connections WHERE practice_id=%s AND connection_id=%s", (ctx.practice_id,source["connectionId"])).fetchone()
        has_grant = db.execute("SELECT 1 FROM provider_source_admissions WHERE practice_id=%s AND operation_id=%s", (ctx.practice_id,p.operation_id)).fetchone()
    if not has_source: register(config,"connection",source)
    if not has_grant: register(config,"grant",registration(p,actor,source))


def accept(ctx, actor, payload):
    p = prepare_handoff(HandoffRequest.model_validate(payload), ctx.practice_id)
    register_pg(ctx,actor,p)
    event = AccessAuditEvent(str(uuid4()), utc_now(), ctx.practice_id, actor.tenant_id,actor.user_id,
        (actor.role,), "provider_handoff.accept", "", "/api/provider/encounters/handoff", "POST",200,"allowed","127.0.0.1")
    PostgresAccessAuditStore(ctx).append(replace(event,outcome="attempted"))
    return PostgresHandoffStore(ctx).accept(p,actor,audit_completion=event)


def test_restricted_source_revisions_replicas_replay_and_immutable_receipts(context):
    ctx, actor = context
    PostgresHandoffStore(ctx).healthcheck()
    req = envelope(); req["note"]["text"] += "\nCafé 🧪\t"
    import hashlib
    req["note"]["sha256"] = hashlib.sha256(req["note"]["text"].encode()).hexdigest()
    one = accept(ctx,actor,req)
    with ctx.connection() as db:
        assert db.execute("SELECT rolsuper,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone() == {"rolsuper":False,"rolcreaterole":False,"rolbypassrls":False}
        assert not db.execute("SELECT has_table_privilege(current_user,'provider_handoff_operations','INSERT') AS allowed").fetchone()["allowed"]
    two = accept(ctx,actor,{**req,"operationId":str(uuid4()),"sourceRevision":2,"expectedSource":one["receipt"]["headerSource"]})
    assert two["receipt"]["encounterVersion"] == 2
    old = accept(ctx,actor,req)
    assert old["receipt"] == one["receipt"] and old["replayed"] and not old["current"] and old["launchPath"] is None
    with pytest.raises(HandoffError,match="source_changed"):
        accept(ctx,actor,{**req,"operationId":str(uuid4()),"expectedSource":two["receipt"]["headerSource"]})
    with pytest.raises(HandoffError,match="source_changed"):
        accept(ctx,actor,{**req,"operationId":str(uuid4()),"sourceRevision":3,"expectedSource":one["receipt"]["headerSource"]})
    stored = PostgresEncounterStore(ctx).get(one["receipt"]["encounterId"])
    assert source_snapshot(stored) == two["receipt"]["headerSource"]
    with pytest.raises(PersistenceUnavailable):
        PostgresEncounterStore(ctx).upsert(replace(stored.bundle,signed_note="tampered"))
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM provider_handoff_operations WHERE practice_id=%s",(ctx.practice_id,)).fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM access_audit_completions c JOIN access_audit_attempts a USING(practice_id,event_id) WHERE c.practice_id=%s AND a.action='provider_handoff.accept' AND c.status=200",(ctx.practice_id,)).fetchone()[0] == 3
    for statement in ("UPDATE provider_handoff_operations SET request_sha256=request_sha256", "DELETE FROM provider_handoff_operations", "TRUNCATE provider_handoff_operations"):
        with pytest.raises(psycopg.Error,match="append only|referenced"):
            with psycopg.connect(DSN) as db:
                db.execute(statement)


def test_sql_canonical_matches_python_unicode_and_controls(context):
    value = {"z":[1,True,None,"\t\n\r\"\\"], "é":"Café 🧪", "a":"\u007f"}
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT cosmaneura_handoff_canonical(%s)",(Jsonb(value),)).fetchone()[0] == canonical(value)


@pytest.mark.parametrize("tamper", ["supporting", "context", "cast", "missing_audit", "manifest_swap", "prior_drift", "float_cas", "offset_plus", "offset_minus", "clock_24"])
def test_narrow_writer_rejects_forged_source_receipts_atomically(context,tamper):
    ctx,actor = context
    payload = envelope()
    prior = None
    if tamper in {"prior_drift", "float_cas"}:
        prior = accept(ctx,actor,payload)
        payload.update(operationId=str(uuid4()),sourceRevision=2,expectedSource=prior["receipt"]["headerSource"])
        if tamper == "prior_drift":
            with psycopg.connect(DSN) as db:
                db.execute("UPDATE encounters SET signed_note=signed_note||' Owner drift.' WHERE practice_id=%s AND external_id=%s",
                           (ctx.practice_id,prior["receipt"]["encounterId"]))
        else:
            payload["expectedSource"]["encounterVersion"] = 1.0
    p = prepare_handoff(HandoffRequest.model_validate(payload),ctx.practice_id)
    register_pg(ctx,actor,p)
    from cosmaneura_billing.persistence import StoredEncounter
    resource = PostgresEncounterStore(ctx).get(prior["receipt"]["encounterId"]).resource_id if prior else str(uuid4())
    stored = StoredEncounter(resource,p.bundle,2 if prior else 1,"provider_completed_note")
    receipt = {"operationId":p.operation_id,"receiptId":str(uuid4()),"acceptedAt":utc_now(),
               "encounterId":p.bundle.encounter_id,"encounterVersion":stored.version,"sourceRevision":p.source_revision,
               "sourceManifestSha256":p.manifest_sha256,"headerSource":source_snapshot(stored)}
    command = {"practice":ctx.practice_id,"recordKey":p.record_key,"sourceRevision":p.source_revision,"manifest":p.manifest,
        "manifestSha256":p.manifest_sha256,"expectedSource":p.expected_source,"bundle":asdict(p.bundle),"resourceId":stored.resource_id,
        "fixture":False,"actor":asdict(actor),"requestSha256":digest({"request":p.intent,"actor":asdict(actor),"practice":ctx.practice_id}),"receipt":receipt,"eventId":str(uuid4())}
    if tamper == "supporting": receipt["headerSource"]["supportingSourceDigests"] = [{"kind":"transcript","sha256":"0"*64}]
    if tamper == "context": command["bundle"]["patient_age"] = 31
    if tamper == "cast": command["bundle"]["patient_age"] = "30"; receipt["headerSource"]["bundleSha256"] = digest(command["bundle"])
    if tamper in {"offset_plus", "offset_minus", "clock_24"}:
        command["manifest"]["completion"]["completedAt"] = "2026-09-30T12:00:00" + ("+00:99" if tamper == "offset_plus" else "-04:60")
        if tamper == "clock_24": command["manifest"]["completion"]["completedAt"] = "2026-09-30T24:00:00Z"
        command["manifestSha256"] = digest(command["manifest"]); receipt["sourceManifestSha256"] = command["manifestSha256"]
    if tamper == "manifest_swap":
        changed = replace(stored,bundle=replace(stored.bundle,signed_note="Different synthetic source with a correctly recomputed pin."))
        command["bundle"] = asdict(changed.bundle); receipt["headerSource"] = source_snapshot(changed)
    with pytest.raises(PersistenceUnavailable) as failure:
        with ctx.connection() as db:
            if tamper != "missing_audit":
                event = AccessAuditEvent(command["eventId"],utc_now(),ctx.practice_id,actor.tenant_id,actor.user_id,
                    (actor.role,),"provider_handoff.accept",stored.resource_id,"/api/provider/encounters/handoff",
                    "POST",200,"allowed","127.0.0.1")
                PostgresAccessAuditStore._insert_attempt(db,replace(event,outcome="attempted"))
                PostgresAccessAuditStore._insert_completion(db,event)
            db.execute("SELECT cosmaneura_commit_provider_handoff(%s)",(Jsonb(command),))
    expected = ("atomic provider audit" if tamper == "missing_audit" else
                "provider source changed" if tamper == "prior_drift" else
                "provider admission unavailable" if tamper in {"offset_plus", "offset_minus", "clock_24"} else
                "provider request is not admitted" if tamper == "manifest_swap" else
                "provider manifest" if tamper in {"context", "cast"} else
                "provider source changed" if tamper == "float_cas" else "persisted provider source")
    assert expected in str(failure.value.__cause__)
    current = PostgresEncounterStore(ctx).get(p.bundle.encounter_id)
    assert current.version == 1 if prior else current is None
    with ctx.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM provider_handoff_operations").fetchone()["n"] == (1 if prior else 0)


def test_exact_replay_rechecks_membership_and_actor(context):
    ctx, actor = context
    req = envelope(); result = accept(ctx,actor,req)
    other = replace(actor,user_id=str(uuid4()))
    with psycopg.connect(DSN) as db:
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES (%s,%s)",(other.tenant_id,other.user_id))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES (%s,%s,%s,%s)",
                   (ctx.practice_id,other.tenant_id,other.user_id,["Provider"]))
    with pytest.raises(HandoffError,match="handoff_conflict"):
        accept(ctx,other,req)
    with pytest.raises(HandoffError,match="not_authorized"):
        PostgresHandoffStore(PostgresContext(ctx.dsn,practice_id="another-practice")).accept(
            prepare_handoff(HandoffRequest.model_validate(req),"another-practice"),actor)
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s AND user_id=%s",(ctx.practice_id,actor.user_id))
    with pytest.raises(HandoffError,match="not_authorized"):
        accept(ctx,actor,req)
    assert PostgresEncounterStore(ctx).get(result["receipt"]["encounterId"]).version == 1


def test_fixture_durable_receipt_uses_existing_owner_seed_and_no_encounter_write(context):
    from cosmaneura_billing.provider_handoff import FixtureRequest, prepare_fixture
    from cosmaneura_billing.seed_demo_release_candidate import seed_synthetic_demo_release_candidate
    ctx, actor = context
    seed_synthetic_demo_release_candidate(Settings(environment="test",database_url=DSN,
        practice_id=ctx.practice_id,entra_tenant_id=actor.tenant_id))
    prepared = prepare_fixture(FixtureRequest(operationId=str(uuid4()),fixtureKey="SYN-RC-0001"))
    before = PostgresEncounterStore(ctx).get("SYN-RC-0001")
    def record():
        event = AccessAuditEvent(str(uuid4()),utc_now(),ctx.practice_id,actor.tenant_id,actor.user_id,
            (actor.role,),"provider_handoff.fixture","","/api/provider/encounters/handoff/fixture","POST",200,"allowed","127.0.0.1")
        PostgresAccessAuditStore(ctx).append(replace(event,outcome="attempted"))
        return PostgresHandoffStore(ctx).accept(prepared,actor,audit_completion=event)
    one = record(); replay = record()
    assert one["current"] and replay["replayed"] and replay["receipt"] == one["receipt"]
    assert PostgresEncounterStore(ctx).get("SYN-RC-0001") == before
    with ctx.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM provider_handoff_operations").fetchone()["n"] == 1
