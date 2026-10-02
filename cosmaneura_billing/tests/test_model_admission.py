"""Operational model authority is consumed, source-bound, and request-local."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cosmaneura_billing import model_admission as admission
from cosmaneura_billing.billing_header import digest, source_snapshot
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.llm import AzureOpenAILLM, MockLLM
from cosmaneura_billing.model_admission import ModelAdmissionError, operational_scope, parse_policy
from cosmaneura_billing.orchestrator import BillingOrchestrator, InferenceInputPolicyError
from cosmaneura_billing.persistence import ATHENA_REVIEW_OPERATIONAL_SOURCE, PostgresContext
from cosmaneura_billing.settings import Settings

DSN = os.environ.get('CAB_MODEL_POLICY_TEST_DSN', '')


def document():
    return {'schemaVersion':'cab-model-data-policy.v1', 'policyId':str(uuid4()), 'reviewRef':'authored-local-only',
        'serverPracticeId':'practice', 'tenantId':str(uuid4()), 'deploymentId':'runtime', 'storageId':'storage',
        'databaseName':'cab_local', 'runtimeRole':'cab_runtime', 'source':ATHENA_REVIEW_OPERATIONAL_SOURCE,
        'notBefore':'2026-01-01T00:00:00Z', 'expiresAt':'2027-01-01T00:00:00Z',
        'azureOpenAI':{'endpoint':'https://authored.openai.azure.com', 'deployment':'authored', 'model':'gpt-5.6-sol',
            'apiVersion':'2025-04-01-preview', 'managedIdentityClientId':str(uuid4())}}


def test_policy_is_closed_immutable_empty_default_and_static_errors():
    assert parse_policy('') is None
    source = document(); policy = parse_policy(json.dumps(source)); changed = policy.document
    changed['source'] = 'other'
    assert policy.document == source
    for value in [' ', 'null', '[]', '{"source":1,"source":2}', json.dumps({**source, 'allowPHI':True}),
                  json.dumps({**source, 'expiresAt':'2027-01-01T00:00:00+00:99'}),
                  json.dumps({**source, 'source':'synthetic_demo_release_candidate'}),
                  json.dumps({**source, 'azureOpenAI':{**source['azureOpenAI'], 'endpoint':'https://private-canary.invalid'}})]:
        with pytest.raises(ValueError, match='^invalid model data policy$'):
            parse_policy(value)


def test_operational_source_is_denied_at_full_pipeline_without_real_scope():
    class Trap(MockLLM):
        def complete_json(self, *a, **kw):
            pytest.fail('no extraction without permit')
    orch = BillingOrchestrator(settings=Settings(cpt_auto_fetch=False), llm=Trap())
    for permit in (None, True, {}, SimpleNamespace(proof=document())):
        with pytest.raises(ModelAdmissionError):
            orch.process(SAMPLE_VISIT, input_provenance=ATHENA_REVIEW_OPERATIONAL_SOURCE, input_scope=permit)
    assert admission._ACTIVE.get() is None


@pytest.fixture(scope='module')
def schema():
    if not DSN:
        pytest.skip('explicit isolated local model-policy PostgreSQL fixture required')
    from athena_production_fixture import prepare_schema
    prepare_schema(DSN)


@pytest.fixture
def fixture(schema, tmp_path):
    from model_admission_fixture import prepare_model_fixture
    return prepare_model_fixture(DSN, tmp_path)


def scope(f):
    return operational_scope(f.policy, settings=f.settings, stored=f.stored, actor=f.actor, context=f.context)


def run(f, permit):
    return f.orchestrator.process(f.stored.bundle, user_id=f.actor.user_id,
        input_provenance=f.stored.source, input_scope=permit)


def test_actual_restricted_source_to_full_pipeline_and_same_connection_save_guard(fixture):
    f = fixture
    with scope(f) as permit:
        result = run(f, permit)
        assert result.inference['inferenceMode'] == 'azure_openai' and not result.degraded
        assert result.biller_worksheet.audit_trail and result.provider_card.captured_codes
        assert len(f.fake.calls) == 2 and all(r['store'] is False for r in f.fake.calls)
        with f.context.connection() as db:
            permit.assert_persistable(db, f.stored)
        assert permit.proof['sourceSha256'] == digest(source_snapshot(f.stored))
        with pytest.raises(ModelAdmissionError):
            run(f, permit)
    assert admission._ACTIVE.get() is None
    with f.context.connection() as db, pytest.raises(ModelAdmissionError):
        permit.assert_persistable(db, f.stored)


@pytest.mark.parametrize('field,value', [('databaseName','wrong'), ('runtimeRole','wrong'), ('serverPracticeId','wrong'),
    ('tenantId','11111111-1111-4111-8111-111111111111'), ('deploymentId','wrong'), ('storageId','wrong')])
def test_policy_cannot_relabel_actual_runtime_identity(fixture, field, value):
    f = fixture; d = f.policy.document; d[field] = value
    f.policy = parse_policy(json.dumps(d)); f.settings = replace(f.settings, model_data_policy_json=json.dumps(d))
    with pytest.raises(ModelAdmissionError), scope(f):
        pytest.fail('mismatched identity admitted')
    assert f.fake.calls == []


@pytest.mark.parametrize('field', ['endpoint','deployment','model','api_version','_managed_identity_client_id'])
def test_actual_transport_drift_fails_before_request(fixture, field):
    f = fixture
    with scope(f) as permit:
        setattr(f.orchestrator.llm, field, 'different')
        with pytest.raises(ModelAdmissionError):run(f, permit)
    assert f.fake.calls == [] and admission._ACTIVE.get() is None


def test_actual_membership_owner_role_and_forged_source_denied(fixture):
    f = fixture
    original = f.context
    f.context = PostgresContext(f.owner.database_url, practice_id=f.owner.practice_id)
    with pytest.raises(ModelAdmissionError), scope(f):pass
    f.context = original
    f.stored = replace(f.stored, bundle=replace(f.stored.bundle, signed_note='forged source'))
    with pytest.raises(ModelAdmissionError), scope(f):pass
    assert f.fake.calls == []


@pytest.mark.parametrize('collaborator', ['secondary_coding','ta4h','shadow_runner'])
def test_actual_secondary_engine_never_receives_operational_note(fixture, collaborator):
    f = fixture
    with scope(f) as permit:
        setattr(f.orchestrator, collaborator, object())
        with pytest.raises(ModelAdmissionError):run(f, permit)
    assert not f.fake.calls


def test_synthetic_only_priority_even_with_matching_policy(fixture):
    f = fixture
    with scope(f) as permit:
        f.orchestrator.settings = replace(f.settings, azure_openai_synthetic_only=True)
        with pytest.raises(InferenceInputPolicyError):run(f, permit)
    assert not f.fake.calls


@pytest.mark.parametrize('stage', ['before_first', 'retry', 'between_tasks', 'before_save'])
def test_expiry_is_checked_at_each_real_request_retry_and_save(fixture, monkeypatch, stage):
    f = fixture; original = admission._now; expired = admission._instant(f.policy.document['expiresAt']) + timedelta(seconds=1)
    def expire():monkeypatch.setattr(admission, '_now', lambda:expired)
    with scope(f) as permit:
        if stage == 'before_first':expire()
        if stage in ('retry','between_tasks'):
            def response_hook(count):
                if count == 1:
                    expire()
                    if stage == 'retry':raise TimeoutError('authored transport timeout')
            f.fake.before_request = response_hook
            monkeypatch.setattr('cosmaneura_billing.llm.time.sleep', lambda _:None)
        if stage == 'before_save':
            run(f, permit);expire()
            with f.context.connection() as db, pytest.raises(ModelAdmissionError):permit.assert_persistable(db, f.stored)
        else:
            with pytest.raises(ModelAdmissionError):run(f, permit)
    assert len(f.fake.calls) == {'before_first':0,'retry':1,'between_tasks':1,'before_save':2}[stage]
    assert admission._ACTIVE.get() is None
    monkeypatch.setattr(admission, '_now', original)


def test_source_mutation_after_admission_and_other_scope_do_not_borrow_permit(fixture):
    f = fixture
    with scope(f) as permit:
        other = replace(f.stored.bundle, encounter_id='another-encounter')
        with pytest.raises(ModelAdmissionError):
            f.orchestrator.process(other, input_provenance=f.stored.source, input_scope=permit)
    assert not f.fake.calls and admission._ACTIVE.get() is None


def test_policy_present_does_not_open_other_unapproved_source_paths(fixture):
    f = fixture
    for source in (None, "explicit_import", "local_synthetic_demo", "provider_completed_note"):
        with pytest.raises(ModelAdmissionError):
            f.orchestrator.process(f.stored.bundle, input_provenance=source)
    assert not f.fake.calls


def test_membership_revocation_between_calls_stops_next_disclosure(fixture):
    import psycopg
    f = fixture
    def revoke(count):
        if count == 1:
            with psycopg.connect(f.owner.database_url) as db:
                db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s", (f.owner.practice_id,))
    f.fake.before_request = revoke
    with scope(f) as permit, pytest.raises(ModelAdmissionError):run(f, permit)
    assert len(f.fake.calls) == 1 and admission._ACTIVE.get() is None


def test_request_context_is_reset_on_failure_and_cannot_cross_thread(fixture):
    from concurrent.futures import ThreadPoolExecutor
    f = fixture
    def fail(count):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(admission._ACTIVE.get).result() is None
        raise RuntimeError('authored extraction failure')
    f.fake.before_request = fail
    with scope(f) as permit, pytest.raises(RuntimeError, match='authored extraction failure'):
        run(f, permit)
    assert admission._ACTIVE.get() is None and len(f.fake.calls) == 1


def test_actual_sdk_origin_or_api_version_cannot_drift(fixture):
    f = fixture
    client = f.orchestrator.llm
    # Metadata-only SDK stand-in; no SDK/token/network construction.
    f.fake.base_url = client.endpoint + '/openai/'
    f.fake.default_query = {'api-version':client.api_version}
    client._injected_client = False
    assert client._operational_transport_identity()['endpoint'] == client.endpoint
    for attr, value in [('base_url','https://authored-canary.invalid/openai/'), ('default_query',{'api-version':'different'})]:
        before = getattr(f.fake, attr); setattr(f.fake, attr, value)
        with pytest.raises(ModelAdmissionError):client._operational_transport_identity()
        setattr(f.fake, attr, before)
    assert not f.fake.calls


def test_retry_budget_cannot_change_after_approval(fixture, monkeypatch):
    f = fixture
    def drift(count):
        f.orchestrator.llm.max_retries = 100
        raise TimeoutError('authored timeout')
    f.fake.before_request = drift
    monkeypatch.setattr('cosmaneura_billing.llm.time.sleep', lambda _:None)
    with scope(f) as permit, pytest.raises(ModelAdmissionError):run(f, permit)
    assert len(f.fake.calls) == 1 and admission._ACTIVE.get() is None
