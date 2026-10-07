"""Closed continuation wire and actual spawned CAB workers; authored data only."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from functools import partial
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from cosmaneura_billing.billing_header_routes import CoreHeaderGenerate, HeaderGenerate
from cosmaneura_billing.core_bridge import CoreLease, create_core_bridge
from cosmaneura_billing.core_bridge_contract import CoreBridgeError, digest, verify_authorization
from cosmaneura_billing.settings import Settings, SettingsError
from cosmaneura_billing.persistence import PostgresContext
from core_bridge_fixture import prepare_core_fixture
from header_generation_fixture import engine
from test_core_async_generation import Authorizer
from test_header_generation import wait_until

DSN = os.environ.get('CAB_HEADER_ASYNC_TEST_DSN', '')


class ContinuousAuthorizer(Authorizer):
    """Fake authenticated Core: continuity rules are independently tested in Core.

    The file contains only synthetic explicit continuation receipts. An expired
    chain cannot be revived, even if a newer provider expiry is supplied later.
    """
    def __init__(self, path, deadline, initial):
        super().__init__(path, deadline)
        self.initial = initial

    def __call__(self, operation, purpose, request_sha):
        value = super().__call__(operation, purpose, request_sha)
        if purpose == 'header.generate':
            until = datetime.fromisoformat(self.initial)
            evidence = self.path / 'continuations.json'
            for receipt in json.loads(evidence.read_text()) if evidence.exists() else []:
                if receipt['requestSha256'] != request_sha or datetime.fromisoformat(receipt['receivedAt']) >= until:
                    raise CoreBridgeError()
                until = min(datetime.fromisoformat(self.deadline), datetime.fromisoformat(receipt['authorizedUntil']))
            if datetime.now(timezone.utc) >= until:
                raise CoreBridgeError()
            value.update(generationContinuationRequired=True, generationAuthorizedUntil=until.isoformat())
            value['expiresAt'] = min(datetime.fromisoformat(value['expiresAt']), until).isoformat()
        return value


def operation():
    return {'operationId':str(uuid4()), 'practiceId':'authored',
        'actor':{'issuer':'https://authored.example','subject':'provider','role':'Provider'}, 'source':{}, 'admission':{}}


def test_consent_is_core_only_strict_and_default_off():
    command={'encounterId':'authored','expectedSource':{},'operationId':str(uuid4()),'responseMode':'async'}
    assert Settings().billing_header_continuation_enabled is False
    for settings in ({'billing_header_continuation_enabled':True}, {'billing_header_continuation_enabled':1}):
        with pytest.raises(SettingsError): Settings(**settings)
    with pytest.raises(SettingsError):
        Settings.from_env({'COSMANEURA_BILLING_HEADER_CONTINUATION_ENABLED':'maybe'})
    for value in (False, None, 1, 'true'):
        with pytest.raises(Exception): CoreHeaderGenerate(**command,continuationConsent=value)
    with pytest.raises(Exception): CoreHeaderGenerate(**{k:v for k,v in command.items() if k!='responseMode'},continuationConsent=True)
    with pytest.raises(Exception): HeaderGenerate(**command,continuationConsent=True)
    assert CoreHeaderGenerate(**command).continuationConsent is None
    assert CoreHeaderGenerate(**command,continuationConsent=True).continuationConsent is True


@pytest.mark.parametrize('change',['missing','false','integer','extra','expired','beyond_hard','reply_beyond_authority','unrequested'])
def test_current_authority_is_closed_and_bounded(tmp_path,change):
    now=datetime.now(timezone.utc);op=operation()
    authorizer=ContinuousAuthorizer(tmp_path,(now+timedelta(seconds=90)).isoformat(),(now+timedelta(seconds=45)).isoformat())
    value=authorizer(op,'header.generate','0'*64)
    assert verify_authorization(value,op,'header.generate','0'*64,now=now,async_generation=True,continuation=True)==value
    if change=='missing': value.pop('generationAuthorizedUntil')
    if change=='false': value['generationContinuationRequired']=False
    if change=='integer': value['generationContinuationRequired']=1
    if change=='extra': value['renewed']=True
    if change=='expired': value['generationAuthorizedUntil']=(now-timedelta(seconds=1)).isoformat()
    if change=='beyond_hard': value['generationAuthorizedUntil']=(now+timedelta(seconds=91)).isoformat()
    if change=='reply_beyond_authority': value['generationAuthorizedUntil']=(now+timedelta(seconds=5)).isoformat()
    with pytest.raises(CoreBridgeError):
        verify_authorization(value,op,'header.generate','0'*64,now=now,async_generation=True,continuation=change!='unrequested')


def test_lease_keeps_hard_deadline_and_rechecks_workload_after_callback(tmp_path):
    now=datetime.now(timezone.utc);op=operation()
    authorizer=ContinuousAuthorizer(tmp_path,(now+timedelta(seconds=90)).isoformat(),(now+timedelta(seconds=30)).isoformat())
    workload=SimpleNamespace(expires_at=now.timestamp()+120)
    lease=CoreLease(operation=op,purpose='header.generate',request_sha='0'*64,principal=None,workload=workload,authorizer=authorizer)
    lease.async_generation=lease.continuation_required=True
    with lease.use():
        (tmp_path/'continuations.json').write_text(json.dumps([{'receivedAt':now.isoformat(),
            'authorizedUntil':(now+timedelta(seconds=60)).isoformat(),'requestSha256':'0'*64}]))
        assert lease.recheck()['generationAuthorizedUntil']==(now+timedelta(seconds=60)).isoformat()
        authorizer.deadline=(now+timedelta(seconds=91)).isoformat()
        with pytest.raises(CoreBridgeError): lease.recheck()
        authorizer.deadline=(now+timedelta(seconds=90)).isoformat()
        def delayed(*args):
            value=authorizer(*args);workload.expires_at=0;return value
        lease.authorizer=delayed
        with pytest.raises(CoreBridgeError): lease.recheck()


@pytest.mark.parametrize('outcome',['continued','disconnected','late','workload_expired'])
def test_spawned_continuation_preserves_full_pipeline_and_original_bounds(tmp_path,outcome):
    if not DSN: pytest.skip('requires explicitly owned local async database')
    f=prepare_core_fixture(DSN,tmp_path,async_generation=True)
    settings=replace(f.settings,billing_header_continuation_enabled=True)
    now=datetime.now(timezone.utc);initial=now+timedelta(seconds=30);hard=now+timedelta(seconds=75)
    authorizer=ContinuousAuthorizer(tmp_path,hard.isoformat(),initial.isoformat())
    (tmp_path/'control.json').write_text('{"delay":32}')
    app=create_core_bridge(settings=settings,policy=f.policy,verifier=f.verifier,authorizer=authorizer,
        orchestrator_factory=partial(engine,path=str(tmp_path)))
    with TestClient(app,headers={'Authorization':'Bearer '+f.token()}) as client:
        accepted=client.post('/api/core/v1/completed-notes',json=f.intake)
        assert accepted.status_code==200,accepted.text
        target={'encounterId':accepted.json()['result']['receipt']['encounterId'],
                'expectedSource':accepted.json()['source']['headerSource']}
        op=str(uuid4());request=f.wrapper('generate',{**target,'operationId':op,'responseMode':'async','continuationConsent':True})
        token=f.token(exp=int(datetime.now(timezone.utc).timestamp())+10) if outcome=='workload_expired' else f.token()
        started=client.post('/api/core/v1/header/generate',json=request,headers={'Authorization':'Bearer '+token})
        assert started.status_code==200,started.text
        assert started.json()['result']['generation']['status']=='running'
        wait_until(lambda:(tmp_path/'calls.txt').exists())
        # Subsequent calls are quick; the first valid call deliberately lasts
        # past the initial 30-second provider authority.
        (tmp_path/'control.json').write_text('{"delay":0.1}')
        if outcome in ('continued','late'):
            received=datetime.now(timezone.utc) if outcome=='continued' else initial+timedelta(seconds=1)
            (tmp_path/'continuations.json').write_text(json.dumps([{'receivedAt':received.isoformat(),
                'authorizedUntil':(now+timedelta(seconds=60)).isoformat(),'requestSha256':digest(request)}]))
        wait_until(lambda:not app.state.header_generation._children,timeout=40)
        result=client.post('/api/core/v1/header/generation-status',json=f.wrapper('generation-status',{**target,'operationId':op}))
        assert result.status_code==200,result.text
        expected='succeeded' if outcome=='continued' else 'unknown'
        assert result.json()['result']['generation']['status']==expected
        assert len((tmp_path/'calls.txt').read_text().splitlines())==(2 if outcome=='continued' else 1)
        with f.context.connection() as db:
            row=db.execute('SELECT count(*) AS n FROM billing_headers WHERE practice_id=%s',(settings.practice_id,)).fetchone()
            assert row['n']==(1 if outcome=='continued' else 0)
        if outcome=='continued':
            assert datetime.now(timezone.utc)>initial
            assert result.json()['result']['view']['header']['generation']['modelInputProof']['sourceOperationId']==f.operation['operationId']
        if outcome!='workload_expired':
            assert json.loads((tmp_path/'completed.json').read_text())=={
                'operation':f.operation['operationId'],'requestSha':digest(request),'status':expected}


def test_disabled_receiver_refuses_opt_in_before_authorization_or_claim(tmp_path):
    if not DSN: pytest.skip('requires explicitly owned local async database')
    f=prepare_core_fixture(DSN,tmp_path,async_generation=True)
    app=create_core_bridge(settings=f.settings,policy=f.policy,verifier=f.verifier,authorizer=f.authorizer,
        orchestrator_factory=lambda *_args,**_kwargs:f.orchestrator)
    with TestClient(app,headers={'Authorization':'Bearer '+f.token()}) as client:
        accepted=client.post('/api/core/v1/completed-notes',json=f.intake)
        assert accepted.status_code==200,accepted.text
        target={'encounterId':accepted.json()['result']['receipt']['encounterId'],
                'expectedSource':accepted.json()['source']['headerSource']}
        count=len(f.calls)
        response=client.post('/api/core/v1/header/generate',json=f.wrapper('generate',{
            **target,'operationId':str(uuid4()),'responseMode':'async','continuationConsent':True}))
        assert response.status_code==409,response.text
        assert len(f.calls)==count
        for invalid in (None,False,1):
            response=client.post('/api/core/v1/header/generate',json=f.wrapper('generate',{
                **target,'operationId':str(uuid4()),'responseMode':'async','continuationConsent':invalid}))
            assert response.status_code==422,response.text
            assert response.json()=={'detail':'invalid_core_request'} and len(f.calls)==count
        # The runtime intentionally has no table SELECT; inspect only as owner.
        with PostgresContext(DSN,practice_id=f.settings.practice_id).connection() as db:
            assert db.execute('SELECT count(*) AS n FROM billing_header_generations WHERE practice_id=%s',(f.settings.practice_id,)).fetchone()['n']==0
