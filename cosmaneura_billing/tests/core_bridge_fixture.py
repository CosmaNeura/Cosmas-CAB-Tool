"""Reusable authored source and local JWT/Core/Azure seams; no external calls."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from hashlib import sha256 as content_sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import jwt
import psycopg
from psycopg.conninfo import make_conninfo
from cryptography.hazmat.primitives.asymmetric import rsa

from cosmaneura_billing.auth import CoreWorkloadPolicy, CoreWorkloadAuthenticator
from cosmaneura_billing.core_bridge import CoreBridgePolicy, create_core_bridge
from cosmaneura_billing.core_bridge_contract import digest, CoreBridgeError
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.llm import AzureOpenAILLM
from cosmaneura_billing.model_admission import parse_policy
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.persistence import PostgresContext
from cosmaneura_billing.provider_admission_cli import register_core
from cosmaneura_billing.provider_handoff import sha256
from cosmaneura_billing.settings import Settings


class AuthoredCoreResponses:
    """Source-exact facts for the note-only fixture through the real adapter."""
    def __init__(self):
        self.responses, self.calls, self.before_request = self, [], None

    def create(self, **request):
        assert SAMPLE_VISIT.signed_note in request["input"]
        self.calls.append(request)
        if self.before_request: self.before_request(len(self.calls))
        quote="Hematochezia - refer for diagnostic colonoscopy, FOBT today"
        if "TASK: extract_clinical_facts" in request["input"]:
            result={"chief_complaint":"Intermittent rectal bleeding", "mdm_problem_events":[
                {"name":"Hematochezia","source":"signed_note","identity_quote":quote,"addressed_quote":quote,"status_quote":""}],
                "mdm_data_events":[],"mdm_risk_events":[]}
        else:
            assert "TASK: propose_diagnoses" in request["input"]
            result={"diagnoses":[{"concept":"Hemorrhage of anus and rectum","primary":True,"quote":quote}]}
        return SimpleNamespace(status="completed",output_text=json.dumps(result),output=[],error=None,model="gpt-5.6-sol",usage=None)


def prepare_core_fixture(dsn, path, *, data_class="synthetic", core_identity=None):
    with psycopg.connect(dsn) as db:
        assert db.info.host == "127.0.0.1" and db.info.port == 32800
        assert db.info.dbname.startswith("cab_core_bridge_test_")
        database = db.info.dbname
        expected = {p.name: content_sha256(p.read_bytes()).hexdigest()
                    for p in (Path(__file__).resolve().parents[1] / "migrations").glob("*.up.sql")}
        assert dict(db.execute("SELECT name,checksum_sha256 FROM cosmaneura_schema_migrations").fetchall()) == expected
    practice, tenant, ordinary = "core-synthetic-"+uuid4().hex, str(uuid4()), str(uuid4())
    owner = Settings(environment="test", database_url=dsn, practice_id=practice, entra_tenant_id=tenant,
        database_runtime_role="core_bridge_"+uuid4().hex[:14], database_runtime_password="disposable-local-only")
    with psycopg.connect(dsn) as db:
        db.execute("INSERT INTO practices(practice_id,entra_tenant_id) VALUES(%s,%s::uuid)", (practice,tenant))
        db.execute("INSERT INTO users(tenant_id,user_id) VALUES(%s::uuid,%s::uuid)", (tenant,ordinary))
        db.execute("INSERT INTO practice_memberships(practice_id,tenant_id,user_id,roles) VALUES(%s,%s,%s,ARRAY['Provider']::text[])",(practice,tenant,ordinary))
    inbound=(CoreWorkloadPolicy(core_identity["outgoing"]["tenantId"],core_identity["outgoing"]["audience"],
        core_identity["outgoing"]["callerId"],core_identity["outgoing"]["role"]) if core_identity else
        CoreWorkloadPolicy(str(uuid4()), "api://authored-cab-bridge", str(uuid4()), "CAB.Core.Provider"))
    policy=CoreBridgePolicy(inbound,"https://authored-core.example","api://authored-core/.default",str(uuid4()))
    now=datetime.now(timezone.utc)
    source={"connectionId":str(uuid4()),"version":1,"tenantId":tenant,"sourceMode":"core_workload_verified",
            "dataClass":data_class,"reviewRef":"authored-local-core-source-only"}
    issuer,subject,core_practice=(core_identity["actor"]["issuer"],core_identity["actor"]["subject"],core_identity["practiceId"]) if core_identity else ("https://authored-clerk.example","authored-provider","practice_authored")
    register_core(owner,"core-connection",{"connection":source,"corePracticeId":core_practice,"actorIssuer":issuer,
        "workload":asdict(inbound),"notBefore":(now-timedelta(minutes=1)).isoformat(),"expiresAt":(now+timedelta(hours=1)).isoformat()})
    projected=register_core(owner,"core-actor",{"connectionId":source["connectionId"],"issuer":issuer,"subject":subject})
    context=PostgresContext(make_conninfo(dsn,user=owner.database_runtime_role),practice_id=practice)
    note=SAMPLE_VISIT.signed_note
    content=digest([["Authored clinical note",note]])
    operation={"operationId":str(uuid4()),"practiceId":core_practice,
        "actor":{"issuer":issuer,"subject":subject,"role":"Provider","sessionSha256":sha256("authored-local-session")},
        "source":{"noteId":"note_authored","appointmentId":"appointment_authored","noteSchemaId":"schema_authored",
            "revision":1,"contentSha256":content,"textSha256":sha256(note),"projectionVersion":"ordered-note-markdown.v1","excludedRecordingTime":False},
        "noteText":note,"visit":{"patientAge":SAMPLE_VISIT.patient_age,"patientSex":{"male":"M","female":"F"}.get(SAMPLE_VISIT.patient_sex,SAMPLE_VISIT.patient_sex),
            "encounterType":SAMPLE_VISIT.encounter_type,"placeOfService":SAMPLE_VISIT.place_of_service,
            "providerSpecialty":SAMPLE_VISIT.provider_specialty,"serviceDate":"2026-09-28"},
        "completion":{"status":"completed","confirmedByProvider":True,"completedAt":now.isoformat()},
        "admission":{"admissionId":"admission_authored","dataClass":data_class,"reviewRef":"authored-core-source-review","contentSha256":content}}
    settings=replace(owner,state_store="postgres",database_url=context.dsn,billing_header_enabled=True,
        provider_handoff_enabled=True,auth_mode="container_apps",allowed_roles=("Provider",),
        review_context_signing_key="authored-review-key-000000000000000000000000000000000000000000",
        azure_openai_enabled=True,azure_openai_synthetic_only=True,azure_language_enabled=False,
        azure_language_shadow_enabled=False,telemetry_enabled=False,cpt_auto_fetch=False,
        azure_client_id=str(uuid4()),azure_openai_endpoint="https://authored-cab-model.openai.azure.com",
        azure_openai_deployment="authored-cab",azure_openai_model="gpt-5.6-sol",
        athena_deployment_id="authored-core-runtime",athena_storage_id="authored-core-storage",
        feedback_path=str(Path(path)/"feedback.jsonl"))
    model={"schemaVersion":"cab-model-data-policy.v2","policyId":str(uuid4()),"reviewRef":"authored-local-model-review",
        "serverPracticeId":practice,"tenantId":tenant,"deploymentId":settings.athena_deployment_id,"storageId":settings.athena_storage_id,
        "databaseName":database,"runtimeRole":owner.database_runtime_role,
        "sources":{"provider_completed_note":{"connectionId":source["connectionId"],"connectionVersion":1,"dataClass":data_class}},
        "notBefore":(now-timedelta(minutes=1)).isoformat(),"expiresAt":(now+timedelta(hours=1)).isoformat(),
        "azureOpenAI":{"endpoint":settings.azure_openai_endpoint,"deployment":settings.azure_openai_deployment,
            "model":settings.azure_openai_model,"apiVersion":settings.azure_openai_api_version,"managedIdentityClientId":settings.azure_client_id}}
    settings=replace(settings,model_data_policy_json=json.dumps(model))
    fake=AuthoredCoreResponses()
    orchestrator=BillingOrchestrator(settings=settings,llm=AzureOpenAILLM(settings=settings,client=fake,max_retries=0))
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    claims={"iss":inbound.issuer,"aud":inbound.audience,"tid":inbound.tenant_id,"azp":inbound.caller_id,
        "roles":[inbound.role],"ver":"2.0","idtyp":"app","iat":int(now.timestamp())-1,"nbf":int(now.timestamp())-1,"exp":int(now.timestamp())+1800}
    def token(**changes):
        return jwt.encode({**claims,**changes},key,algorithm="RS256",headers={"kid":"authored-key"})
    f=SimpleNamespace(owner=owner,context=context,settings=settings,policy=policy,source=source,operation=operation,
        projection=projected,ordinary=ordinary,key=key,token=token,fake=fake,orchestrator=orchestrator,
        calls=[],deny=False,response_change=None,model_policy=parse_policy(settings.model_data_policy_json))
    def authorizer(op,purpose,request_sha):
        f.calls.append(purpose)
        if f.deny: raise CoreBridgeError()
        current=datetime.now(timezone.utc)
        response={"schemaVersion":"core-cab-authorization.v1","allowed":True,"operationId":op["operationId"],"purpose":purpose,
            "requestSha256":request_sha,"operationSha256":digest(op),"practiceId":op["practiceId"],
            "actor":{k:op["actor"][k] for k in ("issuer","subject","role")},"source":deepcopy(op["source"]),"admission":deepcopy(op["admission"]),
            "authorizedAt":current.isoformat(),"expiresAt":(current+timedelta(seconds=25)).isoformat()}
        if f.response_change: f.response_change(response)
        return response
    f.verifier=CoreWorkloadAuthenticator(inbound,signing_key=lambda kid:key.public_key() if kid=="authored-key" else None)
    f.authorizer=authorizer
    f.app=create_core_bridge(settings=settings,policy=policy,verifier=f.verifier,authorizer=authorizer,
        orchestrator_factory=lambda *a,**k:orchestrator)
    f.intake={"schemaVersion":"core-cab-workload.v1","purpose":"completed-note.accept","operation":operation,"expectedSource":None}
    def wrapper(action,command):
        return {"schemaVersion":"core-cab-workload.v1","purpose":"header."+action,"sourceOperationId":operation["operationId"],
            "practiceId":operation["practiceId"],"actor":{k:operation["actor"][k] for k in ("issuer","subject")},"command":command}
    f.wrapper=wrapper
    return f
