"""Isolated temporary SQLite ledger for synthetic SMS and scripted-call demos.

The host supplies authorization and consent from server-owned policy at action
time. It verifies provider signatures before invoking receivers. This module
never imports the chase scheduler, increments attempts, or performs network I/O.
An injected submitter is a future transport seam, not an activation mechanism.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from uuid import UUID


class CommunicationsError(ValueError):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass(frozen=True)
class Eligibility:
    """Server-created decisions; never deserialize these flags from a request."""
    role_allowed: bool = False
    case_open: bool = False
    direct_contract_clear: bool = False
    consent_valid: bool = False
    practice_enabled: bool = False
    quiet_hours_clear: bool = False
    touch_limit_clear: bool = False


_PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_ACCOUNT = re.compile(r"AC[0-9a-fA-F]{32}\Z")
_SID = re.compile(r"(?:SM|MM|CA)[0-9a-fA-F]{32}\Z")
_STATUS = {
    "sms": {"accepted": 0, "queued": 1, "sending": 2, "sent": 3,
            "failed": 4, "undelivered": 4, "canceled": 4, "delivered": 5},
    "automated_voice": {"accepted": 0, "queued": 1, "initiated": 1,
                        "ringing": 2, "in_progress": 3, "busy": 4,
                        "no_answer": 4, "failed": 4, "canceled": 4, "completed": 5},
}
_STOP = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "REVOKE", "OPTOUT"}
_START = {"START", "UNSTOP", "YES"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
 practice_id TEXT NOT NULL, case_id TEXT NOT NULL, patient_ref TEXT NOT NULL,
 phone TEXT NOT NULL, sender TEXT NOT NULL, account_sid TEXT NOT NULL,
 PRIMARY KEY(practice_id,case_id));
CREATE TABLE IF NOT EXISTS operations (
 practice_id TEXT NOT NULL, operation_id TEXT NOT NULL, case_id TEXT NOT NULL,
 patient_ref TEXT NOT NULL, account_sid TEXT NOT NULL, sender TEXT NOT NULL,
 phone TEXT NOT NULL, channel TEXT NOT NULL, text TEXT NOT NULL, actor_id TEXT NOT NULL,
 body_hash TEXT NOT NULL, status TEXT NOT NULL, provider_sid TEXT,
 simulated INTEGER NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(practice_id,operation_id), UNIQUE(account_sid,provider_sid));
CREATE TABLE IF NOT EXISTS events (
 event_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, account_sid TEXT NOT NULL,
 provider_sid TEXT NOT NULL, practice_id TEXT, case_id TEXT, operation_id TEXT,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, channel TEXT NOT NULL,
 kind TEXT NOT NULL, direction TEXT NOT NULL, text TEXT NOT NULL,
 status TEXT, disposition TEXT, needs_review INTEGER NOT NULL,
 occurred_at TEXT NOT NULL, received_at TEXT NOT NULL, body_hash TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'events_are_append_only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'events_are_append_only'); END;
CREATE TABLE IF NOT EXISTS suppressions (
 account_sid TEXT NOT NULL, sender TEXT NOT NULL, phone TEXT NOT NULL,
 channel TEXT NOT NULL, event_key TEXT NOT NULL,
 PRIMARY KEY(account_sid,sender,phone,channel));
"""


def _check(value, pattern=_TOKEN):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CommunicationsError(400, "invalid_communications_value")
    return value


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 1600
            or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in value)):
        raise CommunicationsError(400, "invalid_message_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise CommunicationsError(400, "invalid_message_text") from None
    return value


def _when(value=None):
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise CommunicationsError(400, "invalid_event_time") from None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _operation(row):
    return {"operationId": row["operation_id"], "caseId": row["case_id"],
            "patientRef": row["patient_ref"], "channel": row["channel"],
            "direction": "outbound", "text": row["text"], "status": row["status"],
            "providerSid": row["provider_sid"], "simulated": bool(row["simulated"]),
            "createdAt": row["created_at"],
            "needsStaffReview": row["status"] in {"submission_unknown", "submission_pending"},
            "actorId": row["actor_id"], "fromNumber": row["sender"], "toNumber": row["phone"]}


def _event(row):
    return {"eventId": row["event_id"], "operationId": row["operation_id"],
            "providerSid": row["provider_sid"], "kind": row["kind"],
            "direction": row["direction"], "channel": row["channel"],
            "text": row["text"], "status": row["status"], "disposition": row["disposition"],
            "occurredAt": row["occurred_at"], "receivedAt": row["received_at"],
            "needsStaffReview": bool(row["needs_review"]), "held": row["case_id"] is None}


class CommunicationsStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()}
        if not any(self.path.is_relative_to(root) for root in roots):
            raise CommunicationsError(400, "temporary_store_required")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _binding(db, practice_id, case_id):
        row = db.execute("SELECT * FROM bindings WHERE practice_id=? AND case_id=?",
                         (_check(practice_id), _check(case_id))).fetchone()
        if row is None:
            raise CommunicationsError(404, "communications_case_not_bound")
        return row

    def bind_case(self, practice_id, case_id, patient_ref, phone, sender, account_sid):
        values = (_check(practice_id), _check(case_id), _check(patient_ref),
                  _check(phone, _PHONE), _check(sender, _PHONE), _check(account_sid, _ACCOUNT))
        with self._db() as db:
            row = db.execute("SELECT * FROM bindings WHERE practice_id=? AND case_id=?", values[:2]).fetchone()
            if row is not None and tuple(row) != values:
                raise CommunicationsError(409, "communications_binding_conflict")
            db.execute("INSERT OR IGNORE INTO bindings VALUES (?,?,?,?,?,?)", values)

    def send(self, *, practice_id, case_id, operation_id, channel, text, actor_id,
             eligibility, submit=None):
        if (not isinstance(eligibility, Eligibility)
                or any(getattr(eligibility, f.name) is not True for f in fields(Eligibility))):
            raise CommunicationsError(403, "communications_policy_blocked")
        try:
            if not isinstance(operation_id, str):
                raise ValueError()
            operation_id = str(UUID(operation_id))
        except (ValueError, AttributeError):
            raise CommunicationsError(400, "invalid_operation_id") from None
        if not isinstance(channel, str) or channel not in _STATUS:
            raise CommunicationsError(400, "invalid_communications_channel")
        _text(text)
        _check(actor_id)
        with self._db() as db:
            binding = self._binding(db, practice_id, case_id)
            body_hash = _hash([dict(binding), channel, text, actor_id])
            prior = db.execute("SELECT * FROM operations WHERE practice_id=? AND operation_id=?",
                               (practice_id, operation_id)).fetchone()
            if prior:
                if prior["body_hash"] != body_hash:
                    raise CommunicationsError(409, "communications_idempotency_conflict")
                return _operation(prior)
            route = (binding["account_sid"], binding["sender"], binding["phone"], channel)
            if db.execute("SELECT 1 FROM suppressions WHERE account_sid=? AND sender=? AND phone=? AND channel=?", route).fetchone():
                raise CommunicationsError(409, "communications_contact_suppressed")
            simulated = submit is None
            local_id = _hash([practice_id, operation_id])[:32]
            sid = ("SM" if channel == "sms" else "CA") + local_id if simulated else None
            if sid and db.execute("SELECT 1 FROM events WHERE account_sid=? AND provider_sid=? AND kind='inbound_sms'",
                                  (binding["account_sid"], sid)).fetchone():
                raise CommunicationsError(409, "communications_provider_id_conflict")
            status = "simulated" if simulated else "submission_pending"
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (practice_id, operation_id, case_id, binding["patient_ref"], *route[:3],
                        channel, text, actor_id, body_hash, status, sid, int(simulated), _when()))
            row = db.execute("SELECT * FROM operations WHERE practice_id=? AND operation_id=?",
                             (practice_id, operation_id)).fetchone()
            self._append(db, event_id="local:" + local_id, account_sid=binding["account_sid"],
                         provider_sid=sid or "pending", binding=binding, operation_id=operation_id,
                         sender=binding["sender"], recipient=binding["phone"], channel=channel,
                         kind="outbound_created", direction="outbound", text=text, status=status,
                         disposition=None, needs_review=False, occurred_at=None)
            operation = _operation(row)
        if simulated:
            return operation
        # Reservation commits before transport. An ambiguous/exceptional submission is
        # never re-sent on replay, even after restart; staff reconciliation is required.
        with self._db() as db:
            # Recheck after reservation and serialize submit against received opt-outs.
            # Any future transport must have its own bounded request timeout.
            suppressed = db.execute("SELECT 1 FROM suppressions WHERE account_sid=? AND sender=? AND phone=? AND channel=?", route).fetchone()
            if suppressed:
                sid, status = None, "suppressed_before_submission"
            else:
                try:
                    result = submit({**operation, "accountSid": binding["account_sid"]})
                    get = result.get if isinstance(result, dict) else lambda key: getattr(result, key, None)
                    outcome, sid, status = get("outcome"), get("provider_id"), get("provider_status")
                    status = status.replace("-", "_") if isinstance(status, str) else "accepted"
                    valid = (outcome == "accepted" and isinstance(sid, str) and _SID.fullmatch(sid)
                             and sid.startswith("CA" if channel == "automated_voice" else ("SM", "MM"))
                             and status in _STATUS[channel])
                    if not valid:
                        sid, status = None, "submission_rejected" if outcome == "rejected" else "submission_unknown"
                except Exception:
                    sid, status = None, "submission_unknown"
            if sid and (db.execute("SELECT 1 FROM operations WHERE account_sid=? AND provider_sid=?",
                                   (binding["account_sid"], sid)).fetchone()
                        or db.execute("SELECT 1 FROM events WHERE account_sid=? AND provider_sid=? AND kind='inbound_sms'",
                                      (binding["account_sid"], sid)).fetchone()):
                sid, status = None, "submission_unknown"
            db.execute("UPDATE operations SET provider_sid=?,status=? WHERE practice_id=? AND operation_id=?",
                       (sid, status, practice_id, operation_id))
            self._append(db, event_id="submission:" + local_id, account_sid=binding["account_sid"],
                         provider_sid=sid or "unknown", binding=binding, operation_id=operation_id,
                         sender=binding["sender"], recipient=binding["phone"], channel=channel,
                         kind="submission", direction="outbound", text="", status=status,
                         disposition=None, needs_review=status == "submission_unknown", occurred_at=None)
            return _operation(db.execute("SELECT * FROM operations WHERE practice_id=? AND operation_id=?",
                                         (practice_id, operation_id)).fetchone())

    def _append(self, db, *, event_id, account_sid, provider_sid, binding, operation_id,
                sender, recipient, channel, kind, direction, text, status, disposition,
                needs_review, occurred_at):
        event_id = _check(event_id)
        canonical_time = _when(occurred_at) if occurred_at is not None else None
        body_hash = _hash([provider_sid, sender, recipient, channel, kind, direction,
                           text, status, disposition, canonical_time])
        event_key = account_sid + ":" + event_id
        prior = db.execute("SELECT * FROM events WHERE event_key=?", (event_key,)).fetchone()
        if prior:
            if prior["body_hash"] != body_hash:
                raise CommunicationsError(409, "communications_event_conflict")
            return prior
        now = _when()
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (event_key, event_id, account_sid, provider_sid,
                    binding["practice_id"] if binding else None, binding["case_id"] if binding else None,
                    operation_id, sender, recipient, channel, kind, direction, text, status,
                    disposition, int(needs_review), canonical_time or now, now, body_hash))
        return db.execute("SELECT * FROM events WHERE event_key=?", (event_key,)).fetchone()

    def receive_sms(self, *, account_sid, provider_sid, sender, recipient, text, event_id, occurred_at=None):
        _check(account_sid, _ACCOUNT)
        _check(provider_sid, _SID)
        if not provider_sid.startswith(("SM", "MM")):
            raise CommunicationsError(400, "invalid_sms_provider_id")
        _check(sender, _PHONE)
        _check(recipient, _PHONE)
        _text(text)
        word = text.strip().upper()
        disposition = ("sms_opt_out" if word in _STOP else "sms_opt_in_review" if word in _START
                       else "help_requested" if word in {"HELP", "INFO"} else "reply_received")
        with self._db() as db:
            matches = db.execute("SELECT * FROM bindings WHERE account_sid=? AND phone=? AND sender=?",
                                 (account_sid, sender, recipient)).fetchall()
            if not matches or db.execute("SELECT 1 FROM operations WHERE account_sid=? AND provider_sid=?",
                                         (account_sid, provider_sid)).fetchone():
                raise CommunicationsError(403, "communications_callback_binding_mismatch")
            # One provider message cannot acquire a different route/body by using a
            # new event ID. Status events for outbound SIDs use a different receiver.
            prior = db.execute("SELECT * FROM events WHERE account_sid=? AND provider_sid=? AND kind='inbound_sms'",
                               (account_sid, provider_sid)).fetchone()
            if prior and (prior["sender"], prior["recipient"], prior["text"]) != (sender, recipient, text):
                raise CommunicationsError(409, "communications_provider_message_conflict")
            if prior:
                _check(event_id)
                if occurred_at is not None:
                    _when(occurred_at)
                same_event = db.execute("SELECT * FROM events WHERE event_key=?", (account_sid + ":" + event_id,)).fetchone()
                if same_event:
                    self._append(db, event_id=event_id, account_sid=account_sid, provider_sid=provider_sid,
                                 binding=None, operation_id=None, sender=sender, recipient=recipient,
                                 channel="sms", kind="inbound_sms", direction="inbound", text=text,
                                 status="received", disposition=disposition, needs_review=True, occurred_at=occurred_at)
                return _event(prior)
            row = self._append(db, event_id=event_id, account_sid=account_sid, provider_sid=provider_sid,
                               binding=matches[0] if len(matches) == 1 else None, operation_id=None,
                               sender=sender, recipient=recipient, channel="sms", kind="inbound_sms",
                               direction="inbound", text=text, status="received", disposition=disposition,
                               needs_review=True, occurred_at=occurred_at)
            if disposition == "sms_opt_out":
                db.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?,?,?,?)",
                           (account_sid, recipient, sender, "sms", row["event_key"]))
            # START is an auditable request for staff review, never consent authority.
            return _event(row)

    @staticmethod
    def _callback_operation(db, account_sid, provider_sid, sender, recipient):
        row = db.execute("SELECT * FROM operations WHERE account_sid=? AND provider_sid=? AND sender=? AND phone=?",
                         (_check(account_sid, _ACCOUNT), _check(provider_sid, _SID),
                          _check(sender, _PHONE), _check(recipient, _PHONE))).fetchone()
        if row is None:
            raise CommunicationsError(403, "communications_callback_binding_mismatch")
        return row

    def receive_status(self, *, account_sid, provider_sid, sender, recipient, status, event_id, occurred_at=None):
        if not isinstance(status, str):
            raise CommunicationsError(400, "invalid_provider_status")
        status = status.replace("-", "_")
        with self._db() as db:
            op = self._callback_operation(db, account_sid, provider_sid, sender, recipient)
            ranks = _STATUS[op["channel"]]
            if status not in ranks:
                raise CommunicationsError(400, "invalid_provider_status")
            event = self._append(db, event_id=event_id, account_sid=account_sid, provider_sid=provider_sid,
                                 binding=op, operation_id=op["operation_id"], sender=sender, recipient=recipient,
                                 channel=op["channel"], kind="status", direction="callback", text="",
                                 status=status, disposition=None, needs_review=False, occurred_at=occurred_at)
            if ranks[status] > ranks.get(op["status"], -1):
                db.execute("UPDATE operations SET status=? WHERE practice_id=? AND operation_id=?",
                           (status, op["practice_id"], op["operation_id"]))
            return _event(event)

    def receive_dtmf(self, *, account_sid, provider_sid, sender, recipient, digit, event_id, occurred_at=None):
        if not isinstance(digit, str) or digit not in {"1", "2", "9"}:
            raise CommunicationsError(400, "invalid_call_digit")
        with self._db() as db:
            op = self._callback_operation(db, account_sid, provider_sid, sender, recipient)
            if op["channel"] != "automated_voice":
                raise CommunicationsError(400, "callback_channel_mismatch")
            disposition = {"1": "callback_requested", "2": "reports_submitted", "9": "stop_calls"}[digit]
            row = self._append(db, event_id=event_id, account_sid=account_sid, provider_sid=provider_sid,
                               binding=op, operation_id=op["operation_id"], sender=sender, recipient=recipient,
                               channel="automated_voice", kind="dtmf", direction="inbound", text=digit,
                               status=None, disposition=disposition, needs_review=True, occurred_at=occurred_at)
            if digit == "9":
                db.execute("INSERT OR IGNORE INTO suppressions VALUES (?,?,?,?,?)",
                           (account_sid, sender, recipient, "automated_voice", row["event_key"]))
            return _event(row)

    def case_view(self, practice_id, case_id):
        with self._db() as db:
            binding = self._binding(db, practice_id, case_id)
            route = (binding["account_sid"], binding["sender"], binding["phone"])
            operations = db.execute("SELECT * FROM operations WHERE practice_id=? AND case_id=? ORDER BY created_at,operation_id",
                                    (practice_id, case_id)).fetchall()
            events = db.execute("SELECT * FROM events WHERE practice_id=? AND case_id=? ORDER BY received_at,rowid",
                                (practice_id, case_id)).fetchall()
            suppressed = {row[0] for row in db.execute("SELECT channel FROM suppressions WHERE account_sid=? AND sender=? AND phone=?", route)}
            held = db.execute("SELECT COUNT(*) FROM events WHERE case_id IS NULL AND account_sid=? AND recipient=? AND sender=?", route).fetchone()[0]
            return {"mode": "simulated" if all(op["simulated"] for op in operations) else "adapter",
                    "operations": [_operation(op) for op in operations], "events": [_event(e) for e in events],
                    "suppression": {"sms": "sms" in suppressed, "automatedVoice": "automated_voice" in suppressed},
                    "heldInboundCount": held}
