"""Reusable authored operational-source fixture; all Azure/vendor calls are fake."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from athena_production_fixture import CASE, prepare, command, app_settings
from test_athena_review import apply_command, billing_command
from cosmaneura_billing.billing_header import HeaderActor
from cosmaneura_billing.llm import AzureOpenAILLM, MockLLM
from cosmaneura_billing.model_admission import parse_policy
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.persistence import PostgresEncounterStore


class AuthoredAzureResponses:
    """Exercise the real adapter over two existing authored demo extractions."""
    def __init__(self):
        self.calls = []
        self.responses = self
        self.before_request = None

    def create(self, **request):
        self.calls.append(request)
        if self.before_request:
            self.before_request(len(self.calls))
        result = MockLLM().complete_json(request['instructions'], request['input'])
        return SimpleNamespace(status='completed', output_text=json.dumps(result), output=[], error=None,
                               model='gpt-5.6-sol', usage=None)


def prepare_model_fixture(dsn, path):
    owner, context, source_actor, service, audit, vendor_calls = prepare(dsn)
    view = service.refresh(CASE, source_actor, command(service, source_actor), audit=audit('athena.refresh'))['view']
    applied = service.apply(CASE, source_actor, apply_command(view), audit=audit('athena.apply'))['view']
    listing = service.billing_read(CASE, source_actor, department_id='1')
    note = service.billing_read(CASE, source_actor, department_id='1', encounter_id='88001',
        discovery_token=listing['pagination']['selectionToken'])['note']
    imported = service.billing_apply(CASE, source_actor, billing_command(applied, note),
        audit=audit('athena.billing_apply'))['view']['billingImport']
    stored = PostgresEncounterStore(context).get(imported['encounterId'])
    actor = HeaderActor(source_actor.tenant_id, source_actor.user_id, 'Authored synthetic Admin', 'Admin')
    settings = replace(app_settings(owner, context, path), azure_openai_enabled=True,
        azure_client_id='33333333-3333-4333-8333-333333333333',
        azure_openai_endpoint='https://authored-cab-model.openai.azure.com',
        azure_openai_deployment='authored-cab', azure_openai_model='gpt-5.6-sol')
    with context.connection() as db:
        db_name = db.execute('SELECT current_database() AS name').fetchone()['name']
    policy = {'schemaVersion':'cab-model-data-policy.v1', 'policyId':str(uuid4()),
        'reviewRef':'authored-local-synthetic-policy-only', 'serverPracticeId':owner.practice_id,
        'tenantId':actor.tenant_id, 'deploymentId':settings.athena_deployment_id,
        'storageId':settings.athena_storage_id, 'databaseName':db_name,
        'runtimeRole':owner.database_runtime_role, 'source':stored.source,
        'notBefore':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
        'expiresAt':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
        'azureOpenAI':{'endpoint':settings.azure_openai_endpoint, 'deployment':settings.azure_openai_deployment,
            'model':settings.azure_openai_model, 'apiVersion':settings.azure_openai_api_version,
            'managedIdentityClientId':settings.azure_client_id}}
    settings = replace(settings, model_data_policy_json=json.dumps(policy))
    fake = AuthoredAzureResponses()
    llm = AzureOpenAILLM(settings=settings, client=fake)
    orchestrator = BillingOrchestrator(settings=settings, llm=llm)
    return SimpleNamespace(owner=owner, context=context, actor=actor, service=service, stored=stored,
        settings=settings, policy=parse_policy(settings.model_data_policy_json), fake=fake,
        orchestrator=orchestrator, vendor_calls=vendor_calls)
