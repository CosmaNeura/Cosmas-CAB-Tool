"""Synthetic PostgreSQL + fake-vendor end-to-end tests; no external requests."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from test_chase_communications_postgres import (
    DSN, PHONE, SENDER, new_lane, seed_case, bind, prepare, fails,
    schema as foundation_schema, psycopg,
)
from cosmaneura_billing.chase.communications.postgres import PostgresCommunicationsStore
from cosmaneura_billing.chase.communications.dispatch import CommunicationsDispatcher, DispatchPolicy, SenderConfig
from cosmaneura_billing.chase.communications.twilio import TwilioAdapter, TwilioConfig, HttpResponse
from cosmaneura_billing.direct_contract import DirectContractList

pytestmark = pytest.mark.skipif(not DSN, reason="disposable PostgreSQL is not configured")
TABLES = ["chase_communication_" + name for name in ("policy_decisions", "consent_decisions",
          "authorizations", "dispatch_starts", "dispatch_receipts", "provider_events", "dispatch_checks", "cancellations", "unknown_resolutions", "event_reviews")]


@pytest.fixture(scope="module", autouse=True)
def schema(foundation_schema):
    if not DSN:
        return
    with psycopg.connect(DSN) as db:
        # The foundation fixture installs the complete canonical SMS dependency set.
        for table in TABLES + ["chase_communication_review_sources",
                               "chase_communication_unassigned_sources"]:
            assert db.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0], table


class FakeVendor:
    def __init__(self):
        self.requests = []
        self.error = None
        self.before_receipt = None

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        body = parse_qs(request.body.decode())
        sid = ("SM" if request.channel == "sms" else "CA") + uuid4().hex
        if self.before_receipt:
            self.before_receipt(sid)
        return HttpResponse(201, json.dumps({"sid": sid, "account_sid": self.account,
            "from": body["From"][0], "to": body["To"][0], "status": "queued", "direction": "outbound-api"}).encode())


@pytest.fixture
def lane():
    lane = new_lane()
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET roles=ARRAY['Admin','BillingStaff'] WHERE practice_id=%s", (lane.practice,))
        db.execute(sql.SQL("GRANT SELECT,INSERT ON {} TO {}").format(sql.SQL(",").join(map(sql.Identifier, TABLES)), sql.Identifier(lane.role)))
        for suffix in ("policy_decisions", "consent_decisions", "provider_events", "dispatch_checks"):
            db.execute(sql.SQL("GRANT USAGE,SELECT ON SEQUENCE {} TO {}").format(
                sql.Identifier("chase_communication_" + suffix + "_sequence_no_seq"), sql.Identifier(lane.role)))
        db.execute(sql.SQL("GRANT SELECT ON chase_attempts,chase_communication_review_sources,chase_communication_unassigned_sources TO {}").format(sql.Identifier(lane.role)))
        db.execute(sql.SQL("GRANT UPDATE(state) ON chase_items TO {}").format(sql.Identifier(lane.role)))
        db.execute(sql.SQL("GRANT EXECUTE ON FUNCTION cosmaneura_chase_admin_allowed(uuid,uuid) TO {}").format(sql.Identifier(lane.role)))
    lane.snapshot = seed_case(lane, observed=datetime.now(timezone.utc).isoformat())
    lane.sender = SenderConfig(lane.account, SENDER, "https://synthetic.invalid/sms", "https://synthetic.invalid/sms-status",
                              "https://synthetic.invalid/voice-status", "https://synthetic.invalid/gather", "Synthetic Practice")
    hour = datetime.now(timezone.utc).hour
    lane.policy = DispatchPolicy("Synthetic Practice", "UTC", quiet_hours=((hour + 1) % 24, (hour + 2) % 24),
                                scheduler_enabled=True, patient_messaging_enabled=True, operationally_activated=True,
                                enabled_channels=("sms", "automated_voice"))
    lane.vendor = FakeVendor()
    lane.vendor.account = lane.account
    lane.adapter = TwilioAdapter(TwilioConfig(**asdict(lane.sender), auth_token="synthetic-test-secret", enabled_channels=lane.policy.enabled_channels), transport=lane.vendor)
    lane.store = PostgresCommunicationsStore(lane.dsn, practice_id=lane.practice, purpose_consent_enabled=True,
                                            enabled_channels=lane.policy.enabled_channels)
    lane.service = service(lane)
    lane.service.approve_policy(decision_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor)
    return lane


def service(lane, **changes):
    args = dict(policy=lane.policy, sender=lane.sender, roster=DirectContractList(refs=[], version="test"), adapter=lane.adapter)
    args.update(changes)
    return CommunicationsDispatcher(lane.store, **args)


def sms_only_service(lane):
    store = PostgresCommunicationsStore(lane.dsn, practice_id=lane.practice,
        purpose_consent_enabled=True, enabled_channels=("sms",))
    return CommunicationsDispatcher(store, policy=replace(lane.policy, enabled_channels=("sms",)),
        sender=lane.sender, roster=DirectContractList(refs=[], version="test"),
        adapter=TwilioAdapter(replace(lane.adapter.config, enabled_channels=("sms",)), transport=lane.vendor))


def test_sms_only_worker_blocks_old_authorized_call_and_allows_cancellation(lane):
    operation = ready(lane, "automated_voice")["operationId"]
    current = sms_only_service(lane)
    fails("communications_channel_disabled", lambda: current.authorize(operation,
        actor_tenant_id=lane.tenant, actor_id=lane.actor))
    assert current.run_once() == [{"operationId": operation, "status": "blocked", "reason": "communications_channel_disabled"}]
    view = current.case_view(lane.case)
    assert view["enabledChannels"] == ["sms"] and view["operations"][0]["cancelable"]
    assert view["operations"][0]["blockedReason"] == "communications_channel_disabled"
    current.cancel(operation, actor_tenant_id=lane.tenant, actor_id=lane.actor)
    assert current.case_view(lane.case)["operations"][0]["status"] == "canceled"
    assert lane.vendor.requests == []


def test_sms_only_receiver_retains_historical_call_status_and_keypad_review(lane):
    ready(lane, "automated_voice")
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    current = sms_only_service(lane)
    current.receive_webhook("voice_status", *webhook(lane, "voice_status", sid))
    current.receive_webhook("voice_gather", *webhook(lane, "voice_gather", sid, Digits="1", CallStatus="in-progress"))
    view = current.case_view(lane.case)
    assert view["operations"][0]["status"] == "completed"
    assert any(event["disposition"] == "callback_requested" for event in view["events"])
    assert len(lane.vendor.requests) == 1


def test_messaging_service_change_requires_new_policy_and_operation_authorization(lane):
    ready(lane)
    sender = replace(lane.sender, messaging_service_sid="MG" + "1" * 32)
    adapter = TwilioAdapter(TwilioConfig(**asdict(sender), auth_token="synthetic-test-secret",
        enabled_channels=lane.policy.enabled_channels), transport=lane.vendor)
    current = service(lane, sender=sender, adapter=adapter)
    assert current.policy_hash != lane.service.policy_hash
    assert current.run_once()[0]["reason"] == "communications_policy_approval_required"
    current.approve_policy(decision_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor)
    assert current.run_once()[0]["reason"] == "communications_approval_stale"
    assert lane.vendor.requests == []


def ready(lane, channel="sms"):
    binding = bind(lane, channel)
    lane.service.record_consent(decision_id=str(uuid4()), binding_id=binding, granted=True,
        source_reference="synthetic-consent:1", actor_tenant_id=lane.tenant, actor_id=lane.actor)
    op = prepare(lane, binding, channel=channel, text=lane.policy.templates[0]["text"] if channel == "sms" else lane.service.call_script)
    lane.service.authorize(op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    return op


def webhook(lane, kind, sid=None, **changes):
    sms, inbound = kind.startswith("sms"), kind == "sms_inbound"
    params = {"AccountSid": lane.account, "MessageSid" if sms else "CallSid": sid or ("SM" if sms else "CA") + uuid4().hex,
        "From": PHONE if inbound else SENDER, "To": SENDER if inbound else PHONE,
        "Direction": "inbound" if inbound else "outbound-api"}
    params.update({"Body": "Synthetic reply"} if inbound else {"MessageStatus" if sms else "CallStatus": "delivered" if sms else "completed"})
    params.update(changes)
    urls = {"sms_inbound": lane.sender.sms_inbound_url, "sms_status": lane.sender.sms_status_url,
            "voice_status": lane.sender.voice_status_url, "voice_gather": lane.sender.voice_action_url}
    raw = urlencode(params).encode()
    digest = hmac.new(b"synthetic-test-secret", (urls[kind] + "".join(k + params[k] for k in sorted(params))).encode(), hashlib.sha1).digest()
    return raw, base64.b64encode(digest).decode()


def receive(lane, kind, sid=None, **changes):
    return lane.service.receive_webhook(kind, *webhook(lane, kind, sid, **changes))


def test_public_staff_service_has_no_token_and_default_off_dispatch(lane):
    public = service(lane, adapter=None)
    op = ready(lane)
    assert "auth_token" not in asdict(public.sender)
    assert public.case_view(lane.case)["operations"][0]["status"] == "authorized"
    fails("communications_transport_disabled", lambda: public.run_once())
    disabled = service(lane, policy=replace(lane.policy, operationally_activated=False))
    assert disabled.run_once()[0]["reason"] == "communications_dispatch_disabled"
    assert lane.vendor.requests == [] and op["status"] == "prepared"


def test_sms_send_receipt_reply_delivery_survive_restart_and_do_not_close(lane):
    ready(lane)
    assert lane.service.run_once()[0]["status"] == "accepted"
    view = lane.service.case_view(lane.case)
    sid = view["operations"][0]["providerSid"]
    assert view["operations"][0]["status"] == "queued"
    receive(lane, "sms_status", sid)
    inbound = webhook(lane, "sms_inbound")
    lane.service.receive_webhook("sms_inbound", *inbound)
    lane.service.receive_webhook("sms_inbound", *inbound)
    fresh = service(lane).case_view(lane.case)
    assert fresh["operations"][0]["status"] == "delivered"
    assert sum(e["kind"] == "sms_inbound" for e in fresh["events"]) == 1
    assert service(lane).run_once() == [] and len(lane.vendor.requests) == 1
    assert not any(PHONE in json.dumps(e) for e in fresh["events"])
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT state FROM chase_items WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == "needs_review"
        assert db.execute("SELECT count(*) FROM chase_attempts WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


def test_concurrent_workers_claim_exactly_once(lane):
    ready(lane)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: service(lane).run_once(), range(6)))
    assert len(lane.vendor.requests) == 1


@pytest.mark.parametrize("error", [TimeoutError("secret/provider body must not escape"), KeyboardInterrupt()])
def test_uncertainty_or_crash_never_retries(lane, error):
    ready(lane)
    lane.vendor.error = error
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            lane.service.run_once()
    else:
        assert lane.service.run_once()[0]["status"] == "unknown"
    assert service(lane).run_once() == []
    assert lane.service.case_view(lane.case)["operations"][0]["status"] == "unknown"
    assert len(lane.vendor.requests) == 1


def test_early_status_is_durable_and_binds_only_after_receipt(lane):
    ready(lane)
    def early(sid):
        receive(lane, "sms_status", sid)
        assert not any(e["kind"] == "sms_status" for e in lane.service.case_view(lane.case)["events"])
    lane.vendor.before_receipt = early
    lane.service.run_once()
    assert lane.service.case_view(lane.case)["operations"][0]["status"] == "delivered"


@pytest.mark.parametrize("body,opt_out", [("STOP", None), ("No más mensajes", "STOP")])
def test_stop_suppresses_and_start_does_not_reactivate(lane, body, opt_out):
    ready(lane)
    receive(lane, "sms_inbound", Body=body, **({"OptOutType": opt_out} if opt_out else {}))
    receive(lane, "sms_inbound", Body="START", OptOutType="START")
    assert lane.service.run_once()[0]["reason"] == "communications_contact_suppressed"
    assert lane.service.case_view(lane.case)["suppression"]["sms"] is True
    assert lane.vendor.requests == []


@pytest.mark.parametrize("digit,disposition", [("1", "callback_requested"), ("2", "reports_submitted"), ("9", "stop_calls")])
def test_scripted_call_keypad_is_separate_from_sms_and_manual_phone(lane, digit, disposition):
    ready(lane, "automated_voice")
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    body = parse_qs(lane.vendor.requests[0].body.decode())
    assert "Synthetic Practice" in body["Twiml"][0] and body["Record"] == ["false"]
    result = receive(lane, "voice_gather", sid, Digits=digit, CallStatus="in-progress")
    assert "Hangup" in result["twiml"]
    view = service(lane).case_view(lane.case)
    assert any(e["disposition"] == disposition for e in view["events"])
    assert view["suppression"]["automatedVoice"] is (digit == "9")
    assert len(lane.vendor.requests) == 1
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_attempts WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


@pytest.mark.parametrize("change,reason", [
    ("closed", "case_blocked"), ("submitted", "case_blocked"), ("contact", "contact_stale"),
    ("consent", "purpose_consent_required"), ("policy", "policy_approval_required"),
    ("membership", "staff_required"), ("direct", "direct_contract_blocked"), ("roster_unknown", "direct_contract_blocked"),
    ("quiet", "quiet_hours"), ("activation", "dispatch_disabled"), ("scheduler", "dispatch_disabled"), ("messaging", "dispatch_disabled"),
])
def test_dispatch_rechecks_each_gate(lane, change, reason):
    op = ready(lane)
    if change in {"closed", "submitted", "membership", "direct"}:
        with psycopg.connect(DSN) as db:
            if change == "membership":
                db.execute("UPDATE practice_memberships SET active=false WHERE practice_id=%s", (lane.practice,))
            elif change == "direct":
                db.execute("UPDATE chase_items SET direct_contract=%s WHERE practice_id=%s", (Jsonb({"is_direct_contract": True}), lane.practice))
            else:
                db.execute("UPDATE chase_items SET state=%s WHERE practice_id=%s", ("verified_closed" if change == "closed" else "submitted", lane.practice))
    elif change == "contact":
        seed_case(lane, observed=datetime.now(timezone.utc).isoformat())
    elif change == "consent":
        lane.service.record_consent(decision_id=str(uuid4()), binding_id=op["bindingId"], granted=False,
            source_reference="synthetic:revoked", actor_tenant_id=lane.tenant, actor_id=lane.actor)
    elif change == "policy":
        lane.service.approve_policy(decision_id=str(uuid4()), approved=False, actor_tenant_id=lane.tenant, actor_id=lane.actor)
    elif change == "roster_unknown":
        lane.service = service(lane, roster=DirectContractList(path="/nonexistent/synthetic-list"))
    elif change == "quiet":
        # Authorization happened while contact was allowed; the worker must
        # recheck when the next quiet interval begins.
        lane.service = service(lane, clock=lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    else:
        key = {"activation": "operationally_activated", "scheduler": "scheduler_enabled", "messaging": "patient_messaging_enabled"}[change]
        lane.service = service(lane, policy=replace(lane.policy, **{key: False}))
    results = lane.service.run_once()
    assert any(row["reason"] == "communications_" + reason for row in results)
    assert lane.vendor.requests == []


def test_unknown_generic_consent_requires_explicit_purpose_confirmation(lane):
    lane.snapshot = seed_case(lane, observed=datetime.now(timezone.utc).isoformat(), consent="unknown")
    binding = bind(lane)
    fails("communications_consent_unavailable", lambda: prepare(lane, binding))
    ready(lane)
    assert lane.service.run_once()[0]["status"] == "accepted"


def test_denied_generic_consent_cannot_be_overridden(lane):
    lane.snapshot = seed_case(lane, observed=datetime.now(timezone.utc).isoformat(), consent="denied")
    binding = bind(lane)
    fails("communications_consent_unavailable", lambda: lane.service.record_consent(decision_id=str(uuid4()),
        binding_id=binding, granted=True, source_reference="synthetic:denied", actor_tenant_id=lane.tenant, actor_id=lane.actor))


def test_purpose_consent_is_required_for_automated_voice_even_with_phone_consent(lane):
    binding = bind(lane, "automated_voice")
    op = prepare(lane, binding, channel="automated_voice", text=lane.service.call_script)
    fails("communications_purpose_consent_required", lambda: lane.service.authorize(op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor))


def test_out_of_order_delivery_status_does_not_regress(lane):
    ready(lane)
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    receive(lane, "sms_status", sid, MessageStatus="delivered")
    receive(lane, "sms_status", sid, MessageStatus="sent")
    receive(lane, "sms_status", sid, MessageStatus="queued")
    assert lane.service.case_view(lane.case)["operations"][0]["status"] == "delivered"


def test_invalid_signature_unknown_sid_wrong_route_and_sequence_conflicts(lane):
    ready(lane, "automated_voice")
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    raw, _ = webhook(lane, "voice_status", sid)
    fails("communications_invalid_callback", lambda: lane.service.receive_webhook("voice_status", raw, "bad"))
    receive(lane, "voice_status", sid, To="+12025550111", CallStatus="completed")
    receive(lane, "voice_status", "CA" + uuid4().hex, CallStatus="completed")
    assert not any(e["kind"] == "voice_status" for e in lane.service.case_view(lane.case)["events"])
    receive(lane, "voice_status", sid, SequenceNumber="1", CallStatus="ringing")
    fails("communications_event_conflict", lambda: receive(lane, "voice_status", sid, SequenceNumber="1", CallStatus="completed"))


def test_mms_body_can_be_empty_without_fetching_media(lane):
    ready(lane)
    receive(lane, "sms_inbound", Body="", NumMedia="1", MediaUrl0="https://should-never-be-fetched.invalid/private")
    event = next(e for e in lane.service.case_view(lane.case)["events"] if e["kind"] == "sms_inbound")
    assert event["mediaCount"] == 1 and event["text"] == ""
    assert "MediaUrl" not in json.dumps(event)


def test_per_case_touch_cap_counts_dispatch_starts_including_uncertain(lane):
    first = ready(lane)
    # Queue approvals before any starts. Their shared consent remains unchanged;
    # the worker must enforce the cap even for previously authorized operations.
    for _ in range(2):
        op = prepare(lane, first["bindingId"], text=lane.policy.templates[0]["text"])
        lane.service.authorize(op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    pending = prepare(lane, first["bindingId"], text=lane.policy.templates[0]["text"])
    results = lane.service.run_once()
    assert sum(row["status"] == "accepted" for row in results) == 2
    assert any(row.get("reason") == "communications_touch_limit" for row in results)
    assert len(lane.vendor.requests) == 2
    fails("communications_touch_limit", lambda: lane.service.authorize(
        pending["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor))
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_authorizations WHERE practice_id=%s AND operation_id=%s",
            (lane.practice, pending["operationId"])).fetchone()[0] == 0


def test_atomic_audit_failure_rolls_back_authorization(lane):
    binding = bind(lane)
    lane.service.record_consent(decision_id=str(uuid4()), binding_id=binding, granted=True,
        source_reference="synthetic:audit", actor_tenant_id=lane.tenant, actor_id=lane.actor)
    op = prepare(lane, binding, text=lane.policy.templates[0]["text"])
    def broken(*_):
        raise RuntimeError("synthetic audit unavailable")
    with pytest.raises(RuntimeError):
        service(lane, transaction_audit=broken).authorize(op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT count(*) FROM chase_communication_authorizations WHERE practice_id=%s", (lane.practice,)).fetchone()[0] == 0


def test_tenant_rls_and_immutable_evidence(lane):
    ready(lane)
    other = new_lane()
    forged = PostgresCommunicationsStore(lane.dsn, practice_id=other.practice)
    fails("communications_case_not_found", lambda: forged.case_view(other.case))
    with psycopg.connect(DSN) as db:
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("DELETE FROM chase_communication_authorizations WHERE practice_id=%s", (lane.practice,))


def test_named_admin_policy_is_enforced(lane):
    with psycopg.connect(DSN) as db:
        db.execute("UPDATE practice_memberships SET roles=ARRAY['BillingStaff'] WHERE practice_id=%s", (lane.practice,))
    fails("communications_admin_required", lambda: lane.service.approve_policy(decision_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor))
    assert lane.vendor.requests == []


def test_blocked_first_page_does_not_starve_later_authorizations(lane):
    ready(lane)
    ready(lane)  # New purpose-consent evidence makes the first approval stale.
    first = lane.service.run_once(limit=1)
    assert first[0]["reason"] == "communications_approval_stale"
    second = lane.service.run_once(limit=1)
    assert second[0]["status"] == "accepted" and len(lane.vendor.requests) == 1


def test_delivery_status_survives_history_truncation(lane):
    ready(lane)
    lane.service.run_once()
    sid = lane.service.case_view(lane.case)["operations"][0]["providerSid"]
    receive(lane, "sms_status", sid)
    with psycopg.connect(DSN) as db:
        for _ in range(205):
            db.execute("""INSERT INTO chase_communication_provider_events
                (practice_id,account_sid,event_id,provider_sid,kind,channel,sender,recipient,
                 case_id,text,status,disposition,body_hash) VALUES (%s,%s,%s,%s,
                 'sms_inbound','sms',%s,%s,%s,'Synthetic reply','received','reply_received',%s)""",
                (lane.practice, lane.account, uuid4().hex * 2, "SM" + uuid4().hex, PHONE, SENDER,
                 lane.case, uuid4().hex * 2))
    view = lane.service.case_view(lane.case)
    assert view["truncated"] is True
    assert view["operations"][0]["status"] == "delivered"


def test_actual_worker_login_dispatches_and_cannot_authorize(lane):
    from types import SimpleNamespace
    from psycopg.conninfo import make_conninfo
    from cosmaneura_billing.migrations import _provision_chase_worker_role
    worker = "comms_worker_" + uuid4().hex[:16]
    settings = SimpleNamespace(practice_id=lane.practice, chase_worker_database_role=worker,
                               chase_worker_database_password="synthetic-local-only")
    with psycopg.connect(DSN) as db:
        _provision_chase_worker_role(db, settings)
        # Test-owned role only; the deployed worker provisioner stays dormant.
        relations = TABLES + ["chase_communication_" + n for n in ("senders", "bindings", "operations", "events", "suppressions")]
        relations += ["chase_attempts", "chase_communication_review_sources"]
        db.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.SQL(",").join(map(sql.Identifier, relations)), sql.Identifier(worker)))
        for statement in (
            "GRANT INSERT ON chase_communication_dispatch_starts, chase_communication_dispatch_receipts, chase_communication_provider_events, chase_communication_dispatch_checks",
            "GRANT USAGE,SELECT ON SEQUENCE chase_communication_provider_events_sequence_no_seq, chase_communication_dispatch_checks_sequence_no_seq",
            "GRANT EXECUTE ON FUNCTION cosmaneura_staff_assignee_allowed(uuid,uuid), cosmaneura_chase_admin_allowed(uuid,uuid)",
        ):
            db.execute(sql.SQL(statement + " TO {}").format(sql.Identifier(worker)))
    ready(lane)
    worker_store = PostgresCommunicationsStore(make_conninfo(DSN, user=worker, password=settings.chase_worker_database_password),
        practice_id=lane.practice, purpose_consent_enabled=True)
    worker_service = CommunicationsDispatcher(worker_store, policy=lane.policy,
        sender=lane.sender, roster=DirectContractList(refs=[], version="test"), adapter=lane.adapter)
    assert worker_service.run_once()[0]["status"] == "accepted"
    raw, signature = webhook(lane, "sms_inbound", Body="STOP")
    worker_service.receive_webhook("sms_inbound", raw, signature)
    assert lane.service.case_view(lane.case)["suppression"]["sms"] is True
    fails("communications_store_unavailable", lambda: worker_service.approve_policy(
        decision_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor))
    with psycopg.connect(make_conninfo(DSN, user=worker, password=settings.chase_worker_database_password)) as db:
        assert db.execute("SELECT has_table_privilege(current_user,'encounters','SELECT')").fetchone()[0] is False
        assert db.execute("SELECT has_table_privilege(current_user,'chase_communication_authorizations','INSERT')").fetchone()[0] is False


@pytest.mark.parametrize("hours,reason", [(25, "contact_stale"), (-1, "contact_stale")])
def test_clock_contact_age_and_future_evidence_fail_closed(lane, hours, reason):
    ready(lane)
    checked_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    lane.service = service(lane, clock=lambda: checked_at)
    assert lane.service.run_once()[0]["reason"] == "communications_" + reason
    assert lane.vendor.requests == []


def test_policy_change_and_new_roster_require_new_exact_approval(lane):
    ready(lane)
    lane.service = service(lane, roster=DirectContractList(refs=[], version="changed"))
    assert lane.service.run_once()[0]["reason"] == "communications_approval_stale"
    lane.policy = replace(lane.policy, max_total_touches=7)
    lane.service = service(lane)
    assert lane.service.run_once()[0]["reason"] == "communications_policy_approval_required"


def test_nonempty_migration_reversal_is_refused(lane):
    ready(lane)
    with psycopg.connect(DSN, autocommit=True) as db:
        with pytest.raises(psycopg.errors.RaiseException):
            db.execute((Path(__file__).resolve().parents[1] / "migrations/012_chase_communications_dispatch.down.sql").read_text(), prepare=False)
        db.execute("ROLLBACK")
        assert db.execute("SELECT to_regclass('chase_communication_authorizations')").fetchone()[0]


def test_pending_hold_is_visible_then_clears_without_erasing_gate_history(lane):
    ready(lane)
    lane.service = service(lane, clock=lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    lane.service.run_once()
    held = lane.service.case_view(lane.case)["operations"][0]
    assert held["status"] == "blocked" and held["blockedReason"] == "communications_quiet_hours"
    assert held["lastCheckedAt"] and held["needsStaffReview"] is True
    after_quiet = datetime.now(timezone.utc) + timedelta(hours=2)
    lane.service = service(lane, clock=lambda: after_quiet)
    assert lane.service.run_once()[0]["status"] == "accepted"
    accepted = lane.service.case_view(lane.case)["operations"][0]
    assert accepted["status"] == "queued"
    assert accepted["blockedReason"] is None and accepted["lastCheckedAt"] is None
    with psycopg.connect(DSN) as db:
        assert db.execute("SELECT reason FROM chase_communication_dispatch_checks WHERE practice_id=%s", (lane.practice,)).fetchall() == [("communications_quiet_hours",)]


@pytest.mark.parametrize("channel", ["sms", "automated_voice"])
def test_recipient_opt_out_survives_sender_and_vendor_account_rotation(lane, channel):
    ready(lane, channel)
    old_service = lane.service
    kind = "sms_inbound" if channel == "sms" else "voice_gather"
    callback = webhook(lane, kind, **({"Body": "STOP"} if channel == "sms" else {"Digits": "9", "CallStatus": "in-progress"}))
    lane.account = "AC" + uuid4().hex
    lane.sender = replace(lane.sender, account_sid=lane.account, from_number="+12025550198")
    lane.vendor.account = lane.account
    lane.adapter = TwilioAdapter(TwilioConfig(**asdict(lane.sender), auth_token="synthetic-test-secret", enabled_channels=lane.policy.enabled_channels), transport=lane.vendor)
    lane.service = service(lane)
    lane.service.approve_policy(decision_id=str(uuid4()), actor_tenant_id=lane.tenant, actor_id=lane.actor)
    binding = bind(lane, channel, sender=lane.sender.from_number)
    lane.service.record_consent(decision_id=str(uuid4()), binding_id=binding, granted=True,
        source_reference="synthetic:rotated", actor_tenant_id=lane.tenant, actor_id=lane.actor)
    text = lane.policy.templates[0]["text"] if channel == "sms" else lane.service.call_script
    op = prepare(lane, binding, channel=channel, text=text)
    lane.service.authorize(op["operationId"], actor_tenant_id=lane.tenant, actor_id=lane.actor)
    old_service.receive_webhook(kind, *callback)
    assert any(row.get("reason") == "communications_contact_suppressed" for row in lane.service.run_once())
    fails("communications_contact_suppressed", lambda: prepare(lane, binding, channel=channel, text=text))
    key = "sms" if channel == "sms" else "automatedVoice"
    assert lane.service.case_view(lane.case)["suppression"][key] is True
    assert lane.vendor.requests == []
