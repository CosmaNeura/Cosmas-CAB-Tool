"""Bounded independent-process/store/API behavior, never vendor calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
import time
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from cosmaneura_billing import api, billing_header as h
from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.billing_header_routes import HeaderGenerate, HeaderSave
from cosmaneura_billing.billing_header_store import PostgresBillingHeaderStore
from cosmaneura_billing.header_generation import GenerationManager, GenerationStore, command_for
from cosmaneura_billing.persistence import PostgresContext, PostgresEncounterStore, PersistenceUnavailable
from cosmaneura_billing.settings import Settings, SettingsError
from header_generation_fixture import prepare

DSN = os.environ.get('CAB_HEADER_ASYNC_TEST_DSN','')


@pytest.fixture
def f(tmp_path):
    if not DSN: pytest.skip('requires explicitly owned local async test DB')
    return prepare(DSN,tmp_path)


def event(f,stored=None):
    stored=stored or f.stored
    return AccessAuditEvent(str(uuid4()),h.utc_now(),f.settings.practice_id,f.actor.tenant_id,f.actor.user_id,
        ('Provider',),'billing_header.compute',stored.resource_id,'/api/encounters/header/generate','POST',200,'allowed','127.0.0.1')


def manager(f):
    m = GenerationManager(f.settings,factory=f.factory); m.start(); return m


def wait_until(fn, timeout=15):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        value=fn()
        if value: return value
        time.sleep(.1)
    pytest.fail('bounded local worker observation timed out')


def calls(f):
    p=f.path/'calls.txt'; return len(p.read_text().splitlines()) if p.exists() else 0


def status(m,f,operation):
    return m.store.call(command_for(f.stored,f.actor,operation),'status')['status']


def test_default_and_closed_request_compatibility():
    assert Settings().billing_header_async_enabled is False
    with pytest.raises(SettingsError): Settings(billing_header_async_enabled=True)
    with pytest.raises(SettingsError): Settings(billing_header_async_enabled=1)
    with pytest.raises(Exception): HeaderGenerate(encounterId='x',expectedSource={},operationId=str(uuid4()),responseMode=True)
    with pytest.raises(Exception): HeaderSave(encounterId='x',expectedSource={},operationId=str(uuid4()),responseMode='async',
        headerId=str(uuid4()),expectedRevision=0,draft={},reason='Human')


def test_actual_spawned_api_quick_response_alias_status_edit_and_reload(f,monkeypatch,tmp_path):
    (f.path/'control.json').write_text('{"delay":1}')
    # Parent composition is also synthetic; each child uses the same module-level factory.
    monkeypatch.setattr(api,'get_orchestrator',f.factory)
    app=api.create_app(settings=f.settings,frontend_dist=tmp_path/'missing')
    app.state.header_generation.factory=f.factory
    with TestClient(app,headers=f.auth) as client:
        assert client.get('/api/session').json()['capabilities']['billingHeaderAsync'] is True
        op=str(uuid4()); command={**f.target,'operationId':op,'responseMode':'async'}
        began=time.monotonic(); result=client.post('/api/encounters/header/generate',json=command)
        assert result.status_code==200,result.text
        assert time.monotonic()-began<1 and result.json()['generation']=={'operationId':op,'status':'running'}
        alias=str(uuid4())
        repeated=client.post('/api/encounters/header/generate',json={**command,'operationId':alias})
        assert repeated.status_code==200 and repeated.json()['generation']['operationId']==alias
        def poll():
            time.sleep(.5)
            r=client.post('/api/encounters/header/generation-status',json={**f.target,'operationId':alias})
            assert r.status_code==200,r.text
            return r.json() if r.json()['generation']['status']!='running' else None
        with ThreadPoolExecutor(1) as pool:
            # An old synchronous caller joins the same claim instead of invoking a model.
            sync=pool.submit(client.post,'/api/encounters/header/generate',json={**f.target,'operationId':str(uuid4())})
            final=wait_until(poll)
            sync_result=sync.result(timeout=15)
            assert sync_result.status_code==200,sync_result.text
            assert sync_result.json()['view']['header']['headerId']==final['view']['header']['headerId']
        assert final['generation']['status']=='succeeded',final
        assert calls(f)==2  # Both actual extraction calls, one full pipeline.
        view=final['view']; draft=h.create_draft(view['header']);draft['note']='Human reviewed synthetic result.'
        saved=client.post('/api/encounters/header/save',json={**f.target,'operationId':str(uuid4()),
            'headerId':view['header']['headerId'],'expectedRevision':0,'draft':draft,'reason':'Human review'})
        assert saved.status_code==200,saved.text
        assert client.post('/api/encounters/header/generation-status',json={**f.target,'operationId':op}).json()['view']['revisionNumber']==1
        assert client.post('/api/encounters/header/generate',json=command).json()['view']['revisionNumber']==1
        assert calls(f)==2
        assert PostgresEncounterStore(f.owner).get(f.stored.bundle.encounter_id).bundle==f.stored.bundle


def test_cross_manager_duplicate_sync_barrier_and_global_capacity(f):
    (f.path/'control.json').write_text('{"delay":2}')
    one,two=manager(f),manager(f)
    try:
        operations=[str(uuid4()),str(uuid4())]
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda pair:pair[0].submit(f.stored,f.actor,f.principal,pair[1],event(f)),zip((one,two),operations)))
        assert results[0]['job']==results[1]['job']
        with f.owner.connection() as db:
            # Only the actual running job's event remains incomplete.
            assert db.execute('SELECT count(*) AS n FROM access_audit_attempts a LEFT JOIN access_audit_completions c USING(practice_id,event_id) WHERE a.practice_id=%s AND c.event_id IS NULL',(f.settings.practice_id,)).fetchone()['n']==1
        second=PostgresEncounterStore(f.owner).upsert(replace(f.stored.bundle,encounter_id='ASYNC-2'),source=f.stored.source)
        third=PostgresEncounterStore(f.owner).upsert(replace(f.stored.bundle,encounter_id='ASYNC-3'),source=f.stored.source)
        two.submit(second,f.actor,f.principal,str(uuid4()),event(f,second))
        with pytest.raises((PersistenceUnavailable,h.BillingHeaderError)):
            one.submit(third,f.actor,f.principal,str(uuid4()),event(f))
        wait_until(lambda:status(one,f,operations[0])!='running')
        assert status(one,f,operations[0])=='succeeded' and calls(f) in (3,4)
    finally: one.shutdown();two.shutdown()


@pytest.mark.parametrize('change',['source','member','audit','crash','shutdown'])
def test_interruption_or_revocation_never_persists_or_reruns(f,change):
    (f.path/'control.json').write_text(json.dumps({'delay':2,'exit':change=='crash'}))
    m=manager(f);op=str(uuid4())
    try:
        m.submit(f.stored,f.actor,f.principal,op,event(f));wait_until(lambda:calls(f)>0)
        if change=='source': PostgresEncounterStore(f.owner).upsert(replace(f.stored.bundle,transcript='New authored source.'))
        if change=='member':
            with f.owner.connection() as db: db.execute('UPDATE practice_memberships SET active=false WHERE practice_id=%s',(f.settings.practice_id,))
        if change=='audit':
            from psycopg import sql
            with f.owner.connection() as db:
                db.execute(sql.SQL('REVOKE INSERT ON access_audit_completions FROM {}').format(sql.Identifier(f.settings.database_runtime_role)))
        if change=='shutdown': m.shutdown()
        wait_until(lambda:not m._children)
        with f.owner.connection() as db:
            assert db.execute('SELECT count(*) AS n FROM billing_headers WHERE practice_id=%s',(f.settings.practice_id,)).fetchone()['n']==0
            assert db.execute('SELECT status FROM billing_header_generations WHERE practice_id=%s',(f.settings.practice_id,)).fetchone()['status']=='unknown'
        if change in ('crash','shutdown'):
            fresh=manager(f)
            try:
                count=calls(f)
                assert fresh.submit(f.stored,f.actor,f.principal,str(uuid4()),event(f))['status']=='unknown'
                assert calls(f)==count
            finally: fresh.shutdown()
    finally: m.shutdown()


def test_unknown_missing_and_runtime_cannot_write_or_forge_source(f):
    store=GenerationStore(PostgresContext(f.settings.database_url,practice_id=f.settings.practice_id))
    cmd=command_for(f.stored,f.actor,str(uuid4()))
    assert store.call(cmd,'status')=={'status':'unknown'}
    with store.context.connection() as db:
        assert db.execute("SELECT has_table_privilege(current_user,'billing_header_generations','SELECT,INSERT,UPDATE,DELETE') AS p").fetchone()['p'] is False
    changed={**cmd,'source':{**cmd['source'],'bundleSha256':'0'*64}}
    with pytest.raises(h.BillingHeaderError,match="source_changed"): store.call(changed,'status')


def test_timeout_and_orphan_claim_remain_unknown_without_restart(f):
    (f.path/'control.json').write_text('{"delay":20}')
    m=manager(f);original=m.store.call
    def shorter(command,action,**kwargs):
        if action=='claim':
            with f.owner.connection() as db: now=db.execute('SELECT clock_timestamp() AS t').fetchone()['t']
            command={**command,'deadline':(now+timedelta(seconds=8)).isoformat()}
        return original(command,action,**kwargs)
    m.store.call=shorter
    try:
        op=str(uuid4());m.submit(f.stored,f.actor,f.principal,op,event(f))
        wait_until(lambda:calls(f)>0);wait_until(lambda:not m._children)
        assert status(m,f,op)=='unknown' and calls(f)==1
        # Another process must observe the terminal attempt, never claim again.
        fresh=manager(f)
        try: assert fresh.submit(f.stored,f.actor,f.principal,str(uuid4()),event(f))['status']=='unknown'
        finally: fresh.shutdown()
    finally: m.shutdown()


def test_orphan_running_waits_original_deadline_and_claim_is_audited(f):
    m=manager(f);op=str(uuid4());attempt=replace(event(f),status=0,outcome='attempted')
    cmd=command_for(f.stored,f.actor,op)
    with f.owner.connection() as db: now=db.execute('SELECT clock_timestamp() AS t').fetchone()['t']
    cmd.update(job=str(uuid4()),owner=str(uuid4()),audit=attempt.event_id,deadline=(now+timedelta(seconds=1)).isoformat())
    m.store.call(cmd,'claim',attempt=attempt)
    fresh=manager(f)
    try:
        assert status(fresh,f,op)=='running'  # Startup cannot kill another owner's lease.
        wait_until(lambda:status(fresh,f,op)=='unknown')
        assert not calls(f)
        with f.owner.connection() as db:
            assert db.execute('SELECT count(*) AS n FROM billing_header_generations WHERE practice_id=%s',(f.settings.practice_id,)).fetchone()['n']==1
    finally: fresh.shutdown();m.shutdown()


def test_legacy_success_receipt_status_survives_lost_async_response(f):
    from test_billing_header import generated
    from cosmaneura_billing.persistence import PostgresAccessAuditStore
    context=PostgresContext(f.settings.database_url,practice_id=f.settings.practice_id)
    headers=PostgresBillingHeaderStore(context);audit=event(f);op=str(uuid4())
    PostgresAccessAuditStore(context).append(replace(audit,status=0,outcome='attempted'))
    headers.create(f.stored.bundle.encounter_id,expected_source=h.source_snapshot(f.stored),generated=generated(f.stored),
        actor=f.actor,operation_id=op,audit_completion=audit)
    assert GenerationStore(context).call(command_for(f.stored,f.actor,op),'status')['status']=='succeeded'


def test_spawn_failure_is_durable_failed_and_cannot_start_again(f,monkeypatch):
    m=manager(f)
    class NoStart:
        def Process(self,**kwargs): return self
        def start(self): raise OSError('synthetic process start failure')
    monkeypatch.setattr('cosmaneura_billing.header_generation.multiprocessing.get_context',lambda *_:NoStart())
    try:
        with pytest.raises(h.BillingHeaderError,match='generation_unavailable'):
            m.submit(f.stored,f.actor,f.principal,str(uuid4()),event(f))
        assert m.submit(f.stored,f.actor,f.principal,str(uuid4()),event(f))['status']=='failed'
        assert not m._children and not calls(f)
    finally: m.shutdown()


@pytest.mark.parametrize('unsupported',['secondary_coding','ta4h','shadow_runner'])
def test_unsupported_parallel_engine_fails_before_model_or_started(f,unsupported):
    (f.path/'control.json').write_text(json.dumps({'unsupported':unsupported}))
    m=manager(f);op=str(uuid4())
    try:
        m.submit(f.stored,f.actor,f.principal,op,event(f))
        wait_until(lambda:status(m,f,op)!='running')
        assert status(m,f,op)=='failed' and calls(f)==0
        with f.owner.connection() as db:
            assert db.execute('SELECT started FROM billing_header_generations WHERE practice_id=%s',(f.settings.practice_id,)).fetchone()['started'] is False
            assert db.execute('SELECT count(*) AS n FROM billing_headers WHERE practice_id=%s',(f.settings.practice_id,)).fetchone()['n']==0
    finally: m.shutdown()
