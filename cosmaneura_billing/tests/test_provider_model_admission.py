"""Registered provider evidence shares the exact Azure guard, never a source tag bypass."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
from cosmaneura_billing import model_admission as admission
from cosmaneura_billing.billing_header import digest, source_snapshot
from cosmaneura_billing.model_admission import ModelAdmissionError, operational_scope, parse_policy
from cosmaneura_billing.persistence import PostgresEncounterStore
from cosmaneura_billing.provider_admission_cli import revoke
from test_model_admission import document, scope, run

DSN = os.environ.get("CAB_PROVIDER_MODEL_TEST_DSN", "")


def v2():
    result = document(); del result["source"]
    result.update(schemaVersion="cab-model-data-policy.v2", sources={
        admission.ATHENA_SOURCE:{}, admission.PROVIDER_SOURCE:{"connectionId":str(uuid4()),"connectionVersion":1,"dataClass":"synthetic"}})
    return result


def test_v2_sources_are_explicit_closed_and_leave_v1_canonical_bytes_unchanged():
    old = document(); expected = json.dumps(old, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert parse_policy(json.dumps(old)).sha256 == hashlib.sha256(expected.encode()).hexdigest()
    value = v2(); assert parse_policy(json.dumps(value)).document == value
    for sources in ({}, {"provider_completed_note":{}}, {"synthetic":{}},
        {admission.ATHENA_SOURCE:{"enabled":True}},
        {admission.PROVIDER_SOURCE:{**value["sources"][admission.PROVIDER_SOURCE],"dataClass":"operational"}},
        {admission.PROVIDER_SOURCE:{**value["sources"][admission.PROVIDER_SOURCE],"connectionVersion":True}}):
        with pytest.raises(ValueError, match="^invalid model data policy$"):
            parse_policy(json.dumps({**value,"sources":sources}))


@pytest.fixture(scope="module")
def schema():
    if not DSN: pytest.skip("explicit dedicated provider model database required")
    import psycopg
    with psycopg.connect(DSN) as db:
        assert db.info.dbname == "cab_provider_model_test_20261002_r1" and db.info.host == "127.0.0.1" and db.info.port == 32800
        source = (Path(__file__).resolve().parents[1]/"migrations/025_provider_model_source.up.sql").read_text()
        expected = re.search(r"AS \$\$(.*?)\$\$;", source, re.S).group(1).strip()
        assert db.execute("SELECT prosrc FROM pg_proc WHERE oid='public.cosmaneura_provider_model_source(uuid,uuid,text,text)'::regprocedure").fetchone()[0].strip() == expected


@pytest.fixture
def fixture(schema, tmp_path):
    from provider_model_fixture import prepare_provider_model_fixture
    return prepare_provider_model_fixture(DSN, tmp_path)


def test_both_sources_use_same_policy_and_full_pipeline_without_source_relabelling(fixture):
    from athena_production_fixture import CASE, command
    from test_athena_review import apply_command, billing_command
    f = fixture
    with scope(f) as permit:
        result = run(f, permit)
        assert result.inference["inferenceMode"] == "azure_openai" and not result.degraded
        assert permit.proof["schemaVersion"] == "cab-model-input-proof.v2"
        assert permit.proof["sourceOperationId"] == f.payload["operationId"]
        assert permit.proof["sourceSha256"] == digest(source_snapshot(f.stored))
        with f.context.connection() as db: permit.assert_persistable(db, f.stored)
    view = f.service.refresh(CASE,f.source_actor,command(f.service,f.source_actor),audit=f.audit("athena.refresh"))["view"]
    view = f.service.apply(CASE,f.source_actor,apply_command(view),audit=f.audit("athena.apply"))["view"]
    listing = f.service.billing_read(CASE,f.source_actor,department_id="1")
    note = f.service.billing_read(CASE,f.source_actor,department_id="1",encounter_id="88001",
        discovery_token=listing["pagination"]["selectionToken"])["note"]
    imported=f.service.billing_apply(CASE,f.source_actor,billing_command(view,note),audit=f.audit("athena.billing_apply"))["view"]["billingImport"]
    f.stored=PostgresEncounterStore(f.context).get(imported["encounterId"])
    with scope(f) as permit:
        run(f,permit)
        assert permit.proof["schemaVersion"]=="cab-model-input-proof.v1" and "sourceOperationId" not in permit.proof
    assert len(f.fake.calls)==4 and admission._ACTIVE.get() is None


@pytest.mark.parametrize("data_class,allowed", [("synthetic",True),("deidentified",False)])
def test_synthetic_only_needs_registered_and_policy_matched_synthetic_source(schema,tmp_path,data_class,allowed):
    from provider_model_fixture import prepare_provider_model_fixture
    f=prepare_provider_model_fixture(DSN,tmp_path,data_class=data_class,synthetic_only=True)
    if allowed:
        with scope(f) as permit: run(f,permit)
        assert len(f.fake.calls)==2
    else:
        with pytest.raises(ModelAdmissionError),scope(f): pass
        assert not f.fake.calls


@pytest.mark.parametrize("key,value", [("connectionId","11111111-1111-4111-8111-111111111111"),
    ("connectionVersion",2),("dataClass","deidentified")])
def test_policy_cannot_borrow_another_connection_version_or_class(fixture,key,value):
    f=fixture; policy=f.policy.document;policy["sources"][admission.PROVIDER_SOURCE][key]=value
    f.policy=parse_policy(json.dumps(policy));f.settings=replace(f.settings,model_data_policy_json=json.dumps(policy))
    with pytest.raises(ModelAdmissionError),scope(f):pass
    assert not f.fake.calls


def test_provider_external_azure_cannot_use_source_tag_without_scope_or_policy(fixture):
    f=fixture
    for settings in (replace(f.settings,model_data_policy_json=""),replace(f.settings,model_data_policy_json="",azure_openai_enabled=False)):
        f.orchestrator.settings=settings
        with pytest.raises(ModelAdmissionError):run(f,None)
    assert not f.fake.calls


def test_unapproved_secondary_cannot_use_offline_provider_path():
    from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
    from cosmaneura_billing.llm import MockLLM
    from cosmaneura_billing.orchestrator import BillingOrchestrator
    from cosmaneura_billing.settings import Settings
    orchestrator=BillingOrchestrator(settings=Settings(cpt_auto_fetch=False),llm=MockLLM())
    for field in ("secondary_coding","ta4h","shadow_runner"):
        setattr(orchestrator,field,object())
        with pytest.raises(ModelAdmissionError):orchestrator.process(SAMPLE_VISIT,input_provenance=admission.PROVIDER_SOURCE)
        setattr(orchestrator,field,None)
    # Unchanged no-policy, offline full-pipeline provider flow remains supported.
    assert orchestrator.process(SAMPLE_VISIT,input_provenance=admission.PROVIDER_SOURCE).inference["inferenceMode"]=="mock"


def test_accepted_storage_window_expiry_does_not_become_model_authority(fixture,monkeypatch):
    f=fixture
    later=admission._instant(f.source_grant["expiresAt"])+timedelta(seconds=1)
    monkeypatch.setattr(admission,"_now",lambda:later)
    with scope(f) as permit:run(f,permit)
    assert len(f.fake.calls)==2
    monkeypatch.setattr(admission,"_now",lambda:admission._instant(f.policy.document["expiresAt"])+timedelta(seconds=1))
    with pytest.raises(ModelAdmissionError),scope(f):pass


@pytest.mark.parametrize("kind",["connection","grant"])
def test_source_revocation_between_model_requests_stops_next_egress(fixture,kind):
    f=fixture
    def revoked(count):
        if count==1:revoke(f.owner,kind,f.source_connection["connectionId"] if kind=="connection" else f.payload["operationId"])
    f.fake.before_request=revoked
    with scope(f) as permit,pytest.raises(ModelAdmissionError):run(f,permit)
    assert len(f.fake.calls)==1 and admission._ACTIVE.get() is None


def test_current_source_revision_and_latest_revocation_do_not_fall_back(fixture):
    from provider_model_fixture import accept_provider
    f=fixture
    with scope(f) as permit:
        same=deepcopy(f.payload);same.update(operationId=str(uuid4()),expectedSource=f.receipt["receipt"]["headerSource"])
        receipt,_=accept_provider(f,same)
        revoke(f.owner,"grant",same["operationId"])
        with pytest.raises(ModelAdmissionError):run(f,permit)
    with pytest.raises(ModelAdmissionError),scope(f):pass
    update=deepcopy(f.payload);update.update(operationId=str(uuid4()),sourceRevision=2,expectedSource=receipt["receipt"]["headerSource"])
    accept_provider(f,update)
    with pytest.raises(ModelAdmissionError),scope(f):pass  # Old source never borrows new acceptance.
    assert not f.fake.calls


def test_final_write_guard_serializes_with_revocation_without_runtime_update_grant(fixture):
    import psycopg
    f=fixture
    with scope(f) as permit:
        run(f,permit)
        with f.context.connection() as db:
            permit.assert_persistable(db,f.stored)
            assert db.execute("SELECT has_table_privilege(current_user,'provider_source_connections','UPDATE') AS write").fetchone()["write"] is False
            with psycopg.connect(f.owner.database_url) as owner:
                owner.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    owner.execute("UPDATE provider_source_connections SET revoked_at=clock_timestamp() WHERE practice_id=%s AND connection_id=%s",
                        (f.owner.practice_id,f.source_connection["connectionId"]))
        revoke(f.owner,"connection",f.source_connection["connectionId"])
        with f.context.connection() as db,pytest.raises(ModelAdmissionError):permit.assert_persistable(db,f.stored)
