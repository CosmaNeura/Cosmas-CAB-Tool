"""Fake official-origin transport and isolated operator-owned synthetic scope."""
from datetime import datetime,timedelta,timezone
import io,json,re
from pathlib import Path
from urllib.request import Request
from uuid import uuid4
import psycopg

from athena_configured_fixture import CASE,prepare as base_prepare,registration,app_settings as base_settings
from cosmaneura_billing.athena_bindings_cli import apply
from cosmaneura_billing.athena_client import AthenaStaffReadClient,MockAthenaStaffReadClient,ATHENA_PREVIEW_SCOPE
from cosmaneura_billing.athena_review_routes import make_athena_service

PACKAGE=Path(__file__).resolve().parents[1]
DEPLOYMENT,STORAGE='synthetic-production-runtime','synthetic-local-storage'

def prepare_schema(dsn):
    with psycopg.connect(dsn,autocommit=True) as db:
        assert db.info.host=='127.0.0.1' and db.info.port==32800 and db.info.dbname.startswith('cab_athena_billing_admission_test_')
        assert db.execute("SELECT count(*) FROM cosmaneura_schema_migrations").fetchone()[0] == 17
        p=PACKAGE/'migrations/023_athena_read_admission.up.sql'
        for name,body in re.findall(r'CREATE(?: OR REPLACE)? FUNCTION public\.(\w+)\(.*?AS \$\$(.*?)\$\$;',p.read_text(),re.S):
            actual=db.execute("SELECT prosrc FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname=%s",(name,)).fetchone()
            assert actual and actual[0].strip()==body.strip(),'dedicated schema differs from final023'

def wire(calls):
    mock=MockAthenaStaffReadClient()
    def open_fake(request,timeout):
        assert request.full_url.startswith('https://api.platform.athenahealth.com/')
        calls.append((request.method,request.full_url.split('.com',1)[1]))
        if request.full_url.endswith('/oauth2/v1/token'):
            payload={'access_token':'authored-synthetic-token','token_type':'Bearer','expires_in':300,'scope':ATHENA_PREVIEW_SCOPE}
        else:
            assert request.method=='GET'
            url=request.full_url.replace('api.platform.','api.preview.platform.').replace('/112233/','/195900/').replace('/4455','/60178')
            payload=json.loads(mock._open(Request(url),timeout).read().replace(b'"60178"',b'"4455"'))
        result=io.BytesIO(json.dumps(payload).encode());result.status=200;result.headers={'Content-Type':'application/json'}
        result.geturl=lambda:request.full_url;return result
    return open_fake

def prepare(dsn,*,admit=True):
    owner,context,actor,_,audit=base_prepare(dsn,register=False)
    registration(owner,environment='production',vendorPracticeId='112233')
    registration(owner,'register-binding',sourcePatientId='4455')
    calls=[]
    def factory(scope,lease):return AthenaStaffReadClient(scope=scope,credential_environment='production',read_lease=lease,
        client_id='authored-fixture',client_secret='authored-fixture-unused',urlopen=wire(calls))
    service=make_athena_service(mode='configured',practice_id=owner.practice_id,chase_store=None,database_context=context,
        credential_environment='production',configured_client_factory=factory,signing_key=b'synthetic-production-discovery-01',
        deployment_id=DEPLOYMENT,storage_id=STORAGE)
    if admit:register_admission(owner,context,actor,service)
    return owner,context,actor,service,audit,calls

def register_admission(owner,context,actor,service,*,revision=1,**changes):
    current=service.current(CASE,actor)
    value={'schemaVersion':'athena-staff-registration.v1','serverPracticeId':owner.practice_id,'tenantId':actor.tenant_id,
        'reviewRef':'explicit-authored-synthetic-test-only','admissionId':str(uuid4()),'admissionRevision':revision,
        'caseId':CASE,'scopeSha256':current['connection']['binding']['scopeSha256'],'runtimeRole':owner.database_runtime_role,
        'deploymentId':DEPLOYMENT,'storageId':STORAGE,'notBefore':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
        'expiresAt':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),**changes}
    apply(owner,'register-read-admission',value);return value

def command(service,actor):
    current=service.current(CASE,actor)
    return {'operationId':str(uuid4()),'expectedRevision':current['view']['revision'],
        'expectedBindingSha256':current['connection']['binding']['scopeSha256'],
        'expectedAdmissionSha256':current['connection']['admission']['grantSha256']}

def app_settings(owner,context,frontend):
    from dataclasses import replace
    return replace(base_settings(owner,context,frontend),athena_credential_environment='production',
                   athena_deployment_id=DEPLOYMENT,athena_storage_id=STORAGE,cpt_auto_fetch=False)
