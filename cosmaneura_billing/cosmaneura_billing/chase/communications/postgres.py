"""Practice-isolated durable preparations and inbound communications evidence.

``prepare`` never submits, queues, increments an Attempt, or changes a
chase/contact/clinical record. The optional migration-012 integration in
``dispatch`` separately authorizes and dispatches immutable preparations.

The authenticated host supplies practice and actor identity and builds Eligibility
from server-owned policy. Provider signatures and sender-to-practice routing must
be verified before receive_sms; caller-supplied practice IDs are not authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import re
from uuid import UUID
from ...communications_channels import enabled_channels as validate_enabled_channels

from .store import (
    CommunicationsError, Eligibility, _ACCOUNT, _PHONE, _SID, _START, _STOP,
    _check, _hash, _text,
)

_EVENT_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)


@dataclass(frozen=True)
class SmsPreparationEligibility:
    """Server-only preparation checks; neither quiet-hours nor send authority.

    The live dispatcher must freshly check every authorization/dispatch gate.
    This type is accepted only by the purpose-consent-enabled PostgreSQL SMS path.
    """
    role_allowed: bool = False
    case_open: bool = False
    direct_contract_clear: bool = False
    consent_valid: bool = False
    practice_enabled: bool = False
    policy_approved: bool = False
    touch_limit_clear: bool = False


def _event_time(value=None):
    """Strict RFC 3339 instants; unknown offsets are not authoritative times."""
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    try:
        if (not isinstance(value, str) or not _EVENT_TIME.fullmatch(value)
                or value.endswith("-00:00")):
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise CommunicationsError(400, "invalid_event_time") from None


def _uuid(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise CommunicationsError(400, "invalid_communications_id") from None


def _channel(value):
    if value not in ("sms", "automated_voice"):
        raise CommunicationsError(400, "invalid_communications_channel")
    return value


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _assert_no_unknown_dispatch(db, practice_id, case_id):
    if db.execute("""SELECT 1 FROM chase_communication_dispatch_starts d
        JOIN chase_communication_operations o USING(practice_id,operation_id)
        LEFT JOIN chase_communication_dispatch_receipts r USING(practice_id,operation_id)
        LEFT JOIN chase_communication_unknown_resolutions u USING(practice_id,operation_id)
        WHERE d.practice_id=%s AND o.case_id=%s AND u.operation_id IS NULL
          AND (r.operation_id IS NULL OR r.outcome='unknown') LIMIT 1""", (practice_id, case_id)).fetchone():
        raise CommunicationsError(409, "communications_unknown_requires_resolution")


def _try_execution_lock(db, practice_id, operation_id):
    return db.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('communications-send:' || %s || ':' || %s,0)) AS locked",
                      (practice_id, operation_id)).fetchone()["locked"] is True


def _operation(row):
    return {"operationId": str(row["operation_id"]), "caseId": row["case_id"],
            "channel": row["channel"], "direction": "outbound", "text": row["text"],
            "status": "prepared", "providerSid": None, "simulated": False,
            "createdAt": _iso(row["created_at"]), "needsStaffReview": True,
            "actorId": str(row["actor_id"]), "actorTenantId": str(row["actor_tenant_id"]),
            "bindingId": str(row["binding_id"])}


def _event(row):
    from .event_review import review_ref
    return {"eventId": row["event_id"], "reviewRef": review_ref("legacy", row["account_sid"], row["event_id"]),
            "operationId": str(row["operation_id"]) if row["operation_id"] else None,
            "providerSid": row["provider_sid"], "kind": row["kind"],
            "direction": row["direction"], "channel": row["channel"],
            "text": row["text"], "status": row["status"], "disposition": row["disposition"],
            "occurredAt": _iso(row["occurred_at"]), "receivedAt": _iso(row["received_at"]),
            "needsStaffReview": True, "held": row["case_id"] is None,
            "actorId": str(row["actor_id"]) if row["actor_id"] else None,
            "actorTenantId": str(row["actor_tenant_id"]) if row["actor_tenant_id"] else None}


class PostgresCommunicationsStore:
    """Immutable server practice scope; no default/global connection or transport."""

    def __init__(self, dsn: str, *, practice_id: str, transaction_audit=None, purpose_consent_enabled=False,
                 enabled_channels=("sms",)):
        import psycopg
        from psycopg.rows import dict_row
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("communications requires a database URL")
        self.practice_id = _check(practice_id)
        if transaction_audit is not None and not callable(transaction_audit):
            raise ValueError("invalid communications audit callback")
        self.transaction_audit = transaction_audit
        if type(purpose_consent_enabled) is not bool:
            raise ValueError("invalid purpose consent integration")
        self.purpose_consent_enabled = purpose_consent_enabled
        self.enabled_channels = validate_enabled_channels(enabled_channels)
        self._dsn, self._psycopg, self._dict_row = dsn, psycopg, dict_row
        # Serialize decisions and opt-outs across processes. Scoped advisory locks
        # avoid requiring UPDATE privileges on append-only evidence tables.
        self._lock_key = int.from_bytes(bytes.fromhex(_hash(["communications", practice_id])[:16]),
                                        "big", signed=True)

    def __repr__(self):
        return "PostgresCommunicationsStore(dsn='<redacted>', practice='<bound>')"

    def require_channel(self, channel):
        _channel(channel)
        if channel not in self.enabled_channels:
            raise CommunicationsError(403, "communications_channel_disabled")

    def _audit(self, db, action, case_id, operation_id=None):
        if self.transaction_audit is not None:
            self.transaction_audit(db, action, case_id, operation_id)

    @contextmanager
    def _db(self):
        try:
            with self._psycopg.connect(self._dsn, row_factory=self._dict_row,
                                       connect_timeout=3) as db:
                db.execute("SELECT set_config('cosmaneura.practice_id', %s, true)",
                           (self.practice_id,))
                db.execute("SET LOCAL statement_timeout = '5s'")
                db.execute("SET LOCAL lock_timeout = '3s'")
                db.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))
                yield db
        except self._psycopg.Error:
            # Provider text, contacts and SQL parameters never escape this boundary.
            raise CommunicationsError(503, "communications_store_unavailable") from None

    def _staff(self, db, tenant, actor):
        tenant, actor = _uuid(tenant), _uuid(actor)
        row = db.execute("SELECT cosmaneura_staff_assignee_allowed(%s::uuid, %s::uuid) AS allowed",
                         (tenant, actor)).fetchone()
        if not row or row["allowed"] is not True:
            raise CommunicationsError(403, "communications_staff_required")
        return tenant, actor

    @contextmanager
    def _execution(self, operation_id):
        # Separate from the practice transaction: provider callbacks can commit
        # while I/O is running. Resolution only tries this lock (never waits
        # holding the practice lock), so early callbacks cannot deadlock it.
        try:
            with self._psycopg.connect(self._dsn, row_factory=self._dict_row, connect_timeout=3) as db:
                db.execute("SELECT set_config('cosmaneura.practice_id',%s,true)", (self.practice_id,))
                db.execute("SET LOCAL statement_timeout='5s'")
                db.execute("SET LOCAL lock_timeout='3s'")
                db.execute("SELECT pg_advisory_xact_lock(hashtextextended('communications-send:' || %s || ':' || %s,0))",
                           (self.practice_id, _uuid(operation_id)))
                yield db
        except self._psycopg.Error:
            raise CommunicationsError(503, "communications_store_unavailable") from None

    def _case(self, db, case_id):
        row = db.execute("SELECT * FROM chase_items WHERE practice_id=%s AND chase_id=%s",
                         (self.practice_id, _check(case_id))).fetchone()
        if row is None:
            raise CommunicationsError(404, "communications_case_not_found")
        return row

    def _binding(self, db, case_id, binding_id, channel):
        row = db.execute("""SELECT * FROM chase_communication_bindings
                            WHERE practice_id=%s AND binding_id=%s AND case_id=%s AND channel=%s""",
                         (self.practice_id, _uuid(binding_id), _check(case_id), _channel(channel))).fetchone()
        if row is None:
            raise CommunicationsError(404, "communications_case_not_bound")
        return row

    def _snapshot(self, db, case_id, snapshot_id, channel):
        latest = db.execute("""SELECT contact_snapshot_id FROM chase_contact_snapshots
            WHERE practice_id=%s AND chase_id=%s
            ORDER BY verified_at DESC, created_at DESC, contact_snapshot_id DESC LIMIT 1""",
                            (self.practice_id, case_id)).fetchone()
        if not latest or str(latest["contact_snapshot_id"]) != _uuid(snapshot_id):
            raise CommunicationsError(409, "communications_contact_stale")
        route = db.execute("""SELECT * FROM chase_contact_routes WHERE practice_id=%s
            AND contact_snapshot_id=%s AND channel=%s""",
                           (self.practice_id, snapshot_id, "sms" if channel == "sms" else "phone")).fetchone()
        if route is None:
            raise CommunicationsError(409, "communications_contact_unavailable")
        _check(route["destination"], _PHONE)
        return route

    def bind_case(self, *, case_id, channel, contact_snapshot_id, sender, account_sid,
                  actor_tenant_id, actor_id):
        """Reference a verified snapshot; never accept or update a destination."""
        self.require_channel(channel)
        _check(sender, _PHONE)
        _check(account_sid, _ACCOUNT)
        with self._db() as db:
            tenant, actor = self._staff(db, actor_tenant_id, actor_id)
            item = self._case(db, case_id)
            route = self._snapshot(db, case_id, contact_snapshot_id, channel)
            db.execute("""INSERT INTO chase_communication_senders(practice_id, account_sid, sender)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""", (self.practice_id, account_sid, sender))
            owner = db.execute("""SELECT practice_id FROM chase_communication_senders
                WHERE practice_id=%s AND account_sid=%s AND sender=%s""",
                               (self.practice_id, account_sid, sender)).fetchone()
            if owner is None:
                raise CommunicationsError(409, "communications_sender_unavailable")
            row = db.execute("""INSERT INTO chase_communication_bindings
                (practice_id,case_id,channel,contact_snapshot_id,route_channel,account_sid,sender,
                 phone,patient_ref,actor_tenant_id,actor_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                RETURNING binding_id""", (self.practice_id, case_id, channel, contact_snapshot_id,
                route["channel"], account_sid, sender, route["destination"], item["patient_ref"], tenant, actor)).fetchone()
            if row is None:
                row = db.execute("""SELECT binding_id FROM chase_communication_bindings
                    WHERE practice_id=%s AND case_id=%s AND channel=%s AND contact_snapshot_id=%s
                      AND account_sid=%s AND sender=%s""",
                    (self.practice_id, case_id, channel, contact_snapshot_id, account_sid, sender)).fetchone()
            self._audit(db, "communications_bind", case_id)
            return {"bindingId": str(row["binding_id"]), "caseId": case_id, "channel": channel}

    def _suppressed(self, db, binding):
        existing = db.execute("""SELECT 1 FROM chase_communication_suppressions
            WHERE practice_id=%s AND account_sid=%s AND sender=%s AND phone=%s AND channel=%s""",
            (self.practice_id, binding["account_sid"], binding["sender"], binding["phone"],
             binding["channel"])).fetchone() is not None
        if existing or not self.purpose_consent_enabled:
            return existing
        # A practice changing its vendor account/sending number cannot override
        # the recipient's channel opt-out. Event attribution stays route-bound.
        if db.execute("""SELECT 1 FROM chase_communication_suppressions
            WHERE practice_id=%s AND phone=%s AND channel=%s LIMIT 1""",
            (self.practice_id, binding["phone"], binding["channel"])).fetchone():
            return True
        return db.execute("""SELECT 1 FROM chase_communication_provider_events
            WHERE practice_id=%s AND channel=%s AND
              ((kind='sms_inbound' AND sender=%s AND disposition='sms_opt_out') OR
               (kind='voice_gather' AND recipient=%s AND disposition='stop_calls')) LIMIT 1""",
            (self.practice_id, binding["channel"], binding["phone"], binding["phone"])).fetchone() is not None

    def prepare(self, *, case_id, binding_id, operation_id, channel, text,
                actor_tenant_id, actor_id, eligibility):
        """Durably reserve an idempotency key for review. Always prepared/not sent.

        Server eligibility remains necessary but is not a dispatch permit. The
        SMS preparation type makes no quiet-hours assertion. Voice purpose
        consent cannot be inferred from the legacy PHONE contact-route consent.
        """
        sms_draft = type(eligibility) is SmsPreparationEligibility
        decision_type = SmsPreparationEligibility if sms_draft else Eligibility
        if ((sms_draft and (channel != "sms" or not self.purpose_consent_enabled))
                or not isinstance(eligibility, decision_type)
                or any(getattr(eligibility, f.name) is not True for f in fields(decision_type))):
            raise CommunicationsError(403, "communications_policy_blocked")
        operation_id, binding_id = _uuid(operation_id), _uuid(binding_id)
        self.require_channel(channel)
        _text(text)
        with self._db() as db:
            tenant, actor = self._staff(db, actor_tenant_id, actor_id)
            binding = self._binding(db, case_id, binding_id, channel)
            body_hash = _hash([case_id, binding_id, channel, text, tenant, actor])
            prior = db.execute("""SELECT * FROM chase_communication_operations
                WHERE practice_id=%s AND operation_id=%s""", (self.practice_id, operation_id)).fetchone()
            if prior:
                if prior["body_hash"] != body_hash:
                    raise CommunicationsError(409, "communications_idempotency_conflict")
                self._audit(db, "communications_prepare", case_id, operation_id)
                return _operation(prior)
            item = self._case(db, case_id)
            if self.purpose_consent_enabled:
                db.execute("SELECT 1 FROM chase_items WHERE practice_id=%s AND chase_id=%s FOR UPDATE",
                           (self.practice_id, case_id))
                _assert_no_unknown_dispatch(db, self.practice_id, case_id)
            direct = item["direct_contract"]
            if (item["state"] in {"verified_closed", "resolved_other", "unreachable"}
                    or not isinstance(direct, dict) or direct.get("is_direct_contract") is not False):
                raise CommunicationsError(403, "communications_policy_blocked")
            route = self._snapshot(db, case_id, str(binding["contact_snapshot_id"]), channel)
            consent_valid = route["consent_status"] == "granted"
            if self.purpose_consent_enabled and route["consent_status"] in {"unknown", "not_required"}:
                purpose = db.execute("""SELECT granted FROM chase_communication_consent_decisions
                    WHERE practice_id=%s AND binding_id=%s ORDER BY sequence_no DESC LIMIT 1""",
                    (self.practice_id, binding_id)).fetchone()
                consent_valid = bool(purpose and purpose["granted"] is True)
            if not consent_valid:
                raise CommunicationsError(403, "communications_consent_unavailable")
            if self._suppressed(db, binding):
                raise CommunicationsError(409, "communications_contact_suppressed")
            row = db.execute("""INSERT INTO chase_communication_operations
                (practice_id,operation_id,case_id,binding_id,channel,text,actor_tenant_id,actor_id,body_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (self.practice_id, operation_id, case_id, binding_id, channel, text, tenant, actor, body_hash)).fetchone()
            self._append(db, event_id="prepared:" + operation_id, account_sid=binding["account_sid"],
                provider_sid=None, case_id=case_id, operation_id=operation_id, sender=binding["sender"],
                recipient=binding["phone"], channel=channel, kind="outbound_prepared", direction="outbound",
                text=text, status="prepared", disposition=None, actor_tenant_id=tenant, actor_id=actor)
            self._audit(db, "communications_prepare", case_id, operation_id)
            return _operation(row)

    def _append(self, db, *, event_id, account_sid, provider_sid, case_id, operation_id,
                sender, recipient, channel, kind, direction, text, status, disposition,
                actor_tenant_id=None, actor_id=None, occurred_at=None):
        _check(event_id)
        canonical_time = _event_time(occurred_at) if occurred_at is not None else None
        # Include attribution and case identity so a replay cannot rebind an event.
        body_hash = _hash([provider_sid, case_id, operation_id, sender, recipient, channel, kind,
                           direction, text, status, disposition, actor_tenant_id, actor_id, canonical_time])
        prior = db.execute("""SELECT * FROM chase_communication_events
            WHERE practice_id=%s AND account_sid=%s AND event_id=%s""",
                           (self.practice_id, account_sid, event_id)).fetchone()
        if prior:
            if prior["body_hash"] != body_hash:
                raise CommunicationsError(409, "communications_event_conflict")
            return prior
        return db.execute("""INSERT INTO chase_communication_events
            (practice_id,event_id,account_sid,provider_sid,case_id,operation_id,sender,recipient,
             channel,kind,direction,text,status,disposition,actor_tenant_id,actor_id,occurred_at,body_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (self.practice_id, event_id, account_sid, provider_sid, case_id, operation_id, sender,
             recipient, channel, kind, direction, text, status, disposition, actor_tenant_id, actor_id,
             canonical_time or datetime.now(timezone.utc), body_hash)).fetchone()

    def _suppress(self, db, *, account_sid, sender, phone, channel, event_id):
        db.execute("""INSERT INTO chase_communication_suppressions
            (practice_id,account_sid,sender,phone,channel,event_id) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING""", (self.practice_id, account_sid, sender, phone, channel, event_id))

    def receive_sms(self, *, account_sid, provider_sid, sender, recipient, text, event_id, occurred_at=None):
        """Persist signature-verified inbound SMS; ambiguous cases are held, not guessed."""
        _check(account_sid, _ACCOUNT)
        _check(provider_sid, _SID)
        if not provider_sid.startswith(("SM", "MM")):
            raise CommunicationsError(400, "invalid_sms_provider_id")
        _check(sender, _PHONE)
        _check(recipient, _PHONE)
        _check(event_id)
        _text(text)
        if occurred_at is not None:
            _event_time(occurred_at)
        word = text.strip().upper()
        disposition = ("sms_opt_out" if word in _STOP else "sms_opt_in_review" if word in _START
                       else "help_requested" if word in {"HELP", "INFO"} else "reply_received")
        with self._db() as db:
            known = db.execute("""SELECT DISTINCT case_id FROM chase_communication_bindings
                WHERE practice_id=%s AND account_sid=%s AND sender=%s AND phone=%s AND channel='sms'""",
                (self.practice_id, account_sid, recipient, sender)).fetchall()
            if not known:
                raise CommunicationsError(403, "communications_callback_binding_mismatch")
            # A historical phone is sufficient to honor STOP, not to attribute a
            # new reply to a patient after the verified contact snapshot changed.
            matches = db.execute("""SELECT DISTINCT b.case_id FROM chase_communication_bindings b
                WHERE b.practice_id=%s AND b.account_sid=%s AND b.sender=%s AND b.phone=%s
                  AND b.channel='sms' AND b.contact_snapshot_id=(
                    SELECT s.contact_snapshot_id FROM chase_contact_snapshots s
                    WHERE s.practice_id=b.practice_id AND s.chase_id=b.case_id
                    ORDER BY s.verified_at DESC,s.created_at DESC,s.contact_snapshot_id DESC LIMIT 1)""",
                (self.practice_id, account_sid, recipient, sender)).fetchall()
            prior = db.execute("""SELECT * FROM chase_communication_events
                WHERE practice_id=%s AND account_sid=%s AND provider_sid=%s AND kind='inbound_sms'""",
                (self.practice_id, account_sid, provider_sid)).fetchone()
            if prior and (prior["sender"], prior["recipient"], prior["text"]) != (sender, recipient, text):
                raise CommunicationsError(409, "communications_provider_message_conflict")
            if prior:
                # Retries keep original attribution even after another matching
                # case is bound. A changed event payload under the same ID fails.
                same_id = db.execute("""SELECT 1 FROM chase_communication_events
                    WHERE practice_id=%s AND account_sid=%s AND event_id=%s""",
                    (self.practice_id, account_sid, event_id)).fetchone()
                if same_id:
                    self._append(db, event_id=event_id, account_sid=account_sid, provider_sid=provider_sid,
                        case_id=prior["case_id"], operation_id=None, sender=sender, recipient=recipient,
                        channel="sms", kind="inbound_sms", direction="inbound", text=text,
                        status="received", disposition=disposition, occurred_at=occurred_at)
                return _event(prior)
            row = self._append(db, event_id=event_id, account_sid=account_sid, provider_sid=provider_sid,
                case_id=matches[0]["case_id"] if len(matches) == 1 else None, operation_id=None,
                sender=sender, recipient=recipient, channel="sms", kind="inbound_sms", direction="inbound",
                text=text, status="received", disposition=disposition, occurred_at=occurred_at)
            if disposition == "sms_opt_out":
                self._suppress(db, account_sid=account_sid, sender=recipient, phone=sender,
                               channel="sms", event_id=row["event_id"])
            # START requests staff review; it never removes or overrides suppression.
            return _event(row)

    def suppress(self, *, case_id, binding_id, channel, event_id, actor_tenant_id, actor_id):
        """Staff records a received opt-out; no clinical/contact data is rewritten."""
        with self._db() as db:
            tenant, actor = self._staff(db, actor_tenant_id, actor_id)
            binding = self._binding(db, case_id, binding_id, channel)
            row = self._append(db, event_id=event_id, account_sid=binding["account_sid"],
                provider_sid=None, case_id=case_id, operation_id=None, sender=binding["sender"],
                recipient=binding["phone"], channel=channel, kind="staff_suppression", direction="staff",
                text="", status="suppressed", disposition="sms_opt_out" if channel == "sms" else "stop_calls",
                actor_tenant_id=tenant, actor_id=actor)
            self._suppress(db, account_sid=binding["account_sid"], sender=binding["sender"],
                           phone=binding["phone"], channel=channel, event_id=row["event_id"])
            return _event(row)

    def receive_status(self, **_event):
        raise CommunicationsError(409, "communications_dispatch_not_implemented")

    def receive_dtmf(self, **_event):
        raise CommunicationsError(409, "communications_dispatch_not_implemented")

    def case_view(self, case_id):
        """Bounded chronology; raw contact routes are never projected into a worklist."""
        with self._db() as db:
            self._case(db, case_id)
            # Preserve unresolved starts inside the bounded view even when many
            # earlier drafts were prepared before dispatch became uncertain.
            priority = """EXISTS (SELECT 1 FROM chase_communication_dispatch_starts d
                LEFT JOIN chase_communication_dispatch_receipts r USING(practice_id,operation_id)
                LEFT JOIN chase_communication_unknown_resolutions u USING(practice_id,operation_id)
                WHERE d.practice_id=o.practice_id AND d.operation_id=o.operation_id
                  AND u.operation_id IS NULL AND (r.operation_id IS NULL OR r.outcome='unknown')) DESC,""" if self.purpose_consent_enabled else ""
            operations = db.execute("""SELECT o.* FROM chase_communication_operations o
                WHERE practice_id=%s AND case_id=%s ORDER BY """ + priority + "created_at DESC,operation_id DESC LIMIT 201",
                (self.practice_id, case_id)).fetchall()
            events = db.execute("""SELECT * FROM chase_communication_events
                WHERE practice_id=%s AND case_id=%s ORDER BY sequence_no DESC LIMIT 201""",
                (self.practice_id, case_id)).fetchall()
            bindings = db.execute("""SELECT DISTINCT ON (channel) * FROM chase_communication_bindings
                WHERE practice_id=%s AND case_id=%s ORDER BY channel,created_at DESC,binding_id DESC""",
                (self.practice_id, case_id)).fetchall()
            suppressed = {b["channel"] for b in bindings if self._suppressed(db, b)}
            held = db.execute("""SELECT count(*) AS count FROM chase_communication_events e
                WHERE e.practice_id=%s AND e.case_id IS NULL AND EXISTS
                (SELECT 1 FROM chase_communication_bindings b WHERE b.practice_id=e.practice_id
                 AND b.case_id=%s AND b.channel=e.channel AND b.account_sid=e.account_sid
                 AND b.sender=e.recipient AND b.phone=e.sender)""", (self.practice_id, case_id)).fetchone()["count"]
            return {"mode": "prepared", "operations": [_operation(r) for r in sorted(operations[:200], key=lambda r: (r["created_at"], str(r["operation_id"])))],
                    "events": [_event(r) for r in reversed(events[:200])],
                    "suppression": {"sms": "sms" in suppressed, "automatedVoice": "automated_voice" in suppressed},
                    "heldInboundCount": held, "truncated": len(operations) > 200 or len(events) > 200}
