"""SMS preparation is durable review work, not a permit to contact anyone."""
from dataclasses import fields, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cosmaneura_billing.chase.communications import runtime
from cosmaneura_billing.chase.communications.postgres import SmsPreparationEligibility
from cosmaneura_billing.chase.communications.store import CommunicationsError
from cosmaneura_billing.direct_contract import DirectContractList
from test_communications_test_recipients import (
    ACTOR, ALLOWED, BINDING, NOW, OPERATION, OTHER, Rows,
    lane, prohibit_database_io,
)


def install_api(lane, monkeypatch):
    principal = SimpleNamespace(tenant_id=ACTOR, user_id=ACTOR, has_any_role=lambda _: False)
    monkeypatch.setattr(runtime, "_context", lambda *_: (lane.service, principal))
    monkeypatch.setattr(runtime, "_contact", lambda *_: (None, {"sms": {"bindingId": BINDING}}))
    monkeypatch.setattr(lane.service, "case_view", lambda _: {})
    monkeypatch.setattr(lane.store, "prepare", lambda **values: lane.prepared.append(values))
    lane.authorization = None
    return principal


def prepare(lane):
    return runtime.prepare(SimpleNamespace(state=SimpleNamespace()), runtime.PrepareRequest(
        chaseId="case-synthetic", actionId=OPERATION, channel="sms", text=lane.operation["text"]))


def query_override(lane, monkeypatch, fragment, rows):
    original = lane.db.execute
    monkeypatch.setattr(lane.db, "execute", lambda query, params=():
        Rows(rows) if fragment in " ".join(query.split()) else original(query, params))


@pytest.mark.parametrize("paused", ["scheduler_enabled", "patient_messaging_enabled", "operationally_activated", "quiet"])
def test_save_while_paused_defers_only_send_authority(lane, monkeypatch, paused):
    principal = install_api(lane, monkeypatch)
    if paused == "quiet":
        lane.service.policy = replace(lane.policy, quiet_hours=(NOW.hour, NOW.hour + 1))
    else:
        lane.service.policy = replace(lane.policy, **{paused: False})
    # This fixture represents an already approved current policy.
    expected = "communications_quiet_hours" if paused == "quiet" else "communications_dispatch_disabled"
    view = runtime.staff_view(lane.service, principal, "case-synthetic")
    assert view["actions"]["sms"] == {"allowed": True, "reason": "",
        "authorization": {"allowed": False, "reason": expected}}
    prepare(lane)
    assert len(lane.prepared) == 1 and type(lane.prepared[0]["eligibility"]) is SmsPreparationEligibility
    assert not hasattr(lane.prepared[0]["eligibility"], "quiet_hours_clear")
    with pytest.raises(CommunicationsError, match=expected):
        lane.service.authorize(OPERATION, actor_tenant_id=ACTOR, actor_id=ACTOR)
    assert not lane.vendor_requests and not lane.db.starts and not lane.db.receipts


@pytest.mark.parametrize("condition,reason", [
    ("off_list", "test_recipient_not_allowed"), ("empty_scope", "test_recipients_unconfigured"),
    ("consent", "purpose_consent_required"), ("stop", "contact_suppressed"),
    ("policy", "policy_approval_required"), ("staff", "staff_required"),
    ("case", "case_blocked"), ("direct", "direct_contract_blocked"),
    ("roster", "direct_contract_blocked"), ("stale", "contact_stale"),
    ("unknown", "unknown_requires_resolution"), ("touches", "touch_limit"),
    ("future_touch", "touch_evidence_invalid"), ("invalid_touch", "touch_evidence_invalid"),
    ("message", "message_requires_identity_and_opt_out"),
])
def test_dark_drafts_keep_all_other_gates(lane, monkeypatch, condition, reason):
    install_api(lane, monkeypatch)
    lane.service.policy = replace(lane.policy, operationally_activated=False)
    if condition == "off_list": lane.binding["phone"] = OTHER
    elif condition == "empty_scope": lane.service.policy = replace(lane.service.policy, test_recipients=())
    elif condition == "consent": lane.db.consent_granted = False
    elif condition == "stop": lane.db.suppressed = True
    elif condition == "policy": lane.db.approved_policy_hash = "0" * 64
    elif condition == "staff": query_override(lane, monkeypatch, "cosmaneura_staff_assignee_allowed", [{"allowed": False}])
    elif condition in {"case", "direct"}:
        query_override(lane, monkeypatch, "FROM chase_items", [{"state": "submitted" if condition == "case" else "ready",
            "direct_contract": {"is_direct_contract": condition == "direct"}, "patient_ref": "synthetic-one"}])
    elif condition == "roster": lane.service.roster = DirectContractList(path="/nonexistent/synthetic-roster")
    elif condition == "stale":
        query_override(lane, monkeypatch, "SELECT verified_at,observed_at", [{"verified_at": NOW - timedelta(days=3), "observed_at": NOW}])
    elif condition == "unknown": query_override(lane, monkeypatch, "LEFT JOIN chase_communication_unknown_resolutions", [{"present": True}])
    elif condition in {"touches", "future_touch", "invalid_touch"}:
        time = (NOW + timedelta(days=1)).isoformat() if condition == "future_touch" else "malformed" if condition == "invalid_touch" else NOW.isoformat()
        query_override(lane, monkeypatch, "FROM chase_attempts", [{"attempted_at": time}] * (8 if condition == "touches" else 1))
    elif condition == "message": lane.operation["text"] = "No practice identity or opt-out."
    with pytest.raises(HTTPException) as exc:
        prepare(lane)
    assert exc.value.detail == "communications_" + reason
    assert not lane.prepared and not lane.vendor_requests and not lane.db.starts


def test_new_sms_authorization_checks_touch_limit_and_invalid_phase_cannot_skip_gates(lane, monkeypatch):
    lane.authorization = None
    query_override(lane, monkeypatch, "FROM chase_attempts", [{"attempted_at": NOW.isoformat()}] * 8)
    with pytest.raises(CommunicationsError, match="communications_touch_limit"):
        lane.service.authorize(OPERATION, actor_tenant_id=ACTOR, actor_id=ACTOR)
    for phase in (False, None, [], "draft", ""):
        with pytest.raises(CommunicationsError, match="communications_invalid_gate_phase"):
            lane.service._gates(lane.db, lane.operation, phase=phase)
    assert not lane.db.starts and not lane.vendor_requests


@pytest.mark.parametrize("field", [f.name for f in fields(SmsPreparationEligibility)])
def test_sms_preparation_attestation_never_accepts_missing_or_truthy_flags(lane, field):
    allowed = SmsPreparationEligibility(**{f.name: True for f in fields(SmsPreparationEligibility)})
    for invalid in (False, 1, "true", None):
        with pytest.raises(CommunicationsError, match="communications_policy_blocked"):
            lane.store.prepare(case_id="case-synthetic", binding_id=BINDING, operation_id=OPERATION,
                channel="sms", text=lane.operation["text"], actor_tenant_id=ACTOR, actor_id=ACTOR,
                eligibility=replace(allowed, **{field: invalid}))


def test_new_attestation_is_not_a_request_or_voice_or_legacy_store_permission(lane):
    allowed = SmsPreparationEligibility(**{f.name: True for f in fields(SmsPreparationEligibility)})
    for channel, purpose, decision in [("sms", True, vars(allowed)), ("automated_voice", True, allowed), ("sms", False, allowed)]:
        lane.store.purpose_consent_enabled = purpose
        with pytest.raises(CommunicationsError, match="communications_policy_blocked"):
            lane.store.prepare(case_id="case-synthetic", binding_id=BINDING, operation_id=OPERATION,
                channel=channel, text=lane.operation["text"], actor_tenant_id=ACTOR, actor_id=ACTOR,
                eligibility=decision)
