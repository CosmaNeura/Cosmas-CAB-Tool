"""Consumed production admission through fake transport and real restricted PG."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import os
from uuid import uuid4
import pytest
psycopg=pytest.importorskip('psycopg')
from athena_production_fixture import CASE,prepare_schema,prepare,register_admission,command,app_settings
from athena_configured_fixture import registration
from cosmaneura_billing.athena_admission import AthenaAdmissionError
from cosmaneura_billing.athena_bindings_cli import apply
from cosmaneura_billing.athena_review import AthenaReviewError
from cosmaneura_billing.athena_client import AthenaStaffReadClient,AthenaOperationUnavailable
from cosmaneura_billing.persistence import PostgresEncounterStore,PersistenceUnavailable,ATHENA_REVIEW_OPERATIONAL_SOURCE,DEPLOYABLE_SYNTHETIC_INFERENCE_SOURCES
from test_athena_review import apply_command,billing_command

DSN=os.environ.get('CAB_ATHENA_PRODUCTION_TEST_DSN','')
pytestmark=pytest.mark.skipif(not DSN,reason='explicit isolated local production-contract fixture required')
@pytest.fixture(scope='module',autouse=True)
def schema():prepare_schema(DSN)
@pytest.fixture
def setup():return prepare(DSN)
def refresh(setup):
    owner,context,actor,service,audit,calls=setup
    request=command(service,actor)
    return service.refresh(CASE,actor,request,audit=audit('athena.refresh')),request

def test_consumed_admission_to_operational_import_exact_replay_and_model_denial(setup,tmp_path):
    import base64,json
    from fastapi.testclient import TestClient
    from cosmaneura_billing.api import create_app
    owner,context,actor,service,audit,calls=setup
    service.store.healthcheck();source,request=refresh(setup)
    view=source['view'];proof=view['sourceAdmission']
    assert view['schemaVersion']=='athena-case-review.v3' and view['mode']=='production'
    assert view['candidate']['source']['syntheticOnly'] is False and view['candidate']['source']['dataClass']=='operational'
    assert view['candidate']['sourceAdmission']==proof and view['candidate']['source']['patientId']=='4455'
    applied=service.apply(CASE,actor,apply_command(view),audit=audit('athena.apply'))['view']
    listing=service.billing_read(CASE,actor,department_id='1')
    assert listing['sourceAdmission']==proof
    note=service.billing_read(CASE,actor,department_id='1',encounter_id='88001',discovery_token=listing['pagination']['selectionToken'])['note']
    bc=billing_command(applied,note)
    imported=service.billing_apply(CASE,actor,bc,audit=audit('athena.billing_apply'))
    encounter=imported['view']['billingImport']['encounterId'];row=PostgresEncounterStore(context).get(encounter)
    assert row.source==ATHENA_REVIEW_OPERATIONAL_SOURCE and row.bundle.signed_note==note['plainText']
    assert row.source not in DEPLOYABLE_SYNTHETIC_INFERENCE_SOURCES
    before=len(calls)
    assert service.refresh(CASE,actor,request,audit=audit('athena.refresh'))['operationReceipt']['replayed']
    assert len(calls)==before
    app=create_app(settings=app_settings(owner,context,tmp_path),frontend_dist=tmp_path);app.state.athena_review_service=service
    claims={'auth_typ':'aad','claims':[{'typ':'oid','val':actor.user_id},{'typ':'tid','val':actor.tenant_id},{'typ':'roles','val':'Admin'}]}
    with TestClient(app,headers={'X-MS-CLIENT-PRINCIPAL':base64.b64encode(json.dumps(claims).encode()).decode()}) as client:
        detail=client.post('/api/encounters/detail',json={'encounterId':encounter}).json()
        assert detail['inferencePolicy']=={'allowed':False,'reason':'operational_source_model_not_approved'}
        result=client.post('/api/encounters/header/generate',json={'encounterId':encounter,'expectedSource':detail['headerSource'],'operationId':str(uuid4())})
        assert result.status_code==403 and 'operational_source_model_not_approved' in result.text
    with pytest.raises(PersistenceUnavailable):PostgresEncounterStore(context).upsert(row.bundle,source=ATHENA_REVIEW_OPERATIONAL_SOURCE)

def test_missing_and_latest_mismatched_admission_never_constructs_client():
    fixture=prepare(DSN,admit=False);owner,context,actor,service,audit,calls=fixture
    assert service.current(CASE,actor)['connection']['admission'] is None
    register_admission(owner,context,actor,service)
    register_admission(owner,context,actor,service,revision=2,deploymentId='wrong-runtime')
    assert service.current(CASE,actor)['connection']['state']=='read_not_admitted' and calls==[]

@pytest.mark.parametrize('end',['commit','rollback','replace','close','exit'])
def test_lease_cannot_survive_its_exact_transaction_even_cached_token(setup,end):
    owner,context,actor,service,audit,calls=setup
    with service.store.unit(CASE,actor,resolve_binding=True) as unit:
        lease,scope=unit.read_lease,unit.bound_scope
        client=service._factory(scope,lease);client.read_card('4455');before=len(calls)
        if end in ('commit','rollback','close'):getattr(unit.conn,end)()
        elif end=='replace':unit.conn.rollback();unit.conn.execute('SELECT 1')
        if end!='exit':
            with pytest.raises(AthenaOperationUnavailable):client.read_card('4455')
            assert len(calls)==before
    with pytest.raises(AthenaOperationUnavailable):client.read_card('4455')
    assert len(calls)==before

def test_expiry_rechecked_after_source_before_persistence_and_newer_grants_stale(setup):
    owner,context,actor,service,audit,calls=setup
    source,request=refresh(setup)
    register_admission(owner,context,actor,service,revision=2)
    assert service.current(CASE,actor)['connection']['state']=='changed'
    with pytest.raises(AthenaReviewError):service.apply(CASE,actor,apply_command(source['view']),audit=audit('athena.apply'))
    expired=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    register_admission(owner,context,actor,service,revision=3,notBefore=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),expiresAt=expired)
    assert service.current(CASE,actor)['connection']['state']=='read_not_admitted'
    before=len(calls)
    assert service.refresh(CASE,actor,request,audit=audit('athena.refresh'))['operationReceipt']['replayed']
    assert len(calls)==before

@pytest.mark.parametrize('change',[lambda v:v['sourceAdmission'].update(grantSha256='0'*64),
    lambda v:v['candidate']['source'].update(syntheticOnly=True),lambda v:v.update(schemaVersion='athena-case-review.v2')])
def test_database_rejects_admission_forgery_and_synthetic_relabel(setup,monkeypatch,change):
    owner,context,actor,service,audit,calls=setup
    refresh(setup);original=context.Jsonb
    def corrupt(value):value=deepcopy(value);change(value);return original(value)
    monkeypatch.setattr(context,'Jsonb',corrupt)
    with pytest.raises(PersistenceUnavailable):service.refresh(CASE,actor,command(service,actor),audit=audit('athena.refresh'))
    assert service.current(CASE,actor)['view']['revision']==1

@pytest.mark.parametrize('stage',['before_save','deferred_commit'])
def test_expiry_during_operation_rolls_back_at_final_boundaries(setup,monkeypatch,stage):
    import time
    from cosmaneura_billing.athena_review_store import _PostgresUnit
    owner,context,actor,service,audit,calls=setup
    register_admission(owner,context,actor,service,revision=2,expiresAt=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    original=_PostgresUnit.save
    def delayed(self,*args,**kwargs):
        if stage=='before_save':time.sleep(1.1)
        result=original(self,*args,**kwargs)
        if stage=='deferred_commit':time.sleep(1.1)
        return result
    monkeypatch.setattr(_PostgresUnit,'save',delayed)
    with pytest.raises((AthenaReviewError,PersistenceUnavailable)):
        refresh(setup)
    assert any(method=='GET' for method,_ in calls)
    assert service.current(CASE,actor)['view']['revision']==0

def test_runtime_cannot_register_or_mutate_admission_and_revocation_serializes(setup):
    from dataclasses import replace
    from psycopg import sql
    owner,context,actor,service,audit,calls=setup
    with context.connection() as db:
        for privilege in ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE'):
            assert not db.execute("SELECT has_table_privilege(current_user,'athena_staff_read_admissions',%s) AS ok",(privilege,)).fetchone()['ok']
    current=service.current(CASE,actor)['connection']['admission']
    revoke={'schemaVersion':'athena-staff-registration.v1','serverPracticeId':owner.practice_id,'tenantId':actor.tenant_id,
        'reviewRef':'synthetic-revocation','caseId':CASE,'admissionId':current['admissionId']}
    with pytest.raises(PermissionError):apply(replace(owner,database_url=context.dsn),'revoke-read-admission',revoke)
    source,request=refresh(setup)
    with service.store.unit(CASE,actor,resolve_binding=True):
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with psycopg.connect(DSN) as db:
                db.execute("SET LOCAL lock_timeout='100ms'")
                db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
                db.execute("UPDATE athena_staff_read_admissions SET revocation_ref='synthetic' WHERE practice_id=%s",(owner.practice_id,))
    apply(owner,'revoke-read-admission',revoke)
    assert service.current(CASE,actor)['connection']['state']=='read_not_admitted'
    before=len(calls)
    assert service.refresh(CASE,actor,request,audit=audit('athena.refresh'))['operationReceipt']['replayed']
    assert len(calls)==before
    with pytest.raises(psycopg.Error,match='immutable'):
        with psycopg.connect(DSN) as db:
            db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
            db.execute("UPDATE athena_staff_read_admissions SET document=document||'{\"deploymentId\":\"forged\"}'::jsonb WHERE practice_id=%s",(owner.practice_id,))

def test_v3_cannot_return_to_preview_or_reuse_discovery_after_new_grant(setup):
    owner,context,actor,service,audit,calls=setup
    source,_=refresh(setup)
    service.apply(CASE,actor,apply_command(source['view']),audit=audit('athena.apply'))
    token=service.billing_read(CASE,actor,department_id='1')['pagination']['selectionToken']
    register_admission(owner,context,actor,service,revision=2)
    source,_=refresh(setup)
    service.apply(CASE,actor,apply_command(source['view']),audit=audit('athena.apply'))
    before=len(calls)
    with pytest.raises(AthenaReviewError,match='stale_discovery'):
        service.billing_read(CASE,actor,department_id='1',encounter_id='88001',discovery_token=token)
    assert len(calls)==before
    registration(owner,connectionRevision=2)
    registration(owner,'register-binding',connectionRevision=2,bindingRevision=2)
    assert service.current(CASE,actor)['connection']['state']=='read_not_admitted'

def test_caller_proof_is_not_a_transport_lease_and_mapper_context_is_typed(setup):
    from cosmaneura_billing.athena_card_chase import CardChaseReadContext,map_athena_card_chase,AthenaCardChaseError
    owner,context,actor,service,audit,calls=setup
    with service.store.unit(CASE,actor,resolve_binding=True) as unit:
        with pytest.raises(AthenaOperationUnavailable):
            AthenaStaffReadClient(scope=unit.bound_scope,credential_environment='production',read_lease=unit.read_lease.public,
                token_provider=lambda:pytest.fail('untrusted proof must not access OAuth'))
    bad=CardChaseReadContext(owner.practice_id,'4455','synthetic-batch','2026-09-30T12:00:00Z','112233',False,object())
    with pytest.raises(AthenaCardChaseError,match='invalid_context'):
        map_athena_card_chase(context=bad,patient_response=None,insurance_response=None)
    assert calls==[]
