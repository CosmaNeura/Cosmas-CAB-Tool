"""Admission is exact server authority, not a patient-data/source label."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from cosmaneura_billing.billing_header import digest, BillingHeaderError
from cosmaneura_billing.provider_admission import connection, grant
from cosmaneura_billing.provider_admission_cli import parse_manifest
from cosmaneura_billing.provider_handoff import HandoffError
from provider_admission_support import source_for, registration, register_memory
from test_provider_handoff import ACTOR, envelope, memory, prepared


def test_empty_registry_never_admits_a_caller_label():
    encounters, store = memory(); p = prepared(envelope())
    with pytest.raises(HandoffError, match='operational_handoff_not_approved'): store.accept(p, ACTOR)
    assert not encounters.all() and not store.operations and not store.admissions.acceptances


@pytest.mark.parametrize('change', [
    {'actorTenantId':str(uuid4())}, {'actorUserId':str(uuid4())}, {'actorRole':'Admin'},
    {'operationId':str(uuid4())}, {'sourceRevision':2}, {'recordKey':'source-encounter-v1:'+'0'*64},
    {'requestSha256':'0'*64}, {'manifestSha256':'0'*64}, {'expectedSource':{}},
    {'connectionVersion':2}, {'notBefore':'2098-01-01T00:00:00Z'},
    {'notBefore':'2000-01-01T00:00:00Z','expiresAt':'2001-01-01T00:00:00Z'},
])
def test_exact_grant_bindings_cannot_be_widened(change):
    encounters, store = memory(); p = prepared(envelope()); c = source_for(ACTOR)
    store.admissions.register_connection(c)
    store.admissions.register_grant(registration(p, ACTOR, **change))
    with pytest.raises(HandoffError, match='operational_handoff_not_approved'): store.accept(p,ACTOR)
    assert not encounters.all() and not store.heads and not store.operations


@pytest.mark.parametrize('field', ['note','context','completion','supporting','cas'])
def test_changed_source_even_with_correct_new_hash_is_not_admitted(field):
    from cosmaneura_billing.provider_handoff import sha256
    _,store=memory(); raw=envelope(); p=prepared(raw); register_memory(store,p,ACTOR)
    if field=='note': raw['note']['text']+=' Changed'; raw['note']['sha256']=sha256(raw['note']['text'])
    if field=='context': raw['visit']['patientAge']=31
    if field=='completion': raw['completion']['status']='signed'
    if field=='supporting': raw['supportingDocuments']=[{**raw['note'],'id':'intake-1','kind':'intake'}]
    if field=='cas': raw['expectedSource']={}
    with pytest.raises(HandoffError,match='operational_handoff_not_approved'): store.accept(prepared(raw),ACTOR)


@pytest.mark.parametrize('kind',['connection','grant'])
def test_revocation_blocks_new_write_but_exact_receipt_recovery_is_read_only(kind):
    _,store=memory(); p=prepared(envelope()); register_memory(store,p,ACTOR)
    one=store.accept(p,ACTOR); before=deepcopy(store.admissions.acceptances)
    store.admissions.revoke(kind, source_for(ACTOR)['connectionId'] if kind=='connection' else p.operation_id)
    assert store.accept(p,ACTOR)=={**one,'replayed':True}
    assert before==store.admissions.acceptances
    advanced=prepared(envelope(sourceRevision=2,expectedSource=one['receipt']['headerSource']))
    register_memory(store,advanced,ACTOR)
    if kind=='grant': store.admissions.revoke(kind,advanced.operation_id)
    with pytest.raises(HandoffError,match='operational_handoff_not_approved'): store.accept(advanced,ACTOR)
    assert len(store.operations)==1 and len(store.admissions.acceptances)==1


def test_expiry_rechecked_before_memory_commit_and_original_source_unchanged():
    encounters,store=memory(); p=prepared(envelope()); register_memory(store,p,ACTOR)
    moments=iter([datetime(2026,9,30,tzinfo=timezone.utc),datetime(2100,1,1,tzinfo=timezone.utc)])
    store.admissions.clock=lambda:next(moments)
    with pytest.raises(HandoffError,match='operational_handoff_not_approved'): store.accept(p,ACTOR)
    assert not encounters.all() and not store.operations and not store.admissions.bindings


def test_different_connection_cannot_take_over_record_and_legacy_not_adopted():
    _,store=memory(); p=prepared(envelope()); register_memory(store,p,ACTOR)
    one=store.accept(p,ACTOR)
    advanced=prepared(envelope(sourceRevision=2,expectedSource=one['receipt']['headerSource']))
    other={**source_for(ACTOR),'connectionId':str(uuid4())}
    store.admissions.register_connection(other)
    store.admissions.register_grant(registration(advanced,ACTOR,other))
    with pytest.raises(HandoffError,match='operational_handoff_not_approved'): store.accept(advanced,ACTOR)
    # Model an untouched pre020 head: legacy receipt recovery is allowed; no adoption.
    store.admissions.bindings.clear()
    assert store.accept(p,ACTOR)['replayed']
    fresh=prepared(envelope(sourceRevision=2,expectedSource=one['receipt']['headerSource']))
    register_memory(store,fresh,ACTOR)
    with pytest.raises(HandoffError,match='operational_handoff_not_approved'): store.accept(fresh,ACTOR)


def test_private_registration_is_closed_and_strict_without_raw_data_in_provenance():
    _,store=memory(); p=prepared(envelope()); register_memory(store,p,ACTOR); store.accept(p,ACTOR)
    proof=store.admissions.acceptances[p.operation_id]
    assert proof['requestSha256']==digest(p.intent)
    assert p.bundle.signed_note not in str(proof) and 'sourceRecordId' not in str(proof)
    for bad in ({**source_for(ACTOR),'dataClass':'phi'}, {**source_for(ACTOR),'sourceMode':'upstream_verified'},
                {**source_for(ACTOR),'approved':True}):
        with pytest.raises((ValueError,HandoffError,BillingHeaderError)): connection(bad)
    with pytest.raises((ValueError,HandoffError,BillingHeaderError)): grant({**registration(p,ACTOR),'sourceRevision':True})
    for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":{"b":1,"b":2}}'):
        with pytest.raises(ValueError): parse_manifest(raw)


def test_malformed_private_registry_record_fails_closed_as_admission_denied():
    from cosmaneura_billing.provider_admission import verify_admission
    p=prepared(envelope()); source=source_for(ACTOR); del source['connectionId']
    with pytest.raises(HandoffError,match='operational_handoff_not_approved'):
        verify_admission(source,registration(p,ACTOR),p,ACTOR,datetime.now(timezone.utc))
