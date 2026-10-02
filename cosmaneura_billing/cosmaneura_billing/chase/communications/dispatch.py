"""Durable, default-off SMS/scripted-call dispatch and signed callback lifecycle.

Staff/API instances need public sender metadata only. Only a separately hosted
worker/callback receiver injects a credential-bearing adapter. A permanent start
commits before network I/O: a crash can lose a send, but cannot silently resend it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import re

from ...direct_contract import DirectContractList, check_direct_contract
from ...communications_channels import enabled_channels
from ...communications_test_policy import validated_test_recipients
from ..models import TERMINAL
from ..policy import assert_no_phi, MessageContainsPHI
from ..scheduling import _practice_zone, _quiet_hours, _inside_quiet_hours
from .postgres import (PostgresCommunicationsStore, _uuid, _iso, _event_time,
                       _assert_no_unknown_dispatch, _try_execution_lock)
from .event_review import CommunicationsEventReview, review_ref
from .store import CommunicationsError, _hash, _STOP, _START
from .twilio import (TwilioAdapter, TwilioError, _sid, _phone, _url,
                     call_script_text, gather_response_twiml)


def _fail(code, status=409):
    raise CommunicationsError(status, "communications_" + code)


@dataclass(frozen=True)
class SenderConfig:
    account_sid: str
    from_number: str = field(repr=False)
    sms_inbound_url: str
    sms_status_url: str
    voice_status_url: str
    voice_action_url: str
    practice_name: str = "your practice"
    messaging_service_sid: str = ""

    def __post_init__(self):
        _sid(self.account_sid, "AC")
        if self.messaging_service_sid != "":
            _sid(self.messaging_service_sid, "MG")
        _phone(self.from_number)
        urls = (self.sms_inbound_url, self.sms_status_url, self.voice_status_url, self.voice_action_url)
        for url in urls:
            _url(url)
        if (len(set(urls)) != 4 or not isinstance(self.practice_name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &'().,-]{0,79}", self.practice_name)
                or self.practice_name != self.practice_name.strip()):
            raise ValueError("invalid communications sender configuration")


@dataclass(frozen=True)
class DispatchPolicy:
    practice_name: str
    timezone: str
    quiet_hours: tuple[int, int] = (21, 8)
    max_total_touches: int = 8
    max_touches_24h: int = 2
    contact_max_age_hours: int = 24
    approval_max_age_hours: int = 24
    scheduler_enabled: bool = False
    patient_messaging_enabled: bool = False
    operationally_activated: bool = False
    enabled_channels: tuple[str, ...] = ("sms",)
    test_mode: bool = False
    test_recipients: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        object.__setattr__(self, "enabled_channels", enabled_channels(self.enabled_channels))
        object.__setattr__(self, "test_recipients", validated_test_recipients(
            self.test_mode, self.test_recipients, self.enabled_channels))
        _practice_zone(self.timezone)
        _quiet_hours(self.quiet_hours)
        if not isinstance(self.quiet_hours, tuple):
            raise ValueError("quiet hours must be immutable")
        for value, ceiling in [(self.max_total_touches, 50), (self.max_touches_24h, 10),
                               (self.contact_max_age_hours, 168), (self.approval_max_age_hours, 168)]:
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid communications policy limit")
        if any(type(getattr(self, name)) is not bool for name in
               ("scheduler_enabled", "patient_messaging_enabled", "operationally_activated")):
            raise ValueError("invalid communications activation latch")

    @property
    def templates(self):
        return [{"id": "reminder", "label": "Reminder",
                 "text": f"{self.practice_name}: Please contact our office about the information we requested. Reply STOP to stop."},
                {"id": "callback", "label": "Offer a callback",
                 "text": f"{self.practice_name}: Please reply if you would like our staff to call you. Do not send medical or insurance details by text. Reply STOP to stop."}]


_SMS_RANK = {"accepted": 0, "scheduled": 0, "queued": 1, "sending": 2, "sent": 3,
             "failed": 4, "undelivered": 4, "canceled": 4, "delivered": 5, "read": 6}
_VOICE_RANK = {"queued": 0, "initiated": 1, "ringing": 2, "in-progress": 3,
               "busy": 4, "failed": 4, "no-answer": 4, "canceled": 4, "completed": 5}


class CommunicationsDispatcher:
    def __init__(self, store: PostgresCommunicationsStore, *, policy: DispatchPolicy,
                 sender: SenderConfig, roster: DirectContractList,
                 adapter: TwilioAdapter | None = None, transaction_audit=None, clock=None):
        if (not isinstance(store, PostgresCommunicationsStore) or not isinstance(policy, DispatchPolicy)
                or not isinstance(sender, SenderConfig) or not isinstance(roster, DirectContractList)
                or sender.practice_name != policy.practice_name
                or (transaction_audit is not None and not callable(transaction_audit))):
            raise ValueError("invalid communications dispatcher configuration")
        if adapter is not None and (not isinstance(adapter, TwilioAdapter)
                or adapter.config.enabled_channels != policy.enabled_channels or any(
                getattr(adapter.config, key) != value for key, value in asdict(sender).items())):
            raise ValueError("communications adapter does not match public sender")
        self.store, self.policy, self.sender, self.roster = store, policy, sender, roster
        self.adapter, self.transaction_audit = adapter, transaction_audit
        self.clock = clock
        values = asdict(policy)
        # Deployment latches are independently checked at every claim, never
        # inferred from a policy approval made while the worker was disabled.
        for key in ("scheduler_enabled", "patient_messaging_enabled", "operationally_activated"):
            del values[key]
        # Preserve approvals for existing inactive configurations. An active
        # restriction is approval-bearing, including an empty (closed) scope.
        if not policy.test_mode:
            del values["test_mode"]
            del values["test_recipients"]
        self.policy_hash = _hash([values, asdict(sender), self.call_script])

    def require_channel(self, channel):
        if channel not in self.policy.enabled_channels:
            _fail("channel_disabled", 403)
        self.store.require_channel(channel)

    def _require_test_recipient(self, channel, phone):
        self.require_channel(channel)
        if self.policy.test_mode:
            if not self.policy.test_recipients:
                _fail("test_recipients_unconfigured", 403)
            if phone not in self.policy.test_recipients:
                _fail("test_recipient_not_allowed", 403)

    @property
    def call_script(self):
        return call_script_text(self.sender.practice_name)

    def _now(self, db):
        # Default eligibility and persisted evidence share the database clock.
        now = self.clock() if self.clock is not None else db.execute(
            "SELECT clock_timestamp() AS now").fetchone()["now"]
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            _fail("invalid_clock", 503)
        return now.astimezone(timezone.utc)

    def _audit(self, db, action, case_id=None, operation_id=None):
        if self.transaction_audit is not None:
            self.transaction_audit(db, action, case_id, operation_id)

    def _admin(self, db, tenant, actor):
        tenant, actor = self.store._staff(db, tenant, actor)
        if db.execute("SELECT cosmaneura_chase_admin_allowed(%s::uuid,%s::uuid) AS allowed",
                      (tenant, actor)).fetchone()["allowed"] is not True:
            _fail("admin_required", 403)
        return tenant, actor

    def approve_policy(self, *, decision_id, actor_tenant_id, actor_id, approved=True):
        decision_id = _uuid(decision_id)
        if type(approved) is not bool:
            _fail("invalid_policy_decision", 400)
        with self.store._db() as db:
            tenant, actor = self._admin(db, actor_tenant_id, actor_id)
            prior = db.execute("SELECT * FROM chase_communication_policy_decisions WHERE practice_id=%s AND decision_id=%s",
                               (self.store.practice_id, decision_id)).fetchone()
            if prior and (prior["policy_hash"], prior["approved"], str(prior["actor_tenant_id"]), str(prior["actor_id"])) != (
                    self.policy_hash, approved, tenant, actor):
                _fail("idempotency_conflict")
            if not prior:
                db.execute("""INSERT INTO chase_communication_policy_decisions
                    (practice_id,decision_id,policy_hash,approved,actor_tenant_id,actor_id)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (self.store.practice_id, decision_id, self.policy_hash, approved, tenant, actor))
            self._audit(db, "communications_policy_decision")
        return {"decisionId": decision_id, "policyHash": self.policy_hash, "approved": approved}

    def record_consent(self, *, decision_id, binding_id, granted, source_reference,
                       actor_tenant_id, actor_id):
        decision_id, binding_id = _uuid(decision_id), _uuid(binding_id)
        if (type(granted) is not bool or not isinstance(source_reference, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", source_reference)):
            _fail("invalid_consent_decision", 400)
        with self.store._db() as db:
            tenant, actor = self.store._staff(db, actor_tenant_id, actor_id)
            binding = db.execute("SELECT * FROM chase_communication_bindings WHERE practice_id=%s AND binding_id=%s",
                                 (self.store.practice_id, binding_id)).fetchone()
            if not binding:
                _fail("case_not_bound", 404)
            if granted:
                self.require_channel(binding["channel"])
            if granted:
                route = self.store._snapshot(db, binding["case_id"], str(binding["contact_snapshot_id"]), binding["channel"])
                if route["consent_status"] not in {"granted", "unknown", "not_required"}:
                    _fail("consent_unavailable", 403)
            prior = db.execute("SELECT * FROM chase_communication_consent_decisions WHERE practice_id=%s AND decision_id=%s",
                               (self.store.practice_id, decision_id)).fetchone()
            if prior and (str(prior["binding_id"]), prior["granted"], prior["source_reference"], str(prior["actor_tenant_id"]), str(prior["actor_id"])) != (
                    binding_id, granted, source_reference, tenant, actor):
                _fail("idempotency_conflict")
            if not prior:
                db.execute("""INSERT INTO chase_communication_consent_decisions
                    (practice_id,decision_id,binding_id,granted,source_reference,actor_tenant_id,actor_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (self.store.practice_id, decision_id, binding_id, granted, source_reference, tenant, actor))
            self._audit(db, "communications_consent_decision", binding["case_id"])
        return {"decisionId": decision_id, "bindingId": binding_id, "granted": granted, "purpose": "insurance_followup"}

    def _policy_decision(self, db):
        row = db.execute("""SELECT * FROM chase_communication_policy_decisions
            WHERE practice_id=%s ORDER BY sequence_no DESC LIMIT 1""", (self.store.practice_id,)).fetchone()
        if not row or row["approved"] is not True or row["policy_hash"] != self.policy_hash:
            _fail("policy_approval_required", 403)
        self._admin(db, str(row["actor_tenant_id"]), str(row["actor_id"]))
        return row

    def _operation(self, db, operation_id):
        row = db.execute("SELECT * FROM chase_communication_operations WHERE practice_id=%s AND operation_id=%s",
                         (self.store.practice_id, _uuid(operation_id))).fetchone()
        if not row:
            _fail("operation_not_found", 404)
        return row

    def _suppressed(self, db, binding):
        if self.store._suppressed(db, binding):
            return True
        if db.execute("""SELECT 1 FROM chase_communication_suppressions
            WHERE practice_id=%s AND phone=%s AND channel=%s LIMIT 1""",
            (self.store.practice_id, binding["phone"], binding["channel"])).fetchone():
            return True
        return db.execute("""SELECT 1 FROM chase_communication_provider_events
            WHERE practice_id=%s AND channel=%s AND
              ((kind='sms_inbound' AND sender=%s AND disposition='sms_opt_out') OR
               (kind='voice_gather' AND recipient=%s AND disposition='stop_calls')) LIMIT 1""",
            (self.store.practice_id, binding["channel"], binding["phone"], binding["phone"])).fetchone() is not None

    def _gates(self, db, op, *, phase, authorization=None):
        if not isinstance(phase, str) or phase not in {"prepare", "authorize", "dispatch"}:
            _fail("invalid_gate_phase", 400)
        self.require_channel(op["channel"])
        policy = self._policy_decision(db)
        self.store._staff(db, str(op["actor_tenant_id"]), str(op["actor_id"]))
        # Match the existing attempt/import writers' case lock. Besides case
        # updates, FOR UPDATE conflicts with the FK key lock of a concurrent
        # contact-snapshot insert until the permanent start has committed.
        item = db.execute("SELECT * FROM chase_items WHERE practice_id=%s AND chase_id=%s FOR UPDATE",
                          (self.store.practice_id, op["case_id"])).fetchone()
        if item is None:
            _fail("case_not_found", 404)
        now = self._now(db)
        _assert_no_unknown_dispatch(db, self.store.practice_id, op["case_id"])
        if item["state"] in {state.value for state in TERMINAL} | {"submitted", "awaiting_verification"}:
            _fail("case_blocked", 403)
        direct = item["direct_contract"]
        check = check_direct_contract(item["patient_ref"], roster=self.roster, now=lambda: _iso(now))
        if not isinstance(direct, dict) or direct.get("is_direct_contract") is not False or check.blocked:
            _fail("direct_contract_blocked", 403)
        binding = self.store._binding(db, op["case_id"], str(op["binding_id"]), op["channel"])
        if binding["account_sid"] != self.sender.account_sid or binding["sender"] != self.sender.from_number:
            _fail("sender_changed")
        route = self.store._snapshot(db, op["case_id"], str(binding["contact_snapshot_id"]), op["channel"])
        self._require_test_recipient(op["channel"], binding["phone"])
        snapshot = db.execute("SELECT verified_at,observed_at FROM chase_contact_snapshots WHERE practice_id=%s AND contact_snapshot_id=%s",
                              (self.store.practice_id, binding["contact_snapshot_id"])).fetchone()
        if any(not timedelta(0) <= now - snapshot[key] <= timedelta(hours=self.policy.contact_max_age_hours)
               for key in ("verified_at", "observed_at")):
            _fail("contact_stale")
        consent = db.execute("""SELECT * FROM chase_communication_consent_decisions
            WHERE practice_id=%s AND binding_id=%s ORDER BY sequence_no DESC LIMIT 1""",
            (self.store.practice_id, binding["binding_id"])).fetchone()
        if route["consent_status"] not in {"granted", "unknown", "not_required"} or not consent or consent["granted"] is not True:
            _fail("purpose_consent_required", 403)
        if self._suppressed(db, binding):
            _fail("contact_suppressed")
        if op["channel"] == "automated_voice":
            if op["text"] != self.call_script:
                _fail("script_approval_mismatch")
        else:
            try:
                assert_no_phi(op["text"])
            except MessageContainsPHI:
                _fail("message_requires_review", 403)
            if "reply stop" not in op["text"].casefold() or self.policy.practice_name.casefold() not in op["text"].casefold():
                _fail("message_requires_identity_and_opt_out", 403)
        if authorization:
            self.store._staff(db, str(authorization["actor_tenant_id"]), str(authorization["actor_id"]))
            if (authorization["policy_decision_id"] != policy["decision_id"]
                    or authorization["consent_decision_id"] != consent["decision_id"]
                    or authorization["direct_contract_hash"] != check.gate_fingerprint):
                _fail("approval_stale")
            if not timedelta(0) <= now - authorization["created_at"] <= timedelta(hours=self.policy.approval_max_age_hours):
                _fail("approval_expired")
        # Preparing an SMS is not a decision to send. Only activation and quiet
        # hours are deferred; touch limits and all shared safeguards still apply.
        # Keep the existing explicitly enabled voice preparation/approval policy.
        live = (phase == "dispatch" or phase == "authorize" and op["channel"] == "sms"
                or phase == "prepare" and op["channel"] == "automated_voice")
        if live:
            if not all((self.policy.scheduler_enabled, self.policy.patient_messaging_enabled, self.policy.operationally_activated)):
                _fail("dispatch_disabled", 403)
            if _inside_quiet_hours(now.astimezone(_practice_zone(self.policy.timezone)), self.policy.quiet_hours):
                _fail("quiet_hours")
        if phase != "authorize" or op["channel"] == "sms":
            manual = db.execute("SELECT attempted_at FROM chase_attempts WHERE practice_id=%s AND chase_id=%s LIMIT 51",
                                (self.store.practice_id, op["case_id"])).fetchall()
            try:
                times = [datetime.fromisoformat(_event_time(row["attempted_at"])) for row in manual]
            except CommunicationsError:
                _fail("touch_evidence_invalid")
            times.extend(row["started_at"] for row in db.execute("""SELECT d.started_at FROM chase_communication_dispatch_starts d
                JOIN chase_communication_operations o USING(practice_id,operation_id)
                WHERE d.practice_id=%s AND o.case_id=%s LIMIT 51""", (self.store.practice_id, op["case_id"])).fetchall())
            if any(value > now for value in times):
                _fail("touch_evidence_invalid")
            if len(times) >= self.policy.max_total_touches or sum(value >= now - timedelta(hours=24) for value in times) >= self.policy.max_touches_24h:
                _fail("touch_limit")
        return binding, policy, consent, check

    def authorize(self, operation_id, *, actor_tenant_id, actor_id):
        operation_id = _uuid(operation_id)
        with self.store._db() as db:
            tenant, actor = self.store._staff(db, actor_tenant_id, actor_id)
            op = self._operation(db, operation_id)
            self.require_channel(op["channel"])
            if self._canceled(db, operation_id):
                _fail("operation_canceled")
            prior = db.execute("SELECT * FROM chase_communication_authorizations WHERE practice_id=%s AND operation_id=%s",
                               (self.store.practice_id, operation_id)).fetchone()
            if prior:
                if (str(prior["actor_tenant_id"]), str(prior["actor_id"])) != (tenant, actor):
                    _fail("idempotency_conflict")
            else:
                binding, policy, consent, check = self._gates(db, op, phase="authorize")
                if self._canceled(db, operation_id):
                    _fail("operation_canceled")
                db.execute("""INSERT INTO chase_communication_authorizations
                    (practice_id,operation_id,policy_decision_id,consent_decision_id,binding_id,direct_contract_hash,actor_tenant_id,actor_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (self.store.practice_id, operation_id, policy["decision_id"],
                    consent["decision_id"], binding["binding_id"], check.gate_fingerprint, tenant, actor))
            self._audit(db, "communications_authorize", op["case_id"], operation_id)
        return {"operationId": operation_id, "status": "authorized", "sent": False}

    def _canceled(self, db, operation_id):
        return db.execute("SELECT 1 FROM chase_communication_cancellations WHERE practice_id=%s AND operation_id=%s",
                          (self.store.practice_id, operation_id)).fetchone() is not None

    def cancel(self, operation_id, *, actor_tenant_id, actor_id):
        """Cancel before the permanent start. Never contact or recall a vendor."""
        operation_id = _uuid(operation_id)
        with self.store._db() as db:
            tenant, actor = self.store._staff(db, actor_tenant_id, actor_id)
            op = self._operation(db, operation_id)
            db.execute("SELECT 1 FROM chase_items WHERE practice_id=%s AND chase_id=%s FOR UPDATE",
                       (self.store.practice_id, op["case_id"]))
            if db.execute("SELECT 1 FROM chase_communication_dispatch_starts WHERE practice_id=%s AND operation_id=%s",
                          (self.store.practice_id, operation_id)).fetchone():
                _fail("dispatch_already_started")
            if not self._canceled(db, operation_id):
                db.execute("""INSERT INTO chase_communication_cancellations
                    (practice_id,operation_id,actor_tenant_id,actor_id) VALUES (%s,%s,%s,%s)""",
                    (self.store.practice_id, operation_id, tenant, actor))
            self._audit(db, "communications_cancel", op["case_id"], operation_id)
        return {"operationId": operation_id, "status": "canceled", "sent": False}

    def _claim(self, operation_id):
        with self.store._db() as db:
            op = self._operation(db, operation_id)
            if self._canceled(db, operation_id):
                return None
            if db.execute("SELECT 1 FROM chase_communication_dispatch_starts WHERE practice_id=%s AND operation_id=%s",
                          (self.store.practice_id, operation_id)).fetchone():
                return None
            authorization = db.execute("SELECT * FROM chase_communication_authorizations WHERE practice_id=%s AND operation_id=%s",
                                       (self.store.practice_id, operation_id)).fetchone()
            if not authorization:
                _fail("approval_required", 403)
            binding, _, _, _ = self._gates(db, op, phase="dispatch", authorization=authorization)
            # Recheck after the case lock also covers a direct database cancel.
            if self._canceled(db, operation_id):
                return None
            request = (self.adapter.build_sms(binding["phone"], op["text"]) if op["channel"] == "sms"
                       else self.adapter.build_call(binding["phone"]))
            db.execute("INSERT INTO chase_communication_dispatch_starts(practice_id,operation_id) VALUES (%s,%s)",
                       (self.store.practice_id, operation_id))
            self._audit(db, "communications_dispatch_start", op["case_id"], operation_id)
            return request

    def resolve_unknown(self, operation_id, *, decision, vendor_reference, actor_tenant_id, actor_id):
        operation_id = _uuid(operation_id)
        if (decision not in {"confirmed_delivered", "confirmed_not_sent", "confirmed_attempted"} or not isinstance(vendor_reference, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", vendor_reference)):
            _fail("invalid_unknown_resolution", 400)
        with self.store._db() as db:
            tenant, actor = self.store._staff(db, actor_tenant_id, actor_id)
            op = self._operation(db, operation_id)
            if not _try_execution_lock(db, self.store.practice_id, operation_id):
                _fail("dispatch_in_progress")
            db.execute("SELECT 1 FROM chase_items WHERE practice_id=%s AND chase_id=%s FOR UPDATE",
                       (self.store.practice_id, op["case_id"]))
            prior = db.execute("SELECT * FROM chase_communication_unknown_resolutions WHERE practice_id=%s AND operation_id=%s",
                               (self.store.practice_id, operation_id)).fetchone()
            if prior:
                if (prior["decision"], prior["vendor_reference"]) != (decision, vendor_reference):
                    _fail("idempotency_conflict")
            else:
                state = db.execute("""SELECT d.operation_id,r.outcome FROM chase_communication_dispatch_starts d
                    LEFT JOIN chase_communication_dispatch_receipts r USING(practice_id,operation_id)
                    WHERE d.practice_id=%s AND d.operation_id=%s""", (self.store.practice_id, operation_id)).fetchone()
                if not state or state["outcome"] not in {None, "unknown"}:
                    _fail("outcome_not_unknown")
                db.execute("""INSERT INTO chase_communication_unknown_resolutions
                    (practice_id,operation_id,decision,vendor_reference,actor_tenant_id,actor_id)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (self.store.practice_id, operation_id, decision, vendor_reference, tenant, actor))
            self._audit(db, "communications_resolve_unknown", op["case_id"], operation_id)
        return {"operationId": operation_id, "decision": decision}

    def _submit_started(self, operation_id, request):
        # Defense at the last transport boundary. The adapter also verifies the
        # whole request against its canonical destination/body before any I/O.
        self._require_test_recipient("automated_voice" if request.channel == "voice" else request.channel,
                                     request.to_number)
        with self.store._execution(operation_id) as db:
            # A missing-receipt investigation can win between claim and
            # execution. It permanently consumes that start without a send.
            if db.execute("""SELECT 1 FROM chase_communication_unknown_resolutions WHERE practice_id=%s AND operation_id=%s
                UNION ALL SELECT 1 FROM chase_communication_dispatch_receipts WHERE practice_id=%s AND operation_id=%s""",
                (self.store.practice_id, operation_id, self.store.practice_id, operation_id)).fetchone():
                return None
            receipt = self.adapter.submit(request)
            # Acquire the practice lock only after I/O; the execution lock and
            # receipt commit share this connection, while early callbacks remain
            # free to acquire the practice lock during the provider request.
            db.execute("SELECT pg_advisory_xact_lock(%s)", (self.store._lock_key,))
            db.execute("""INSERT INTO chase_communication_dispatch_receipts
                (practice_id,operation_id,account_sid,outcome,provider_sid,status,error_code)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (self.store.practice_id, operation_id, self.sender.account_sid,
                receipt.outcome, receipt.provider_id, receipt.provider_status, receipt.error_code))
            op = self._operation(db, operation_id)
            self._audit(db, "communications_dispatch_receipt", op["case_id"], operation_id)
            return receipt

    def run_once(self, *, limit=25):
        if not self.adapter or self.adapter.transport is None:
            _fail("transport_disabled", 503)
        if type(limit) is not int or not 1 <= limit <= 50:
            _fail("invalid_batch_limit", 400)
        with self.store._db() as db:
            rows = db.execute("""SELECT a.operation_id FROM chase_communication_authorizations a
                LEFT JOIN chase_communication_dispatch_starts d USING(practice_id,operation_id)
                LEFT JOIN chase_communication_cancellations x USING(practice_id,operation_id)
                LEFT JOIN LATERAL (SELECT checked_at FROM chase_communication_dispatch_checks c
                  WHERE c.practice_id=a.practice_id AND c.operation_id=a.operation_id ORDER BY checked_at DESC LIMIT 1) c ON true
                WHERE a.practice_id=%s AND d.operation_id IS NULL AND x.operation_id IS NULL
                ORDER BY COALESCE(c.checked_at,a.created_at),a.operation_id LIMIT %s""",
                (self.store.practice_id, limit)).fetchall()
        results = []
        for row in rows:
            operation_id = str(row["operation_id"])
            try:
                request = self._claim(operation_id)
            except CommunicationsError as exc:
                if exc.status >= 500:
                    raise
                with self.store._db() as db:
                    db.execute("INSERT INTO chase_communication_dispatch_checks(practice_id,operation_id,reason) VALUES (%s,%s,%s)",
                               (self.store.practice_id, operation_id, exc.code))
                results.append({"operationId": operation_id, "status": "blocked", "reason": exc.code})
                continue
            if request is None:
                continue
            # A process exit here deliberately leaves a consumed, uncertain start.
            receipt = self._submit_started(operation_id, request)
            if receipt is None:
                continue
            results.append({"operationId": operation_id, "status": receipt.outcome})
        return results

    def receive_webhook(self, kind, raw_form, signature):
        """Verify exact configured URL/account signature before any DB mutation.

        No MediaUrl is fetched, and no raw request/unknown provider fields persist.
        Early callbacks remain unassigned until a validated HTTP receipt binds SID.
        """
        if not self.adapter:
            _fail("callback_adapter_required", 503)
        try:
            event = self.adapter.parse_webhook(kind, raw_form, signature)
        except TwilioError:
            _fail("invalid_callback", 403)
        values = asdict(event)
        body_hash = _hash(values)
        event_id = _hash([kind, event.provider_id, event.provider_sequence] if event.provider_sequence is not None
                         else [kind, event.provider_id] if kind == "sms_inbound" else values)
        channel = "sms" if event.channel == "sms" else "automated_voice"
        disposition = event.disposition
        if kind == "sms_inbound":
            word = (event.text or "").strip().upper()
            disposition = ("sms_opt_out" if event.opt_out == "STOP" or word in _STOP else
                           "sms_opt_in_review" if event.opt_out == "START" or word in _START else
                           "help_requested" if event.opt_out == "HELP" or word in {"HELP", "INFO"} else "reply_received")
        with self.store._db() as db:
            # Exact sender registration determines tenant authority, not a webhook
            # body or a patient supplied practice ID.
            if not db.execute("SELECT 1 FROM chase_communication_senders WHERE practice_id=%s AND account_sid=%s AND sender=%s",
                              (self.store.practice_id, self.sender.account_sid, self.sender.from_number)).fetchone():
                _fail("callback_binding_mismatch", 403)
            prior = db.execute("SELECT * FROM chase_communication_provider_events WHERE practice_id=%s AND account_sid=%s AND event_id=%s",
                               (self.store.practice_id, self.sender.account_sid, event_id)).fetchone()
            if prior and prior["body_hash"] != body_hash:
                _fail("event_conflict")
            if not prior:
                case_id = None
                if kind == "sms_inbound":
                    matches = db.execute("""SELECT DISTINCT b.case_id FROM chase_communication_bindings b
                        WHERE b.practice_id=%s AND b.account_sid=%s AND b.sender=%s AND b.phone=%s AND b.channel='sms'
                          AND b.contact_snapshot_id=(SELECT s.contact_snapshot_id FROM chase_contact_snapshots s
                            WHERE s.practice_id=b.practice_id AND s.chase_id=b.case_id
                            ORDER BY s.verified_at DESC,s.created_at DESC,s.contact_snapshot_id DESC LIMIT 1)""",
                        (self.store.practice_id, self.sender.account_sid, event.to_number, event.from_number)).fetchall()
                    if len(matches) == 1:
                        case_id = matches[0]["case_id"]
                db.execute("""INSERT INTO chase_communication_provider_events
                    (practice_id,account_sid,event_id,provider_sid,kind,channel,sender,recipient,case_id,text,status,
                     disposition,opt_out,media_count,provider_sequence,body_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (self.store.practice_id, self.sender.account_sid, event_id, event.provider_id, kind, channel,
                     event.from_number, event.to_number, case_id, event.text or "", event.provider_status,
                     disposition, event.opt_out, event.media_count, event.provider_sequence, body_hash))
                self._audit(db, "communications_provider_event", case_id)
        return {"accepted": True, "eventId": event_id,
                "twiml": gather_response_twiml() if kind == "voice_gather" else "<Response />"}

    def case_view(self, case_id):
        view = self.store.case_view(case_id)
        with self.store._db() as db:
            # Contact numbers remain inside server bindings; never project them.
            rows = db.execute("""SELECT o.operation_id,a.created_at AS authorized_at,d.started_at,
                r.outcome,r.provider_sid,r.status,r.account_sid,c.reason,c.checked_at,x.created_at AS canceled_at,
                u.decision,u.vendor_reference,u.created_at AS resolved_at
                FROM chase_communication_operations o
                LEFT JOIN chase_communication_authorizations a USING(practice_id,operation_id)
                LEFT JOIN chase_communication_dispatch_starts d USING(practice_id,operation_id)
                LEFT JOIN chase_communication_dispatch_receipts r USING(practice_id,operation_id)
                LEFT JOIN chase_communication_cancellations x USING(practice_id,operation_id)
                LEFT JOIN chase_communication_unknown_resolutions u USING(practice_id,operation_id)
                LEFT JOIN LATERAL (SELECT reason,checked_at FROM chase_communication_dispatch_checks c
                  WHERE c.practice_id=o.practice_id AND c.operation_id=o.operation_id
                  ORDER BY c.checked_at DESC,c.sequence_no DESC LIMIT 1) c ON true
                WHERE o.practice_id=%s AND o.case_id=%s AND o.operation_id=ANY(%s::uuid[])""",
                (self.store.practice_id, case_id, [op["operationId"] for op in view["operations"]])).fetchall()
            states = {str(row["operation_id"]): row for row in rows}
            events = db.execute("""SELECT e.*,r.operation_id FROM chase_communication_provider_events e
                LEFT JOIN chase_communication_dispatch_receipts r ON r.practice_id=e.practice_id
                  AND r.account_sid=e.account_sid AND r.provider_sid=e.provider_sid
                LEFT JOIN chase_communication_operations o ON o.practice_id=r.practice_id AND o.operation_id=r.operation_id
                LEFT JOIN chase_communication_bindings b ON b.practice_id=o.practice_id AND b.binding_id=o.binding_id
                WHERE e.practice_id=%s AND (e.case_id=%s OR (o.case_id=%s AND e.kind<>'sms_inbound'
                  AND (e.sender IS NULL OR e.sender=b.sender) AND (e.recipient IS NULL OR e.recipient=b.phone)))
                ORDER BY e.sequence_no DESC LIMIT 201""", (self.store.practice_id, case_id, case_id)).fetchall()
            # Current delivery state must not regress when the visible history
            # window truncates older terminal callbacks. Grouping bounds each
            # displayed operation to the finite provider status vocabulary.
            statuses = db.execute("""SELECT r.operation_id,e.status FROM chase_communication_dispatch_receipts r
                JOIN chase_communication_operations o USING(practice_id,operation_id)
                JOIN chase_communication_bindings b ON b.practice_id=o.practice_id AND b.binding_id=o.binding_id
                JOIN chase_communication_provider_events e ON e.practice_id=r.practice_id AND e.account_sid=r.account_sid
                  AND e.provider_sid=r.provider_sid AND e.kind IN ('sms_status','voice_status')
                WHERE r.practice_id=%s AND o.operation_id=ANY(%s::uuid[])
                  AND (e.sender IS NULL OR e.sender=b.sender) AND (e.recipient IS NULL OR e.recipient=b.phone)
                GROUP BY r.operation_id,e.status ORDER BY max(e.sequence_no)""",
                (self.store.practice_id, [op["operationId"] for op in view["operations"]])).fetchall()
            for op in view["operations"]:
                state = states[op["operationId"]]
                op["status"] = (state["status"] if state["outcome"] == "accepted" else state["outcome"] or
                                ("unknown" if state["started_at"] else "canceled" if state["canceled_at"] else "blocked" if state["checked_at"] else
                                 "authorized" if state["authorized_at"] else "prepared"))
                op["cancelable"] = state["started_at"] is None and state["canceled_at"] is None
                op["canceledAt"] = _iso(state["canceled_at"]) if state["canceled_at"] else None
                op["unknownResolution"] = ({"decision": state["decision"], "vendorReference": state["vendor_reference"],
                    "resolvedAt": _iso(state["resolved_at"])} if state["resolved_at"] else None)
                op["canResolveUnknown"] = bool(op["status"] == "unknown" and not state["resolved_at"]
                    and _try_execution_lock(db, self.store.practice_id, op["operationId"]))
                pending_check = state["checked_at"] is not None and op["cancelable"]
                op["blockedReason"] = state["reason"] if pending_check else None
                op["lastCheckedAt"] = _iso(state["checked_at"]) if pending_check else None
                op["providerSid"] = state["provider_sid"]
                ranks = _SMS_RANK if op["channel"] == "sms" else _VOICE_RANK
                relevant = [e for e in statuses if str(e["operation_id"]) == op["operationId"]]
                for e in relevant:
                    if ranks.get(e["status"], -1) >= ranks.get(op["status"], -1):
                        op["status"] = e["status"]
                op["needsStaffReview"] = (op["status"] in {"blocked", "unknown", "failed", "undelivered", "rejected", "busy", "no-answer"}
                    and not state["resolved_at"])
            for e in reversed(events[:200]):
                view["events"].append({"eventId": e["event_id"], "reviewRef": review_ref("provider", e["account_sid"], e["event_id"]), "operationId": str(e["operation_id"]) if e["operation_id"] else None,
                    "providerSid": e["provider_sid"], "kind": e["kind"], "direction": "inbound" if e["kind"] == "sms_inbound" else "callback",
                    "channel": e["channel"], "text": e["text"], "status": e["status"], "disposition": e["disposition"],
                    "occurredAt": _iso(e["received_at"]), "receivedAt": _iso(e["received_at"]),
                    "needsStaffReview": e["kind"] == "sms_inbound" or bool(e["disposition"]), "held": False,
                    "mediaCount": e["media_count"], "actorId": None, "actorTenantId": None})
            bindings = db.execute("""SELECT DISTINCT ON(channel) * FROM chase_communication_bindings
                WHERE practice_id=%s AND case_id=%s ORDER BY channel,created_at DESC,binding_id DESC""",
                (self.store.practice_id, case_id)).fetchall()
            for binding in bindings:
                key = "sms" if binding["channel"] == "sms" else "automatedVoice"
                view["suppression"][key] = self._suppressed(db, binding)
            held = db.execute("""SELECT count(*) AS count FROM chase_communication_provider_events e
                WHERE e.practice_id=%s AND e.kind='sms_inbound' AND e.case_id IS NULL AND EXISTS
                (SELECT 1 FROM chase_communication_bindings b WHERE b.practice_id=e.practice_id AND b.case_id=%s
                 AND b.channel='sms' AND b.account_sid=e.account_sid AND b.sender=e.recipient AND b.phone=e.sender)""",
                (self.store.practice_id, case_id)).fetchone()["count"]
            view["heldInboundCount"] += held
            view["truncated"] = view["truncated"] or len(events) > 200
            CommunicationsEventReview(self).apply_to_history(db, case_id, view)
        view["events"].sort(key=lambda event: (event["receivedAt"], event["eventId"]))
        view["truncated"] = view["truncated"] or len(view["events"]) > 200
        view["events"] = view["events"][-200:]
        view.update(mode="durable", callScript=self.call_script, templates=self.policy.templates,
                    enabledChannels=list(self.policy.enabled_channels))
        return view
