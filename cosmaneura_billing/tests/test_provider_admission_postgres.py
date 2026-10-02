"""Restricted-role registry, revocation and real receiver flow on disposable PG."""
import base64
from copy import deepcopy
from dataclasses import asdict, replace
import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from test_provider_handoff_postgres import context, DSN, register_pg, accept
from test_provider_handoff import envelope
from test_completed_note_client import synthetic_envelope, SyntheticExtraction
from provider_admission_support import source_for, registration
from cosmaneura_billing.api import create_app
from cosmaneura_billing.billing_header import create_draft
from cosmaneura_billing.migrations import _provision_chase_worker_role
from cosmaneura_billing.provider_admission_cli import register, revoke
from cosmaneura_billing.provider_handoff import HandoffError, HandoffRequest, prepare_handoff
from cosmaneura_billing.provider_handoff_store import PostgresHandoffStore, _PostgresUnit
from cosmaneura_billing.persistence import PersistenceUnavailable, PostgresEncounterStore
from cosmaneura_billing.settings import Settings
import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

pytestmark=pytest.mark.skipif(not DSN,reason='explicit disposable database required')
TABLES=('provider_source_connections','provider_source_admissions','provider_source_bindings','provider_source_acceptances')


def owner_settings(ctx,actor):
    return Settings(environment='test',database_url=DSN,practice_id=ctx.practice_id,entra_tenant_id=actor.tenant_id)


def test_runtime_can_only_read_registry_worker_cannot_access_or_bypass(context):
    ctx,actor=context; source=source_for(actor)
    runtime=replace(owner_settings(ctx,actor),database_url=ctx.dsn)
    with pytest.raises(PermissionError): register(runtime,'connection',source)
    register(owner_settings(ctx,actor),'connection',source)
    with ctx.connection() as db:
        for table in TABLES:
            assert db.execute('SELECT has_table_privilege(current_user,%s,\'SELECT\') AS v',(table,)).fetchone()['v']
            for action in ('INSERT','UPDATE','DELETE','TRUNCATE'):
                assert not db.execute('SELECT has_table_privilege(current_user,%s,%s) AS v',(table,action)).fetchone()['v']
        assert not db.execute("SELECT has_function_privilege(current_user,'cosmaneura_commit_provider_handoff_019(jsonb)','EXECUTE') AS v").fetchone()['v']
        assert db.execute('SELECT count(*) AS n FROM provider_source_connections').fetchone()['n']==1
    with pytest.raises(PersistenceUnavailable):
        with ctx.connection() as db: db.execute('SELECT cosmaneura_commit_provider_handoff_019(%s)',(Jsonb({}),))
    worker='admission_worker_'+uuid4().hex[:12]
    config=replace(owner_settings(ctx,actor),chase_worker_database_role=worker,chase_worker_database_password='local-test-only')
    with psycopg.connect(DSN) as db: _provision_chase_worker_role(db,config)
    with psycopg.connect(make_conninfo(DSN,user=worker)) as db:
        for table in TABLES:
            for action in ('SELECT','INSERT','UPDATE','DELETE'):
                assert not db.execute('SELECT has_table_privilege(current_user,%s,%s)',(table,action)).fetchone()[0]
        for fn in ('cosmaneura_commit_provider_handoff','cosmaneura_commit_provider_handoff_019'):
            assert not db.execute('SELECT has_function_privilege(current_user,%s,\'EXECUTE\')',(fn+'(jsonb)',)).fetchone()[0]


def test_grant_revocation_between_preflight_and_writer_rolls_back_source_and_audit(context,monkeypatch):
    ctx,actor=context; raw=envelope(); p=prepare_handoff(HandoffRequest.model_validate(raw),ctx.practice_id)
    original=_PostgresUnit.audit
    def revoke_after_preflight(unit,*args):
        original(unit,*args)
        revoke(owner_settings(ctx,actor),'grant',p.operation_id)
    monkeypatch.setattr(_PostgresUnit,'audit',revoke_after_preflight)
    with pytest.raises(PersistenceUnavailable): accept(ctx,actor,raw)
    assert PostgresEncounterStore(ctx).get(p.record_key) is None
    with ctx.connection() as db:
        for table in ('provider_handoff_operations','provider_source_acceptances','provider_source_bindings'):
            assert db.execute('SELECT count(*) AS n FROM '+table).fetchone()['n']==0
        assert db.execute('SELECT revoked_at IS NOT NULL AS v FROM provider_source_admissions').fetchone()['v']
    with psycopg.connect(DSN) as db:
        assert db.execute('SELECT count(*) FROM access_audit_completions WHERE practice_id=%s',(ctx.practice_id,)).fetchone()[0]==0


def test_writer_locks_connection_until_commit_then_allows_revocation_and_receipt_recovery(context,monkeypatch):
    ctx,actor=context; raw=envelope(); original=_PostgresUnit.persist
    def assert_locked(unit,*args):
        original(unit,*args)
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with psycopg.connect(DSN) as db:
                db.execute("SET LOCAL lock_timeout='200ms'")
                db.execute('UPDATE provider_source_connections SET revoked_at=clock_timestamp() WHERE practice_id=%s',(ctx.practice_id,))
    monkeypatch.setattr(_PostgresUnit,'persist',assert_locked)
    one=accept(ctx,actor,raw)
    revoke(owner_settings(ctx,actor),'connection',source_for(actor)['connectionId'])
    assert accept(ctx,actor,raw)=={**one,'replayed':True}
    with ctx.connection() as db:
        proof=db.execute('SELECT provenance FROM provider_source_acceptances').fetchone()['provenance']
        assert proof['connection']==source_for(actor) and proof['admission']['operationId']==raw['operationId']
        assert proof['admission']['actorUserId']==actor.user_id
        assert db.execute('SELECT count(*) AS n FROM provider_source_acceptances').fetchone()['n']==1
    # Reuse an already registered new grant, then revoke its connection.
    newer=prepare_handoff(HandoffRequest.model_validate({**raw,'operationId':str(uuid4()),'sourceRevision':2,
        'expectedSource':one['receipt']['headerSource']}),ctx.practice_id)
    with pytest.raises(ValueError,match='unavailable'): register(owner_settings(ctx,actor),'grant',registration(newer,actor))
    with pytest.raises(HandoffError,match='operational_handoff_not_approved'): PostgresHandoffStore(ctx).accept(newer,actor)


@pytest.mark.parametrize('field',['notBefore','expiresAt','operationId','connectionId','sourceConnectionId'])
def test_sql_itself_rejects_missing_mandatory_grant_field(context,monkeypatch,field):
    ctx,actor=context; raw=envelope(); p=prepare_handoff(HandoffRequest.model_validate(raw),ctx.practice_id)
    register_pg(ctx,actor,p)
    # Deliberately malformed owner input: runtime cannot create it. Avoid bypassing
    # append-only history by making a distinct new registration before acceptance.
    bad=deepcopy(raw); bad['operationId']=str(uuid4()); q=prepare_handoff(HandoffRequest.model_validate(bad),ctx.practice_id)
    doc=registration(q,actor)
    with psycopg.connect(DSN) as db:
        if field=='sourceConnectionId':
            source={**source_for(actor),'connectionId':str(uuid4())}; doc['connectionId']=source['connectionId']
            del source['connectionId']
            db.execute('INSERT INTO provider_source_connections VALUES(%s,%s,%s,NULL)',(ctx.practice_id,doc['connectionId'],Jsonb(source)))
        else: del doc[field]
        db.execute('INSERT INTO provider_source_admissions VALUES(%s,%s,%s,%s,NULL)',
            (ctx.practice_id,q.operation_id,doc.get('connectionId',source_for(actor)['connectionId']),Jsonb(doc)))
    # Capture a complete legitimate runtime command, then bind it to the invalid
    # interval registration. Its source/actor digests remain otherwise exact.
    commands=[]; original=_PostgresUnit.persist
    def capture(unit,*args):
        class Capture:
            def execute(self,sql,params): commands.append(params[0].obj)
        previous=unit.conn; unit.conn=Capture()
        try: original(unit,*args)
        finally: unit.conn=previous
        raise RuntimeError('captured only')
    monkeypatch.setattr(_PostgresUnit,'persist',capture)
    with pytest.raises(RuntimeError,match='captured only'): accept(ctx,actor,raw)
    from cosmaneura_billing.billing_header import digest
    cmd=commands[0]; cmd['receipt']['operationId']=q.operation_id
    cmd['requestSha256']=digest({'request':q.intent,'actor':asdict(actor),'practice':ctx.practice_id})
    with pytest.raises(PersistenceUnavailable) as error:
        with ctx.connection() as db: db.execute('SELECT cosmaneura_commit_provider_handoff(%s)',(Jsonb(cmd),))
    assert 'provider admission unavailable' in str(error.value.__cause__)
    assert PostgresEncounterStore(ctx).get(q.record_key) is None


def test_registered_public_receiver_generates_editable_header_and_reloads_across_replicas(context,tmp_path):
    ctx,actor=context
    with psycopg.connect(DSN) as db:
        admin=str(uuid4())
        db.execute('INSERT INTO users(tenant_id,user_id) VALUES(%s,%s)',(actor.tenant_id,admin))
        db.execute('INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s,%s,%s)',(ctx.practice_id,actor.tenant_id,admin,['Admin']))
    config=Settings(environment='test',auth_mode='container_apps',entra_tenant_id=actor.tenant_id,
        practice_id=ctx.practice_id,state_store='postgres',chase_store='postgres',database_url=ctx.dsn,
        provider_handoff_enabled=True,billing_header_enabled=True,azure_openai_enabled=False,
        azure_language_enabled=False,telemetry_enabled=False,cpt_auto_fetch=False,
        review_context_signing_key='local-synthetic-registry-rehearsal-only-key')
    auth={'auth_typ':'aad','claims':[{'typ':'oid','val':actor.user_id},{'typ':'tid','val':actor.tenant_id},{'typ':'roles','val':'Provider'}]}
    headers={'X-MS-CLIENT-PRINCIPAL':base64.b64encode(json.dumps(auth).encode()).decode()}
    app=create_app(settings=config,frontend_dist=tmp_path/'none')
    replica=create_app(settings=config,frontend_dist=tmp_path/'none')
    raw=synthetic_envelope(); p=prepare_handoff(HandoffRequest.model_validate(raw),ctx.practice_id)
    register_pg(ctx,actor,p)
    with TestClient(app,client=('127.0.0.1',51001)) as client, TestClient(replica,client=('127.0.0.1',51002)) as other:
        client.headers.update(headers); other.headers.update(headers)
        extraction=SyntheticExtraction(); app.state.orchestrator.coding.llm=extraction
        accepted=client.post('/api/provider/encounters/handoff',json=raw)
        assert accepted.status_code==200,accepted.text
        assert extraction.calls==0 and accepted.json()['generation']=='not_started'
        receipt=accepted.json()['receipt']; target={'encounterId':receipt['encounterId'],'expectedSource':receipt['headerSource']}
        generated=client.post('/api/encounters/header/generate',json={**target,'operationId':str(uuid4())})
        assert generated.status_code==200,generated.text
        view=generated.json()['view']; draft=create_draft(view['header']); draft['note']='Reviewed registered synthetic source.'
        saved=client.post('/api/encounters/header/save',json={**target,'headerId':view['header']['headerId'],
            'expectedRevision':0,'operationId':str(uuid4()),'draft':draft,'reason':'Synthetic provider review'})
        assert saved.status_code==200,saved.text
        assert saved.json()['view']['revision']['actor']['id']==actor.user_id
        loaded=other.post('/api/encounters/header',json=target)
        assert loaded.status_code==200 and loaded.json()['view']['revision']['draft']['note']==draft['note']
        assert other.post('/api/provider/encounters/handoff',json=raw).json()['replayed']
        assert PostgresEncounterStore(ctx).get(p.record_key).bundle.signed_note==raw['note']['text']
        assert extraction.calls==2 and client.get('/api/ready').status_code==200
        from psycopg import sql
        with psycopg.connect(DSN) as db:
            from psycopg.conninfo import conninfo_to_dict
            db.execute(sql.SQL('REVOKE SELECT ON provider_source_acceptances FROM {}').format(sql.Identifier(conninfo_to_dict(ctx.dsn)['user'])))
        assert client.get('/api/ready').status_code==503


def test_owner_cannot_rewrite_registry_or_acceptance_history(context):
    ctx,actor=context; accept(ctx,actor,envelope())
    for table,column in (('provider_source_connections','document'),('provider_source_admissions','document'),
                         ('provider_source_bindings','record_key'),('provider_source_acceptances','provenance')):
        for statement in ('UPDATE '+table+' SET '+column+'='+column+' WHERE practice_id=%s',
                          'DELETE FROM '+table+' WHERE practice_id=%s'):
            with pytest.raises(psycopg.Error,match='immutable|append only'):
                with psycopg.connect(DSN) as db: db.execute(statement,(ctx.practice_id,))
    revoke(owner_settings(ctx,actor),'connection',source_for(actor)['connectionId'])
    with pytest.raises(psycopg.Error,match='immutable'):
        with psycopg.connect(DSN) as db:
            db.execute('UPDATE provider_source_connections SET revoked_at=NULL WHERE practice_id=%s',(ctx.practice_id,))
