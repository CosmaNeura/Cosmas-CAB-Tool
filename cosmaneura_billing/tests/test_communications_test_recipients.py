"""Synthetic recipient boundary through real gates; no database or network I/O.

The fake persistence below supplies existing authorization/contact evidence and
records writes. It does not claim to test PostgreSQL locks or transactions.
"""
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from cosmaneura_billing.chase.communications import runtime, worker
from cosmaneura_billing.chase.communications.config import TwilioServiceConfig
from cosmaneura_billing.chase.communications.dispatch import CommunicationsDispatcher, DispatchPolicy, SenderConfig
from cosmaneura_billing.chase.communications.postgres import PostgresCommunicationsStore
from cosmaneura_billing.chase.communications.store import CommunicationsError, _hash
from cosmaneura_billing.chase.communications.twilio import TwilioAdapter, TwilioConfig, TwilioError, HttpResponse
from cosmaneura_billing.communications_test_policy import validated_test_recipients
from cosmaneura_billing.direct_contract import DirectContractList, check_direct_contract
from cosmaneura_billing.settings import Settings, SettingsError
from test_communications_settings import environment

ALLOWED, OTHER = "+12025550101", "+12025550102"
ACTOR = "11111111-1111-4111-8111-111111111111"
BINDING = "22222222-2222-4222-8222-222222222222"
OPERATION = "33333333-3333-4333-8333-333333333333"
NOW = datetime(2026, 9, 30, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def prohibit_database_io(monkeypatch):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: pytest.fail("database I/O forbidden"))


def test_api_and_worker_share_exact_private_scope_and_default_closed_empty_mode():
    env = environment(COSMANEURA_COMMUNICATIONS_TEST_MODE="true",
                      COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS=f"{OTHER}, {ALLOWED}")
    for config in (Settings.from_env(env), TwilioServiceConfig.from_env(env)):
        assert config.communications_test_recipients == (ALLOWED, OTHER)
        assert ALLOWED not in repr(config) and OTHER not in repr(config)
        policy = runtime.dispatch_policy(config)
        assert policy.test_mode and policy.test_recipients == (ALLOWED, OTHER)
        assert ALLOWED not in repr(policy)
    service = worker.configured_dispatcher(environ=env)
    assert service.policy.test_recipients == (ALLOWED, OTHER)
    assert service.adapter.transport is None
    api = runtime.dispatcher_from_settings(Settings.from_env(env))
    assert api.policy_hash == service.policy_hash
    empty = Settings.from_env(environment(COSMANEURA_COMMUNICATIONS_TEST_MODE="true"))
    assert empty.communications_test_recipients == () and not empty.patient_messaging_enabled
    baseline = Settings.from_env(environment())
    assert not baseline.communications_test_mode and baseline.communications_test_recipients == ()


@pytest.mark.parametrize("changes", [
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": ALLOWED, "COSMANEURA_COMMUNICATIONS_TEST_MODE": "false"},
    {"COSMANEURA_COMMUNICATIONS_TEST_MODE": "maybe"},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": "2025550101"},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": "+1202555*"},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": "+12025550101 ext2"},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": "+０１２０２５５５０１０１"},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": f"{ALLOWED},{ALLOWED}"},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": f"{ALLOWED},"},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": " "},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": None},
    {"COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS": ",".join(f"+1202555{1000+i}" for i in range(21))},
    {"COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS": "sms,automated_voice"},
    {"COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS": "automated_voice"},
])
def test_configuration_is_closed_and_errors_do_not_echo_numbers(changes):
    env = environment(COSMANEURA_COMMUNICATIONS_TEST_MODE="true")
    env.update(changes)
    for config in (Settings, TwilioServiceConfig):
        with pytest.raises(SettingsError) as exc:
            config.from_env(env)
        assert ALLOWED not in str(exc.value) and "2025550101" not in str(exc.value)


@pytest.mark.parametrize("mode,recipients", [(1, (ALLOWED,)), (True, [ALLOWED]), (True, (42,)),
                                            (False, (ALLOWED,)), (True, (ALLOWED, ALLOWED))])
def test_direct_configuration_cannot_bypass_strict_types(mode, recipients):
    with pytest.raises(ValueError):
        validated_test_recipients(mode, recipients, ("sms",))
    with pytest.raises(ValueError):
        DispatchPolicy("Synthetic Practice", "UTC", test_mode=mode, test_recipients=recipients)


class Rows:
    def __init__(self, rows=()): self.rows = list(rows)
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows


class EvidenceDB:
    """Only SQL used by these paths is modeled; any new query fails the test."""
    def __init__(self, lane):
        self.lane = lane
        self.starts, self.receipts, self.blocked = [], [], []
        self.consent_granted, self.suppressed = True, False
        self.approved_policy_hash = lane.service.policy_hash

    def execute(self, query, params=()):
        q = " ".join(query.split())
        lane = self.lane
        if q.startswith("INSERT INTO chase_communication_dispatch_starts"):
            self.starts.append(params); return Rows()
        if q.startswith("INSERT INTO chase_communication_dispatch_receipts"):
            self.receipts.append(params); return Rows()
        if q.startswith("INSERT INTO chase_communication_dispatch_checks"):
            self.blocked.append(params); return Rows()
        if "cosmaneura_staff_assignee_allowed" in q or "cosmaneura_chase_admin_allowed" in q:
            return Rows([{"allowed": True}])
        if q.startswith("SELECT pg_advisory_xact_lock"):
            return Rows()
        if q.startswith("SELECT a.operation_id FROM chase_communication_authorizations"):
            return Rows([] if self.starts else [{"operation_id": OPERATION}])
        if "FROM chase_communication_unknown_resolutions" in q:
            return Rows([{"present": True}] if self.receipts else [])
        if "FROM chase_communication_policy_decisions" in q:
            return Rows([{"decision_id": ACTOR, "approved": True, "policy_hash": self.approved_policy_hash,
                          "actor_tenant_id": ACTOR, "actor_id": ACTOR}])
        if q.startswith("SELECT * FROM chase_communication_operations"):
            return Rows([lane.operation])
        if "FROM chase_communication_cancellations" in q:
            return Rows()
        if "FROM chase_communication_authorizations" in q:
            return Rows([lane.authorization])
        if "FROM chase_items" in q:
            return Rows([{"state": "ready", "direct_contract": {"is_direct_contract": False}, "patient_ref": "synthetic-one"}])
        if "FROM chase_communication_bindings" in q:
            assert params[1:] == (BINDING, "case-synthetic", "sms")
            return Rows([lane.binding])
        if "FROM chase_contact_snapshots" in q:
            return Rows([{"contact_snapshot_id": BINDING, "verified_at": NOW, "observed_at": NOW}])
        if "FROM chase_contact_routes" in q:
            return Rows([{"destination": lane.binding["phone"], "channel": "sms", "consent_status": "unknown"}])
        if "FROM chase_communication_consent_decisions" in q:
            return Rows([{"decision_id": BINDING, "granted": self.consent_granted}])
        if "FROM chase_communication_suppressions" in q:
            return Rows([{"present": True}] if self.suppressed else [])
        if "FROM chase_communication_provider_events" in q or "FROM chase_communication_events" in q:
            return Rows()
        if "FROM chase_attempts" in q:
            return Rows()
        if "FROM chase_communication_dispatch_starts" in q:
            return Rows([{"present": True}] if self.starts else [])
        raise AssertionError(f"Unmodeled SQL: {q}")


@pytest.fixture
def lane(monkeypatch):
    lane = SimpleNamespace(vendor_requests=[], prepared=[])
    lane.sender = SenderConfig("AC" + "0" * 32, "+12025550199", "https://synthetic.invalid/inbound",
        "https://synthetic.invalid/sms-status", "https://synthetic.invalid/voice-status",
        "https://synthetic.invalid/gather", "Synthetic Practice")
    lane.policy = DispatchPolicy("Synthetic Practice", "UTC", test_mode=True, test_recipients=(ALLOWED,),
        scheduler_enabled=True, patient_messaging_enabled=True, operationally_activated=True)
    lane.roster = DirectContractList(refs=[], version="synthetic-test")
    lane.store = PostgresCommunicationsStore("postgresql://unused.invalid/cab", practice_id="synthetic-test",
                                            purpose_consent_enabled=True)
    def vendor(request):
        lane.vendor_requests.append(request)
        body = parse_qs(request.body.decode())
        return HttpResponse(201, json.dumps({"sid": "SM" + "1" * 32, "account_sid": lane.sender.account_sid,
            "from": body["From"][0], "to": body["To"][0], "status": "queued", "direction": "outbound-api"}).encode())
    lane.adapter = TwilioAdapter(TwilioConfig(**asdict(lane.sender), auth_token="synthetic-not-a-credential"), transport=vendor)
    lane.service = CommunicationsDispatcher(lane.store, policy=lane.policy, sender=lane.sender,
        roster=lane.roster, adapter=lane.adapter, clock=lambda: NOW)
    lane.binding = {"binding_id": BINDING, "phone": ALLOWED, "channel": "sms", "contact_snapshot_id": BINDING,
        "account_sid": lane.sender.account_sid, "sender": lane.sender.from_number}
    lane.operation = {"operation_id": OPERATION, "case_id": "case-synthetic", "binding_id": BINDING,
        "channel": "sms", "text": lane.policy.templates[0]["text"], "actor_tenant_id": ACTOR, "actor_id": ACTOR}
    lane.authorization = {"actor_tenant_id": ACTOR, "actor_id": ACTOR, "policy_decision_id": ACTOR,
        "consent_decision_id": BINDING, "created_at": NOW,
        "direct_contract_hash": check_direct_contract("synthetic-one", roster=lane.roster).gate_fingerprint}
    lane.db = EvidenceDB(lane)
    monkeypatch.setattr(lane.store, "_db", lambda: nullcontext(lane.db))
    monkeypatch.setattr(lane.store, "_execution", lambda operation: nullcontext(lane.db))
    return lane


def test_allowlisted_sms_passes_existing_gates_and_is_submitted_once(lane):
    assert lane.service.run_once() == [{"operationId": OPERATION, "status": "accepted"}]
    assert len(lane.db.starts) == len(lane.db.receipts) == len(lane.vendor_requests) == 1
    assert parse_qs(lane.vendor_requests[0].body.decode())["To"] == [ALLOWED]
    assert lane.service.run_once() == []
    assert len(lane.vendor_requests) == 1


@pytest.mark.parametrize("recipients,phone,reason", [
    ((), ALLOWED, "communications_test_recipients_unconfigured"),
    ((ALLOWED,), OTHER, "communications_test_recipient_not_allowed"),
])
def test_worker_blocks_forged_or_prior_preparation_before_start_or_build(lane, monkeypatch, recipients, phone, reason):
    lane.service.policy = replace(lane.policy, test_recipients=recipients)
    lane.binding["phone"] = phone
    monkeypatch.setattr(lane.adapter, "build_sms", lambda *args: pytest.fail("blocked recipient built request"))
    assert lane.service.run_once() == [{"operationId": OPERATION, "status": "blocked", "reason": reason}]
    assert not lane.vendor_requests and not lane.db.starts and not lane.db.receipts
    assert lane.db.blocked[0][-1] == reason


def test_authorization_rechecks_off_list_contact(lane):
    lane.binding["phone"] = OTHER
    lane.authorization = None
    with pytest.raises(CommunicationsError, match="communications_test_recipient_not_allowed"):
        lane.service.authorize(OPERATION, actor_tenant_id=ACTOR, actor_id=ACTOR)
    assert not lane.db.starts and not lane.vendor_requests


def test_staff_readiness_exposes_safe_reason_without_disclosing_scope(lane, monkeypatch):
    lane.binding["phone"] = OTHER
    monkeypatch.setattr(lane.service, "case_view", lambda case: {})
    monkeypatch.setattr(runtime, "_contact", lambda *args: (None, {"sms": {"bindingId": BINDING}}))
    principal = SimpleNamespace(tenant_id=ACTOR, user_id=ACTOR, has_any_role=lambda roles: False)
    view = runtime.staff_view(lane.service, principal, "case-synthetic")
    assert view["actions"]["sms"] == {"allowed": False, "reason": "communications_test_recipient_not_allowed",
        "authorization": {"allowed": False, "reason": "communications_test_recipient_not_allowed"}}
    assert not view["actions"]["automatedVoice"]["allowed"]
    assert ALLOWED not in json.dumps(view) and "test_recipients" not in json.dumps(view)


def test_changed_test_scope_invalidates_existing_policy_approval_before_send(lane):
    lane.service = CommunicationsDispatcher(lane.store, policy=replace(lane.policy, test_recipients=(ALLOWED, OTHER)),
        sender=lane.sender, roster=lane.roster, adapter=lane.adapter, clock=lambda: NOW)
    assert lane.service.run_once()[0]["reason"] == "communications_policy_approval_required"
    assert not lane.db.starts and not lane.vendor_requests


def test_allowlist_does_not_activate_dark_dispatch(lane):
    lane.service.policy = replace(lane.policy, operationally_activated=False)
    assert lane.service.run_once()[0]["reason"] == "communications_dispatch_disabled"
    assert not lane.db.starts and not lane.vendor_requests


@pytest.mark.parametrize("consent,suppressed,reason", [(False, False, "communications_purpose_consent_required"),
                                                       (True, True, "communications_contact_suppressed")])
def test_allowlist_never_overrides_consent_or_stop(lane, consent, suppressed, reason):
    lane.db.consent_granted, lane.db.suppressed = consent, suppressed
    assert lane.service.run_once()[0]["reason"] == reason
    assert not lane.db.starts and not lane.vendor_requests


def test_runtime_prepare_checks_server_contact_and_rejects_client_recipient_override(lane, monkeypatch):
    principal = SimpleNamespace(tenant_id=ACTOR, user_id=ACTOR)
    monkeypatch.setattr(runtime, "_context", lambda *args: (lane.service, principal))
    monkeypatch.setattr(runtime, "_contact", lambda *args: (None, {"sms": {"bindingId": BINDING}}))
    monkeypatch.setattr(runtime, "staff_view", lambda *args: {"mode": "durable"})
    monkeypatch.setattr(lane.store, "prepare", lambda **kwargs: lane.prepared.append(kwargs))
    payload = dict(chaseId="case-synthetic", actionId=OPERATION, channel="sms", text=lane.operation["text"])
    for override in ({"to": ALLOWED}, {"phone": ALLOWED}, {"test_mode": False}):
        with pytest.raises(ValidationError): runtime.PrepareRequest(**payload, **override)
    lane.binding["phone"] = OTHER
    request = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        runtime.prepare(request, runtime.PrepareRequest(**payload))
    assert exc.value.status_code == 403 and exc.value.detail == "communications_test_recipient_not_allowed"
    assert not lane.prepared and not lane.db.starts
    lane.binding["phone"] = ALLOWED
    assert runtime.prepare(request, runtime.PrepareRequest(**payload)) == {"mode": "durable"}
    assert len(lane.prepared) == 1 and lane.prepared[0]["binding_id"] == BINDING
    assert not lane.vendor_requests


def test_final_transport_boundary_rejects_off_list_and_body_destination_forgery(lane):
    with pytest.raises(CommunicationsError, match="communications_test_recipient_not_allowed"):
        lane.service._submit_started(OPERATION, lane.adapter.build_sms(OTHER, lane.operation["text"]))
    allowed = lane.adapter.build_sms(ALLOWED, lane.operation["text"])
    pairs = parse_qs(allowed.body.decode())
    pairs["To"] = [OTHER]
    with pytest.raises(TwilioError):
        lane.service._submit_started(OPERATION, replace(allowed, body=urlencode(pairs, doseq=True).encode()))
    assert not lane.vendor_requests and not lane.db.receipts


def test_test_mode_blocks_voice_but_inactive_existing_voice_path_is_preserved(lane):
    with pytest.raises(ValueError): replace(lane.policy, enabled_channels=("sms", "automated_voice"))
    voice_policy = replace(lane.policy, test_mode=False, test_recipients=(), enabled_channels=("sms", "automated_voice"))
    lane.store.enabled_channels = voice_policy.enabled_channels
    adapter = TwilioAdapter(replace(lane.adapter.config, enabled_channels=voice_policy.enabled_channels), transport=lane.adapter.transport)
    service = CommunicationsDispatcher(lane.store, policy=voice_policy, sender=lane.sender, roster=lane.roster, adapter=adapter)
    voice = adapter.build_call(ALLOWED)
    with pytest.raises(CommunicationsError, match="communications_channel_disabled"):
        lane.service._submit_started(OPERATION, voice)
    # A deliberately rejected fake voice response is sufficient to prove the
    # original non-test channel reaches transport; no live call is made.
    assert service._submit_started(OPERATION, voice).outcome == "unknown"
    assert len(lane.vendor_requests) == 1


def test_policy_hash_preserves_inactive_approvals_and_binds_active_recipient_scope(lane):
    baseline = replace(lane.policy, test_mode=False, test_recipients=())
    def service(policy):
        return CommunicationsDispatcher(lane.store, policy=policy, sender=lane.sender, roster=lane.roster)
    previous_values = asdict(baseline)
    for key in ("scheduler_enabled", "patient_messaging_enabled", "operationally_activated", "test_mode", "test_recipients"):
        del previous_values[key]
    original = _hash([previous_values, asdict(lane.sender), lane.service.call_script])
    assert service(baseline).policy_hash == original
    hashes = {service(policy).policy_hash for policy in (baseline, lane.policy,
        replace(lane.policy, test_recipients=()), replace(lane.policy, test_recipients=(OTHER,)))}
    assert len(hashes) == 4
    assert service(replace(lane.policy, test_recipients=(OTHER, ALLOWED))).policy_hash == service(
        replace(lane.policy, test_recipients=(ALLOWED, OTHER))).policy_hash
