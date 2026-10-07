"""Current Core authorization survives only its original bounded async intent."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from functools import partial
import json
import os
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from cosmaneura_billing.core_bridge import create_core_bridge
from cosmaneura_billing.core_bridge_contract import CoreBridgeError, digest, verify_authorization
from core_bridge_fixture import prepare_core_fixture
from header_generation_fixture import engine
from test_header_generation import wait_until

DSN=os.environ.get('CAB_HEADER_ASYNC_TEST_DSN','')


class Authorizer:
    def __init__(self,path,deadline): self.path,self.deadline=Path(path),deadline
    def __call__(self,operation,purpose,request_sha):
        if (self.path/'deny').exists(): raise CoreBridgeError()
        now=datetime.now(timezone.utc)
        value={'schemaVersion':'core-cab-authorization.v1','allowed':True,'operationId':operation['operationId'],
            'purpose':purpose,'requestSha256':request_sha,'operationSha256':digest(operation),'practiceId':operation['practiceId'],
            'actor':{k:operation['actor'][k] for k in ('issuer','subject','role')},'source':deepcopy(operation['source']),
            'admission':deepcopy(operation['admission']),'authorizedAt':now.isoformat(),'expiresAt':(now+timedelta(seconds=25)).isoformat()}
        if purpose=='header.generate':
            value['generationExpiresAt']=self.deadline
            value['expiresAt']=min(now+timedelta(seconds=25),datetime.fromisoformat(self.deadline)).isoformat()
        return value
    def complete(self,operation,request_sha,status):
        (self.path/'completed.json').write_text(json.dumps({'operation':operation['operationId'],'requestSha':request_sha,'status':status}))


@pytest.mark.parametrize('outcome',['success','revoked','policy_expired'])
def test_core_spawn_reauthorizes_original_source_and_policy(outcome,tmp_path):
    if not DSN: pytest.skip('requires explicitly owned local async test DB')
    f=prepare_core_fixture(DSN,tmp_path,async_generation=True)
    now=datetime.now(timezone.utc)
    settings=f.settings
    if outcome=='policy_expired':
        policy=json.loads(settings.model_data_policy_json);policy['expiresAt']=(now+timedelta(seconds=10)).isoformat()
        settings=replace(settings,model_data_policy_json=json.dumps(policy))
    (tmp_path/'control.json').write_text(json.dumps({'delay':12 if outcome=='policy_expired' else 6 if outcome=='revoked' else .3}))
    authorizer=Authorizer(tmp_path,(now+timedelta(seconds=60)).isoformat())
    app=create_core_bridge(settings=settings,policy=f.policy,verifier=f.verifier,authorizer=authorizer,
        orchestrator_factory=partial(engine,path=str(tmp_path)))
    with TestClient(app,headers={'Authorization':'Bearer '+f.token()}) as client:
        accepted=client.post('/api/core/v1/completed-notes',json=f.intake)
        assert accepted.status_code==200,accepted.text
        body=accepted.json();target={'encounterId':body['result']['receipt']['encounterId'],'expectedSource':body['source']['headerSource']}
        op=str(uuid4());command=f.wrapper('generate',{**target,'operationId':op,'responseMode':'async'})
        started=client.post('/api/core/v1/header/generate',json=command)
        assert started.status_code==200,started.text
        assert started.json()['result']['generation']['status']=='running'
        wait_until(lambda:(tmp_path/'calls.txt').exists())
        if outcome=='revoked': (tmp_path/'deny').touch()
        wait_until(lambda:not app.state.header_generation._children,timeout=30)
        with f.context.connection() as db:
            row=db.execute('SELECT count(*) AS n FROM billing_headers WHERE practice_id=%s',(settings.practice_id,)).fetchone()
            assert row['n']==(1 if outcome=='success' else 0)
        if outcome=='revoked': (tmp_path/'deny').unlink()
        result=client.post('/api/core/v1/header/generation-status',json=f.wrapper('generation-status',{**target,'operationId':op}))
        assert result.status_code==200,result.text
        assert result.json()['result']['generation']['status']==('succeeded' if outcome=='success' else 'unknown')
        assert len((tmp_path/'calls.txt').read_text().splitlines()) == (2 if outcome=='success' else 1)
        completion=json.loads((tmp_path/'completed.json').read_text())
        assert completion=={'operation':f.operation['operationId'],'requestSha':digest(command),
                            'status':'succeeded' if outcome=='success' else 'unknown'}
        if outcome=='success':
            assert result.json()['result']['view']['header']['generation']['modelInputProof']['sourceOperationId']==f.operation['operationId']


def test_core_deadline_is_closed_fixed_and_cannot_be_widened(tmp_path):
    from types import SimpleNamespace
    from cosmaneura_billing.core_bridge import CoreLease
    now=datetime.now(timezone.utc)
    operation={'operationId':str(uuid4()),'practiceId':'synthetic','actor':{'issuer':'https://local.example','subject':'synthetic','role':'Provider'},
               'source':{},'admission':{}}
    authorizer=Authorizer(tmp_path,(now+timedelta(seconds=60)).isoformat())
    lease=CoreLease(operation=operation,purpose='header.generate',request_sha='0'*64,principal=None,
        workload=SimpleNamespace(expires_at=now.timestamp()+300),authorizer=authorizer)
    lease.async_generation=True
    with lease.use():
        lease.recheck()
        authorizer.deadline=(now+timedelta(seconds=61)).isoformat()
        with pytest.raises(CoreBridgeError): lease.recheck()
    value=authorizer(operation,'header.generate','0'*64)
    with pytest.raises(CoreBridgeError): verify_authorization(value,operation,'header.generate','0'*64)
