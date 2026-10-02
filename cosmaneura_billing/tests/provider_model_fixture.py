"""Both registered source lanes in one practice; all vendor/model I/O is fake."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from athena_production_fixture import prepare, app_settings
from model_admission_fixture import AuthoredAzureResponses
from provider_admission_support import source_for, registration
from test_provider_handoff import envelope
from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.billing_header import HeaderActor, utc_now
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.llm import AzureOpenAILLM
from cosmaneura_billing.model_admission import parse_policy
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.persistence import PostgresAccessAuditStore, PostgresEncounterStore
from cosmaneura_billing.provider_admission_cli import register
from cosmaneura_billing.provider_handoff import HandoffRequest, prepare_handoff, sha256
from cosmaneura_billing.provider_handoff_store import PostgresHandoffStore


def accept_provider(f, payload):
    prepared = prepare_handoff(HandoffRequest.model_validate(payload), f.owner.practice_id)
    grant = registration(prepared, f.actor, f.source_connection,
        expiresAt=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat())
    register(f.owner, "grant", grant)
    event = AccessAuditEvent(str(uuid4()), utc_now(), f.owner.practice_id, f.actor.tenant_id, f.actor.user_id,
        (f.actor.role,), "provider_handoff.accept", "", "/api/provider/encounters/handoff", "POST", 200, "allowed", "127.0.0.1")
    PostgresAccessAuditStore(f.context).append(replace(event, outcome="attempted"))
    result = PostgresHandoffStore(f.context).accept(prepared, f.actor, audit_completion=event)
    return result, grant


def prepare_provider_model_fixture(dsn, path, *, data_class="synthetic", synthetic_only=False):
    import psycopg
    with psycopg.connect(dsn) as db:
        assert db.info.dbname == "cab_provider_model_test_20261002_r1" and db.info.host == "127.0.0.1" and db.info.port == 32800
    owner, context, source_actor, service, audit, vendor_calls = prepare(dsn)
    actor = HeaderActor(source_actor.tenant_id, source_actor.user_id, "Authored synthetic Admin", "Admin")
    source = {**source_for(actor), "dataClass": data_class}
    register(owner, "connection", source)
    f = SimpleNamespace(owner=owner, context=context, actor=actor, source_actor=source_actor,
        source_connection=source, service=service, audit=audit, vendor_calls=vendor_calls)
    payload = envelope()
    payload["note"].update(text=SAMPLE_VISIT.signed_note, sha256=sha256(SAMPLE_VISIT.signed_note))
    payload["supportingDocuments"] = [{"kind":kind,"id":kind+"-1","version":"1","text":text,"sha256":sha256(text)}
        for kind,text in (("intake",SAMPLE_VISIT.intake_text),("transcript",SAMPLE_VISIT.transcript)) if text]
    payload["visit"].update(patientAge=SAMPLE_VISIT.patient_age,
        patientSex={"male": "M", "female": "F"}.get(SAMPLE_VISIT.patient_sex, SAMPLE_VISIT.patient_sex),
        encounterType=SAMPLE_VISIT.encounter_type, placeOfService=SAMPLE_VISIT.place_of_service,
        providerSpecialty=SAMPLE_VISIT.provider_specialty, serviceDate="2026-09-28")
    f.receipt, f.source_grant = accept_provider(f, payload)
    f.payload = payload
    f.stored = PostgresEncounterStore(context).get(f.receipt["receipt"]["encounterId"])
    settings = replace(app_settings(owner, context, path), provider_handoff_enabled=False,
        azure_openai_enabled=True, azure_openai_synthetic_only=synthetic_only,
        azure_client_id="33333333-3333-4333-8333-333333333333",
        azure_openai_endpoint="https://authored-cab-model.openai.azure.com",
        azure_openai_deployment="authored-cab", azure_openai_model="gpt-5.6-sol")
    policy = {"schemaVersion":"cab-model-data-policy.v2", "policyId":str(uuid4()),
        "reviewRef":"authored-local-synthetic-model-policy", "serverPracticeId":owner.practice_id,
        "tenantId":actor.tenant_id, "deploymentId":settings.athena_deployment_id, "storageId":settings.athena_storage_id,
        "databaseName":"cab_provider_model_test_20261002_r1", "runtimeRole":owner.database_runtime_role,
        "sources":{"athena_staff_review_operational":{}, "provider_completed_note":{
            "connectionId":source["connectionId"], "connectionVersion":source["version"], "dataClass":data_class}},
        "notBefore":(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
        "expiresAt":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
        "azureOpenAI":{"endpoint":settings.azure_openai_endpoint,"deployment":settings.azure_openai_deployment,
            "model":settings.azure_openai_model,"apiVersion":settings.azure_openai_api_version,
            "managedIdentityClientId":settings.azure_client_id}}
    f.settings = replace(settings, model_data_policy_json=json.dumps(policy))
    f.policy = parse_policy(f.settings.model_data_policy_json)
    f.fake = AuthoredAzureResponses()
    f.orchestrator = BillingOrchestrator(settings=f.settings, llm=AzureOpenAILLM(settings=f.settings, client=f.fake, max_retries=0))
    return f
