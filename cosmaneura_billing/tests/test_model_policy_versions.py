"""Immutable v1/v2 parsing; registered provider authority is checked separately at runtime."""
import hashlib
import json
from uuid import uuid4
import pytest
from cosmaneura_billing import model_admission as admission
from cosmaneura_billing.model_admission import parse_policy
from test_model_admission import document

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
