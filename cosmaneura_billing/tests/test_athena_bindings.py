"""Authored identity fixtures only; no vendor or credential access."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cosmaneura_billing.athena_bindings import AthenaBindingError, PostgresAthenaBindingStore, decode_scope
from cosmaneura_billing.athena_bindings_cli import _manifest, parse_manifest, main
from cosmaneura_billing.athena_review import ReviewActor
from cosmaneura_billing.athena_staff_contract import AthenaStaffProfile, AthenaStaffScope


TENANT = "e9329ff5-cb9a-4a96-8931-abdf90c53e0f"
ACTOR = ReviewActor(TENANT, "3605f9c2-fd78-4c3b-9be4-9bb445a90b04", "BillingStaff")
PROFILE = AthenaStaffProfile("synthetic-practice", "production", "112233", 1)
SCOPE = AthenaStaffScope(PROFILE, "synthetic-case", "opaque-ref-🧪", "4455", 1)


def result(scope=SCOPE):
    p = scope.profile
    return {"tenantId": TENANT, "serverPracticeId": p.server_practice_id,
        "environment": p.environment, "vendorPracticeId": p.vendor_practice_id,
        "connectionRevision": p.connection_revision, "profileSha256": p.fingerprint,
        "caseId": scope.case_id, "patientRef": scope.patient_ref, "sourcePatientId": scope.source_patient_id,
        "bindingRevision": scope.binding_revision, "scopeSha256": scope.fingerprint}


def decode(value):
    return decode_scope(value, practice_id=PROFILE.server_practice_id, case_id=SCOPE.case_id, tenant_id=TENANT)


def test_exact_server_scope_reconstructs_frozen_identity_without_guessing_patient():
    assert decode(result()) == SCOPE
    assert decode(result()).patient_ref != decode(result()).source_patient_id
    assert decode(result(replace(SCOPE, source_patient_id="60178"))).profile.environment == "production"


@pytest.mark.parametrize("key,value", [("tenantId",str(uuid4())), ("serverPracticeId","other"),
    ("caseId","another"), ("patientRef","4455"), ("sourcePatientId","4456"),
    ("connectionRevision",True), ("bindingRevision",2), ("scopeSha256","0"*64),
    ("profileSha256","0"*64), ("environment","preview"), ("vendorPracticeId","195900")])
def test_mismatch_or_forged_scope_cannot_pass_exact_digest_validation(key,value):
    with pytest.raises(AthenaBindingError, match="^binding_invalid$"):
        decode({**result(),key:value})


@pytest.mark.parametrize("value", [None, [], {}, {"error":[]}, {"error":"other"},
    {**result(),"synthetic":True}, {k:v for k,v in result().items() if k!="patientRef"}])
def test_result_shape_is_closed(value):
    with pytest.raises(AthenaBindingError): decode(value)


@pytest.mark.parametrize("code", ["not_authorized","not_found","connection_missing","connection_revoked",
    "binding_missing","binding_revoked","binding_stale"])
def test_closed_errors_retain_staff_usable_reason_without_source_ids(code):
    with pytest.raises(AthenaBindingError,match=f"^{code}$"): decode({"error":code})


class FakeDB:
    def __init__(self): self.calls=[]; self.open=False; self.autocommit=False
    def execute(self,query,params):
        assert self.open
        self.calls.append((query,params))
        return SimpleNamespace(fetchone=lambda:{"scope":result()})


def test_context_manager_owns_scope_transaction_and_in_transaction_does_not_commit():
    db=FakeDB()
    @contextmanager
    def connection():
        db.open=True
        try: yield db
        finally: db.open=False
    context=SimpleNamespace(practice_id=PROFILE.server_practice_id,connection=connection)
    store=PostgresAthenaBindingStore(context)
    with store.scope(SCOPE.case_id,ACTOR) as bound:
        assert db.open and bound==SCOPE
        assert store.in_transaction(db,SCOPE.case_id,ACTOR)==SCOPE
    assert not db.open and len(db.calls)==2
    assert db.calls[0][1]==(ACTOR.tenant_id,ACTOR.user_id,ACTOR.role,SCOPE.case_id)


def test_autocommit_cannot_claim_to_hold_a_returned_scope():
    db=FakeDB(); db.open=True; db.autocommit=True
    store=PostgresAthenaBindingStore(SimpleNamespace(practice_id=PROFILE.server_practice_id))
    with pytest.raises(AthenaBindingError): store.in_transaction(db,SCOPE.case_id,ACTOR)
    assert db.calls==[]


def manifest(action="register-connection"):
    common={"schemaVersion":"athena-staff-registration.v1","serverPracticeId":PROFILE.server_practice_id,
            "tenantId":TENANT,"reviewRef":"private-review-reference"}
    fields={"register-connection":{"environment":"production","vendorPracticeId":"112233","connectionRevision":1},
        "register-binding":{"caseId":SCOPE.case_id,"patientRef":SCOPE.patient_ref,"sourcePatientId":"4455",
                            "connectionRevision":1,"bindingRevision":1},
        "revoke-connection":{"connectionRevision":1},
        "revoke-binding":{"caseId":SCOPE.case_id,"bindingRevision":1}}
    return common|fields[action]


@pytest.mark.parametrize("action",["register-connection","register-binding","revoke-connection","revoke-binding"])
def test_private_manifest_exact_practice_tenant_and_shape(action):
    settings=SimpleNamespace(practice_id=PROFILE.server_practice_id,entra_tenant_id=TENANT)
    assert _manifest(settings,action,manifest(action))==manifest(action)
    for field,value in (("tenantId",str(uuid4())),("serverPracticeId","other"),
                        ("synthetic",True),("schemaVersion","other")):
        with pytest.raises(ValueError): _manifest(settings,action,manifest(action)|{field:value})


@pytest.mark.parametrize("revision",[True,False,"1",0,-1,2**31,1.5])
def test_operator_revision_does_not_accept_bool_or_coercion(revision):
    settings=SimpleNamespace(practice_id=PROFILE.server_practice_id,entra_tenant_id=TENANT)
    with pytest.raises(ValueError): _manifest(settings,"register-connection",manifest()|{"connectionRevision":revision})


@pytest.mark.parametrize("raw",['{"tenantId":"a","tenantId":"b"}','{"nested":{"x":1,"x":2}}','{"x":NaN}'])
def test_private_manifest_parser_rejects_duplicates_and_nonfinite_values(raw):
    with pytest.raises(ValueError): parse_manifest(raw)


def test_cli_failure_does_not_print_manifest_value_or_database_error(tmp_path,monkeypatch,capsys):
    path=tmp_path/"private.json"; path.write_text('{"secret":"do-not-echo"}')
    monkeypatch.setattr("sys.argv",["athena_bindings_cli","register-connection",str(path)])
    monkeypatch.setattr("cosmaneura_billing.athena_bindings_cli.Settings.from_env",
                        lambda: (_ for _ in ()).throw(RuntimeError("do-not-echo")))
    assert main()==1
    assert capsys.readouterr().out=="Athena identity registration failed\n"
