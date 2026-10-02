"""Consumed Core authority and unchanged CAB pipeline/receipt boundaries."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from cosmaneura_billing.auth import PrincipalError, PrincipalRoleError, AuthPrincipal
from cosmaneura_billing.billing_header import create_draft
from cosmaneura_billing.core_bridge import create_core_bridge
from cosmaneura_billing.core_bridge_contract import CoreOperation
from cosmaneura_billing.persistence import PostgresMembershipStore, PostgresEncounterStore
from core_bridge_fixture import prepare_core_fixture

DSN=os.environ.get("CAB_CORE_BRIDGE_TEST_DSN","")

@pytest.fixture
def setup(tmp_path):
    if not DSN: pytest.skip("requires the owned Core bridge PostgreSQL fixture")
    f=prepare_core_fixture(DSN,tmp_path)
    with TestClient(f.app,headers={"Authorization":"Bearer "+f.token()}) as client:
        yield f,client


def post(client,path,payload):
    response=client.post(path,json=payload)
    assert response.status_code==200,response.text
    return response.json()


def accept(f,client):
    return post(client,"/api/core/v1/completed-notes",f.intake)


def command(accepted):
    return {"encounterId":accepted["result"]["receipt"]["encounterId"],"expectedSource":accepted["source"]["headerSource"]}


def test_disabled_has_no_bridge_routes():
    with TestClient(create_core_bridge()) as client:
        assert client.get("/api/live").json()["bridge"]=="disabled"
        assert client.post("/api/core/v1/completed-notes",json={}).status_code==404


@pytest.mark.parametrize("change",[{"aud":"wrong"},{"azp":str(uuid4())},{"roles":["unrelated"]},{"idtyp":"user"},{"scp":"read"},{"exp":1}])
def test_real_signed_workload_rejects_wrong_authority(setup,change):
    f,client=setup
    response=client.post("/api/core/v1/completed-notes",json=f.intake,headers={"Authorization":"Bearer "+f.token(**change)})
    assert response.status_code==401 and not f.calls and not f.fake.calls


def test_actual_source_to_edit_and_exact_retry_after_model_policy_removal(setup):
    f,client=setup
    accepted=accept(f,client)
    assert accepted["source"]["inferencePolicy"]=={"allowed":True,"reason":None}
    assert accepted["source"]["noteText"]==f.operation["noteText"] and not f.fake.calls
    assert accepted["result"]["launchPath"] is None
    assert accept(f,client)["result"]["replayed"] is True
    target=command(accepted)
    read=post(client,"/api/core/v1/header/read",f.wrapper("read",target))
    assert read["source"]["inferencePolicy"]["allowed"] and read["result"]["view"] is None and not f.fake.calls
    generate=f.wrapper("generate",{**target,"operationId":str(uuid4())})
    generated=post(client,"/api/core/v1/header/generate",generate)
    view=generated["result"]["view"]
    assert len(f.fake.calls)==2 and generated["result"]["created"]
    assert view["header"]["generation"]["modelInputProof"]["sourceOperationId"]==f.operation["operationId"]
    assert view["header"]["createdBy"]["identity"]=={"kind":"core","issuer":f.operation["actor"]["issuer"],
        "subject":f.operation["actor"]["subject"],"projectionId":f.projection["projectionId"],"mappingVersion":1}
    draft=create_draft(view["header"],view["revision"]); draft["note"]="Authored provider review; clinical source unchanged."
    save=f.wrapper("save",{**target,"operationId":str(uuid4()),"headerId":view["header"]["headerId"],
        "expectedRevision":view["revisionNumber"],"draft":draft,"reason":"Provider clarified the billing review note."})
    edited=post(client,"/api/core/v1/header/save",save)
    assert edited["result"]["view"]["revision"]["draft"]==draft and edited["result"]["view"]["revisionNumber"]==1
    assert post(client,"/api/core/v1/header/read",f.wrapper("read",target))["result"]["view"]==edited["result"]["view"]
    f.app.state.model_data_policy=None
    f.app.state.settings=replace(f.settings,model_data_policy_json="")
    assert post(client,"/api/core/v1/header/read",f.wrapper("read",target))["source"]["inferencePolicy"]["allowed"] is False
    retry=post(client,"/api/core/v1/header/generate",generate)
    assert retry["result"]["created"] is False and len(f.fake.calls)==2
    assert post(client,"/api/core/v1/header/save",save)["result"]["created"] is False
    next_draft=deepcopy(draft);next_draft["note"]="Review continues while model access is unavailable."
    next_save=f.wrapper("save",{**save["command"],"draft":next_draft,"operationId":str(uuid4()),"expectedRevision":1})
    assert post(client,"/api/core/v1/header/save",next_save)["result"]["view"]["revisionNumber"]==2
    assert len(f.fake.calls)==2
    assert PostgresEncounterStore(f.context).get(target["encounterId"]).bundle.signed_note==f.operation["noteText"]
    if os.environ.get("CAB_CORE_BRIDGE_EVIDENCE"):
        out=Path(os.environ["CAB_CORE_BRIDGE_EVIDENCE"]);out.mkdir(parents=True,exist_ok=True)
        for name,value in (("accepted",accepted),("generated-header",generated),("edited-header",edited)):
            (out/(name+".json")).write_text(json.dumps(value,indent=2,ensure_ascii=False)+"\n")


def test_core_authority_source_binding_and_no_impersonation(setup):
    f,client=setup
    with pytest.raises(PrincipalError): f.verifier.authenticate([(b"authorization",b"Bearer "+f.token().encode())]*2)
    forged=deepcopy(f.intake);forged["operation"]["completion"]["confirmedByProvider"]=1
    assert client.post("/api/core/v1/completed-notes",json=forged).status_code==422
    f.response_change=lambda r:r.update(requestSha256="0"*64)
    assert client.post("/api/core/v1/completed-notes",json=f.intake).status_code==403
    f.response_change=None
    accepted=accept(f,client)
    f.deny=True
    assert client.post("/api/core/v1/header/generate",json=f.wrapper("generate",{**command(accepted),"operationId":str(uuid4())})).status_code==403
    assert not f.fake.calls
    import psycopg
    with psycopg.connect(f.owner.database_url) as db:
        denied=db.execute("SELECT count(*) FROM access_audit_completions WHERE practice_id=%s AND status=403 AND outcome='denied'",(f.owner.practice_id,)).fetchone()[0]
        assert denied>=2
    memberships=PostgresMembershipStore(f.context,tenant_id=f.owner.entra_tenant_id)
    with pytest.raises(PrincipalRoleError):
        memberships.authorize(AuthPrincipal(f.projection["internalUserId"],f.owner.entra_tenant_id,f.owner.practice_id,frozenset({"Provider"})))
    assert memberships.authorize(AuthPrincipal(f.ordinary,f.owner.entra_tenant_id,f.owner.practice_id,frozenset({"Provider"}))).user_id==f.ordinary
    with f.context.connection() as db:
        assert not db.execute("SELECT has_table_privilege(current_user,'core_actor_projections','INSERT') AS ok").fetchone()["ok"]
        assert not db.execute("SELECT has_table_privilege(current_user,'provider_source_admissions','INSERT') AS ok").fetchone()["ok"]
        assert not db.execute("SELECT has_table_privilege(current_user,'encounters','INSERT') AS ok").fetchone()["ok"]
        assert not db.execute("SELECT has_function_privilege(current_user,'cosmaneura_commit_provider_handoff_019(jsonb)','EXECUTE') AS ok").fetchone()["ok"]


def test_current_source_cas_and_superseded_receipt_are_explicit(setup):
    f,client=setup; first=accept(f,client); old=deepcopy(f.intake)
    fresh=deepcopy(old);fresh["operation"]["operationId"]=str(uuid4());fresh["operation"]["source"]["revision"]=2
    fresh["operation"]["visit"]["serviceDate"]="2026-09-29"
    next_source=post(client,"/api/core/v1/completed-notes",fresh)
    assert next_source["source"]["encounterVersion"]==2
    assert next_source["source"]["visitContext"]["serviceDate"]=="2026-09-29"
    same_revision=deepcopy(fresh);same_revision["operation"]["operationId"]=str(uuid4())
    assert client.post("/api/core/v1/completed-notes",json=same_revision).status_code==409
    changed_retry=deepcopy(old);changed_retry["operation"]["source"]["excludedRecordingTime"]=True
    assert client.post("/api/core/v1/completed-notes",json=changed_retry).status_code==409
    retry=post(client,"/api/core/v1/completed-notes",old)
    assert retry["result"]["current"] is False and retry["source"] is None
    assert client.post("/api/core/v1/header/read",json=f.wrapper("read",command(first))).status_code==409
    assert not f.fake.calls


@pytest.mark.parametrize("when",["next_egress","before_save"])
def test_core_revocation_stops_egress_or_header_persistence(setup,when):
    f,client=setup; accepted=accept(f,client)
    if when=="next_egress":
        f.fake.before_request=lambda count:setattr(f,"deny",True)
    else:
        original=f.orchestrator.process
        def process(*args,**kwargs):
            result=original(*args,**kwargs);f.deny=True;return result
        f.orchestrator.process=process
    response=client.post("/api/core/v1/header/generate",json=f.wrapper("generate",{**command(accepted),"operationId":str(uuid4())}))
    assert response.status_code==403
    assert len(f.fake.calls)==(1 if when=="next_egress" else 2)
    with f.context.connection() as db:
        assert db.execute("SELECT count(*) AS n FROM billing_headers").fetchone()["n"]==0
    from cosmaneura_billing import core_bridge, model_admission
    assert core_bridge._ACTIVE.get() is None and model_admission._ACTIVE.get() is None


def test_mapping_revocation_and_expired_authorization_fail_closed(setup):
    from datetime import datetime,timedelta,timezone
    from cosmaneura_billing.provider_admission_cli import revoke
    f,client=setup
    f.response_change=lambda r:r.update(authorizedAt=(datetime.now(timezone.utc)-timedelta(seconds=31)).isoformat(),
        expiresAt=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    assert client.post("/api/core/v1/completed-notes",json=f.intake).status_code==403
    f.response_change=None; accept(f,client)
    revoke(f.owner,"core-actor",f.projection["projectionId"])
    assert client.post("/api/core/v1/completed-notes",json=f.intake).status_code==403
    assert not f.fake.calls


def test_bridge_role_cannot_bypass_source_or_immutable_identity(setup):
    import psycopg
    f,client=setup; accepted=accept(f,client)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(f.context.dsn) as db:
        db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)",(f.owner.practice_id,))
        db.execute("UPDATE encounters SET signed_note=signed_note WHERE external_id=%s",(accepted["source"]["encounterId"],))
    with pytest.raises(psycopg.errors.RaiseException),psycopg.connect(f.owner.database_url) as db:
        db.execute("UPDATE users SET identity_kind='entra' WHERE tenant_id=%s AND user_id=%s",(f.owner.entra_tenant_id,f.projection["internalUserId"]))
    with psycopg.connect(f.owner.database_url) as db:
        assert db.execute("SELECT count(*) FROM access_audit_attempts WHERE practice_id=%s",(f.owner.practice_id,)).fetchone()[0]>=1
        assert db.execute("SELECT count(*) FROM access_audit_completions WHERE practice_id=%s",(f.owner.practice_id,)).fetchone()[0]>=1


def test_small_core_clock_skew_does_not_invent_future_storage_authority(setup):
    from datetime import datetime,timedelta,timezone
    f,client=setup
    f.response_change=lambda r:r.update(authorizedAt=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(),
        expiresAt=(datetime.now(timezone.utc)+timedelta(seconds=26)).isoformat())
    assert accept(f,client)["source"]["inferencePolicy"]["allowed"] is True
    assert not f.fake.calls


def test_authenticated_core_denial_is_definitive_not_an_ambiguous_retry(setup):
    from types import SimpleNamespace
    from urllib.error import HTTPError
    from cosmaneura_billing.core_bridge import CoreOperationAuthorizer
    from cosmaneura_billing.core_bridge_contract import CoreBridgeError,digest
    f,_=setup; wire=object.__new__(CoreOperationAuthorizer);wire.policy=f.policy
    wire.credential=SimpleNamespace(get_token=lambda scope:SimpleNamespace(token="authored-test-only"))
    def denied(request,timeout):
        assert request.full_url.startswith(f.policy.core_origin+"/internal/cab/v1/operations/")
        raise HTTPError(request.full_url,403,"denied",{},None)
    wire.opener=SimpleNamespace(open=denied)
    with pytest.raises(CoreBridgeError) as result:wire(f.operation,"completed-note.accept",digest(f.intake))
    assert result.value.status_code==403 and not f.fake.calls
