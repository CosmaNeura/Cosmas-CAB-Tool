"""Restricted-role configured source review, receipts and nested source binding."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import os
from threading import Barrier
from uuid import uuid4

import pytest
psycopg=pytest.importorskip("psycopg")
from athena_configured_fixture import CASE,prepare_schema,prepare,registration,app_settings
from cosmaneura_billing.athena_review import AthenaReviewError,AthenaReviewService
from cosmaneura_billing.athena_review_store import _PostgresUnit
from cosmaneura_billing.athena_client import MockAthenaStaffReadClient
from cosmaneura_billing.persistence import PostgresEncounterStore,PersistenceUnavailable
from test_athena_review import apply_command,billing_command,refresh_command

DSN=os.environ.get("CAB_ATHENA_CONFIGURED_TEST_DSN","")
pytestmark=pytest.mark.skipif(not DSN,reason="explicit dedicated local configured Athena database required")

@pytest.fixture(scope="module",autouse=True)
def schema(): prepare_schema(DSN)

@pytest.fixture
def setup(): return prepare(DSN)

def command(service,actor):
    current=service.current(CASE,actor)
    return {"operationId":str(uuid4()),"expectedRevision":current["view"]["revision"],
            "expectedBindingSha256":current["connection"]["binding"]["scopeSha256"]}

def refresh(setup):
    owner,context,actor,service,audit=setup
    cmd=command(service,actor)
    return service.refresh(CASE,actor,cmd,audit=audit("athena.refresh")),cmd

def test_registered_preview_review_to_import_and_exact_superseded_receipts(setup):
    owner,context,actor,service,audit=setup
    service.store.healthcheck()
    source,cmd=refresh(setup)
    view=source["view"]
    assert view["schemaVersion"]=="athena-case-review.v2" and source["connection"]["currentReviewMatchesBinding"]
    applied=service.apply(CASE,actor,apply_command(view),audit=audit("athena.apply"))["view"]
    listing=service.billing_read(CASE,actor,department_id="1")
    assert listing["sourceBinding"]==view["sourceBinding"] and listing["pagination"]["selectionToken"]
    note=service.billing_read(CASE,actor,department_id="1",encounter_id="88001",discovery_token=listing["pagination"]["selectionToken"])["note"]
    bc=billing_command(applied,note)
    imported=service.billing_apply(CASE,actor,bc,audit=audit("athena.billing_apply"))
    row=PostgresEncounterStore(context).get(imported["view"]["billingImport"]["encounterId"])
    assert row.bundle.signed_note==note["plainText"] and row.source=="athena_staff_review_preview_synthetic"
    replay=service.refresh(CASE,actor,cmd,audit=audit("athena.refresh"))
    assert replay["view"]==imported["view"] and replay["operationReceipt"]["revision"]==1
    assert replay["operationReceipt"]["replayed"] and not replay["operationReceipt"]["current"]
    registration(owner,"revoke-binding")
    service._factory=lambda _:pytest.fail("replay cannot construct client")
    recovered=service.billing_apply(CASE,actor,bc,audit=audit("athena.billing_apply"))
    assert recovered["operationReceipt"]["replayed"] and recovered["connection"]["state"]=="binding_revoked"
    assert recovered["view"]==imported["view"]

@pytest.mark.parametrize("override",[None,"60178","60179"])
def test_caller_patient_override_including_null_rejected_before_client(setup,override):
    _,_,actor,service,audit=setup
    service._factory=lambda _:pytest.fail("caller cannot choose patient")
    with pytest.raises(AthenaReviewError,match="invalid_request"):
        service.refresh(CASE,actor,{**command(service,actor),"sourcePatientId":override},audit=audit("athena.refresh"))
    assert service.current(CASE,actor)["view"]["revision"]==0

def test_missing_revoked_production_and_environment_states_never_construct_client():
    owner,context,actor,service,audit=prepare(DSN,register=False)
    service._factory=lambda _:pytest.fail("inactive mapping cannot construct client")
    assert service.current(CASE,actor)["connection"]["state"]=="connection_missing"
    registration(owner,environment="production",vendorPracticeId="112233")
    assert service.current(CASE,actor)["connection"]["state"]=="binding_missing"
    registration(owner,"register-binding",sourcePatientId="999")
    assert service.current(CASE,actor)["connection"]["state"]=="read_not_admitted"
    with pytest.raises(AthenaReviewError): service.refresh(CASE,actor,command(service,actor),audit=audit("athena.refresh"))
    registration(owner,"revoke-connection")
    assert service.current(CASE,actor)["connection"]["state"]=="connection_revoked"

def test_mapping_change_invalidates_new_work_and_case_patient_redacts_old_view(setup):
    owner,context,actor,service,audit=setup
    source,cmd=refresh(setup)
    service.apply(CASE,actor,apply_command(source["view"]),audit=audit("athena.apply"))
    registration(owner,"register-binding",bindingRevision=2,sourcePatientId="60179")
    assert service.current(CASE,actor)["connection"]["state"]=="changed"
    with pytest.raises(AthenaReviewError,match="stale_review"):
        service.apply(CASE,actor,apply_command(source["view"]),audit=audit("athena.apply"))
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE chase_items SET patient_ref='another-opaque-patient' WHERE practice_id=%s",(owner.practice_id,))
    current=service.current(CASE,actor)
    assert current["view"]["revision"]==2 and current["view"]["redacted"] and current["view"]["candidate"] is None
    with pytest.raises(AthenaReviewError,match="stale_review"):
        service.refresh(CASE,actor,cmd,audit=audit("athena.refresh"))
    registration(owner,"register-binding",bindingRevision=3,patientRef="another-opaque-patient",sourcePatientId="60179")
    changed=service.refresh(CASE,actor,command(service,actor),audit=audit("athena.refresh"))["view"]
    assert changed["candidate"]["source"]["patientId"]=="60179" and changed["applied"] is None and changed["billingImport"] is None

@pytest.mark.parametrize("edit",[
    lambda v:v["candidate"]["source"].update(patientId="60179"),
    lambda v:v["candidate"]["sourceBinding"].update(bindingRevision=999),
    lambda v:v.pop("sourceBinding"),
    lambda v:v.update(schemaVersion="athena-case-review.v1"),
    lambda v:v.update(applied={"relabeled":"historical"}),
    lambda v:v.pop("revision"),
    lambda v:v.update(revision=None),
    lambda v:v.update(revision="2"),
    lambda v:v.update(revision=2.5),
])
def test_database_rejects_forged_nested_source_and_downgrade(setup,monkeypatch,edit):
    _,context,actor,service,audit=setup
    refresh(setup)
    original=context.Jsonb
    def corrupt(view):
        view=deepcopy(view);edit(view);return original(view)
    # Corrupt only the JSON payload at the DB boundary: the separately typed
    # row revision stays valid so the trigger itself must reject bad payloads.
    monkeypatch.setattr(context,"Jsonb",corrupt)
    with pytest.raises(PersistenceUnavailable):
        service.refresh(CASE,actor,command(service,actor),audit=audit("athena.refresh"))
    assert service.current(CASE,actor)["view"]["revision"]==1

def test_different_operations_serialize_before_scope_share_to_update_and_one_wins(setup):
    _,_,actor,service,audit=setup
    commands=[command(service,actor),command(service,actor)]
    events=[audit("athena.refresh"),audit("athena.refresh")]
    barrier=Barrier(2)
    def work(index):
        barrier.wait(timeout=5)
        try: return service.refresh(CASE,actor,commands[index],audit=events[index])["view"]["revision"]
        except AthenaReviewError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(work,range(2)))
    assert sorted(map(str,results))==["1","stale_review"]

def test_not_found_authority_and_legacy_receipt_compatibility(setup):
    _,context,actor,service,audit=setup
    with pytest.raises(AthenaReviewError,match="not_found"):service.current("absent",actor)
    with pytest.raises(AthenaReviewError,match="not_authorized"):service.current(CASE,replace(actor,user_id=str(uuid4())))
    legacy=AthenaReviewService(service.store,MockAthenaStaffReadClient())
    cmd=refresh_command()
    initial=legacy.refresh(CASE,actor,cmd,audit=audit("athena.refresh"))
    applied=legacy.apply(CASE,actor,apply_command(initial),audit=audit("athena.apply"))
    note=legacy.billing_read(CASE,actor,department_id="1",encounter_id="88001")["note"]
    bc=billing_command(applied,note)
    legacy.billing_apply(CASE,actor,bc,audit=audit("athena.billing_apply"))
    configured,_=refresh(setup)
    recovered=service.refresh(CASE,actor,cmd,audit=audit("athena.refresh"))
    assert recovered["view"]==configured["view"] and recovered["operationReceipt"]["sourceBinding"] is None
    recovered=service.billing_apply(CASE,actor,bc,audit=audit("athena.billing_apply"))
    assert recovered["view"]==configured["view"] and recovered["operationReceipt"]["revision"]==3
    with pytest.raises(AthenaReviewError,match="stale_review"):
        legacy.refresh(CASE,actor,refresh_command(4),audit=audit("athena.refresh"))

def test_real_api_configured_contract_null_override_and_readiness(setup,tmp_path):
    import base64,json
    from fastapi.testclient import TestClient
    from cosmaneura_billing.api import create_app
    owner,context,actor,service,audit=setup
    app=create_app(settings=app_settings(owner,context,tmp_path),frontend_dist=tmp_path)
    app.state.athena_review_service=service
    claims={"auth_typ":"aad","claims":[{"typ":"oid","val":actor.user_id},
        {"typ":"tid","val":actor.tenant_id},{"typ":"roles","val":"Admin"}]}
    headers={"X-MS-CLIENT-PRINCIPAL":base64.b64encode(json.dumps(claims).encode()).decode()}
    with TestClient(app,headers=headers) as client:
        current=client.post("/api/athena/card-chase",json={"chaseId":CASE})
        assert current.status_code==200,current.text
        assert current.json()["connection"]["state"]=="ready"
        payload={"chaseId":CASE,**command(service,actor)}
        assert client.post("/api/athena/card-chase/read",json={**payload,"sourcePatientId":None}).status_code==422
        result=client.post("/api/athena/card-chase/read",json=payload)
        assert result.status_code==200,result.text
        assert result.json()["operationReceipt"]["current"]
        assert client.post("/api/athena/card-chase",json={"chaseId":"absent"}).status_code==404
        assert client.get("/api/ready").status_code==200
        with psycopg.connect(DSN) as db:
            db.execute("ALTER TABLE athena_case_review_events DISABLE TRIGGER athena_configured_review_guard")
        try: assert client.get("/api/ready").status_code==503
        finally:
            with psycopg.connect(DSN) as db:
                db.execute("ALTER TABLE athena_case_review_events ENABLE ALWAYS TRIGGER athena_configured_review_guard")

def test_source_operation_holds_mapping_against_revocation_until_completion(setup):
    owner,_,actor,service,audit=setup
    factory=service._factory
    def checked(scope):
        client=factory(scope);read=client.read_card
        def held(patient):
            with pytest.raises(psycopg.errors.LockNotAvailable):
                with psycopg.connect(DSN) as db:
                    db.execute("SET LOCAL lock_timeout='100ms'")
                    db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(owner.practice_id,))
                    db.execute("UPDATE athena_staff_case_bindings SET revocation_ref='synthetic-revoke' WHERE practice_id=%s",(owner.practice_id,))
            return read(patient)
        client.read_card=held;return client
    service._factory=checked
    refresh(setup)
    registration(owner,"revoke-binding")
    assert service.current(CASE,actor)["connection"]["state"]=="binding_revoked"

@pytest.mark.parametrize("values",[{"sourcePatientId":"60185"},{"vendorPracticeId":"195901"}])
def test_unadmitted_preview_mapping_is_visible_without_client_construction(values):
    owner,_,actor,service,_=prepare(DSN,register=False)
    registration(owner,**({"vendorPracticeId":values["vendorPracticeId"]} if "vendorPracticeId" in values else {}))
    registration(owner,"register-binding",**({"sourcePatientId":values["sourcePatientId"]} if "sourcePatientId" in values else {}))
    service._factory=lambda _:pytest.fail("outside existing Preview cohort")
    assert service.current(CASE,actor)["connection"]["state"]=="read_not_admitted"

@pytest.mark.parametrize("kind",["apply","billing_apply"])
def test_new_decision_nested_binding_and_import_atomicity(setup,monkeypatch,kind):
    _,context,actor,service,audit=setup
    source,_=refresh(setup)
    request=apply_command(source["view"])
    if kind=="billing_apply":
        applied=service.apply(CASE,actor,request,audit=audit("athena.apply"))["view"]
        note=service.billing_read(CASE,actor,department_id="1",encounter_id="88001")["note"]
        request=billing_command(applied,note)
    previous=service.current(CASE,actor)["view"]
    original=_PostgresUnit.save
    def corrupt(self,view,*args,**kwargs):
        view=deepcopy(view)
        view["applied" if kind=="apply" else "billingImport"]["sourceBinding"]["sourcePatientId"]="60179"
        return original(self,view,*args,**kwargs)
    monkeypatch.setattr(_PostgresUnit,"save",corrupt)
    with pytest.raises(PersistenceUnavailable):getattr(service,kind)(CASE,actor,request,audit=audit("athena."+kind))
    assert service.current(CASE,actor)["view"]==previous
    assert not PostgresEncounterStore(context).all()

def test_discovery_token_is_invalid_after_same_patient_mapping_revision(setup):
    owner,_,actor,service,audit=setup
    source,_=refresh(setup)
    service.apply(CASE,actor,apply_command(source["view"]),audit=audit("athena.apply"))
    token=service.billing_read(CASE,actor,department_id="1")["pagination"]["selectionToken"]
    registration(owner,"register-binding",bindingRevision=2)
    source,_=refresh(setup)
    service.apply(CASE,actor,apply_command(source["view"]),audit=audit("athena.apply"))
    with pytest.raises(AthenaReviewError,match="stale_discovery"):
        service.billing_read(CASE,actor,department_id="1",encounter_id="88001",discovery_token=token)

def test_utf8_case_binding_preserves_unicode_quote_and_backslash():
    from cosmaneura_billing.athena_review import digest
    fixture=prepare(DSN,register=False,practice_suffix='-Café-🧪"\\')
    owner,context,actor,service,audit=fixture
    patient='opaque-Café-🧪"\\'
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE chase_items SET patient_ref=%s WHERE practice_id=%s",(patient,owner.practice_id))
        serialized=db.execute("SELECT '['||to_json(%s::text)::text||','||to_json(%s::text)::text||']'",(owner.practice_id,patient)).fetchone()[0]
    import hashlib
    assert hashlib.sha256(serialized.encode()).hexdigest()==digest([owner.practice_id,patient])
    registration(owner);registration(owner,"register-binding",patientRef=patient)
    result,_=refresh(fixture)
    assert result["view"]["caseBinding"]==digest([owner.practice_id,patient])
    assert service.apply(CASE,actor,apply_command(result["view"]),audit=audit("athena.apply"))["view"]["revision"]==2
