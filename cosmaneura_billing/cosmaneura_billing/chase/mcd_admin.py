"""Synthetic-only MCD insurance-card roster and staff phone-task ledger.

This additive boundary records work; it has no send or dial capability.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from ..direct_contract import DirectContractCheck, DirectContractList, check_direct_contract

ROSTER_SCHEMA = "cosmaneura.mcd_admin.card_chase.roster.v1"
SYNTHETIC_SOURCE = "synthetic_fixture"
_ROSTER_KEYS = {"schema", "synthetic", "source", "roster_id", "people"}
_PERSON_KEYS = {
    "synthetic", "person_ref", "display_label", "phone", "outreach_reason",
    "due_at", "voice_consent", "requested_channel",
}
_ROSTER_ID = re.compile(r"SYN-MCD-ROSTER-[A-Z0-9][A-Z0-9-]{0,63}\Z")
_PERSON_REF = re.compile(r"SYN-MCD-[A-Z0-9][A-Z0-9-]{0,63}\Z")
_LABEL = re.compile(r"Synthetic (?:Person|Patient) [A-Za-z0-9-]{1,32}\Z")
# NANP's fictional-use range is 555-0100 through 555-0199.
_PHONE = re.compile(r"\+1[2-9][0-9]{2}55501[0-9]{2}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STAFF = re.compile(r"SYN-STAFF-[A-Z0-9][A-Z0-9-]{0,63}\Z")


class UnsafeMcdAdminConfiguration(ValueError):
    pass


class SyntheticRosterRejected(ValueError):
    pass


class IdempotencyConflict(ValueError):
    pass


class TaskStateConflict(ValueError):
    pass


def _env_bool(env: Mapping[str, str], name: str) -> bool:
    raw = (env.get(name) or "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise UnsafeMcdAdminConfiguration(f"{name} must be a boolean")


@dataclass(frozen=True)
class McdAdminChaseConfig:
    """Fail-closed runtime posture for the local synthetic slice."""

    chase_transport: str = "record_only"
    chase_scheduler_enabled: bool = False
    patient_messaging_enabled: bool = False
    patient_messaging_operationally_activated: bool = False
    cab_deploy_enabled: bool = False
    athena_mode: str = "disabled"

    def __post_init__(self) -> None:
        unsafe = {
            "transport": self.chase_transport != "record_only",
            "scheduler": self.chase_scheduler_enabled,
            "patient messaging": self.patient_messaging_enabled,
            "patient messaging activation": self.patient_messaging_operationally_activated,
            "CAB deployment": self.cab_deploy_enabled,
            "Athena live mode": self.athena_mode == "live",
        }
        if reason := next((name for name, active in unsafe.items() if active), ""):
            raise UnsafeMcdAdminConfiguration(f"{reason} must remain disabled")
        if self.athena_mode not in {"disabled", "mock"}:
            raise UnsafeMcdAdminConfiguration("Athena mode must be disabled or mock")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "McdAdminChaseConfig":
        env = os.environ if env is None else env
        return cls(
            chase_transport=(env.get("COSMANEURA_CHASE_TRANSPORT") or "record_only")
            .strip().lower(),
            chase_scheduler_enabled=_env_bool(env, "COSMANEURA_CHASE_SCHEDULER_ENABLED"),
            patient_messaging_enabled=_env_bool(env, "COSMANEURA_PATIENT_MESSAGING_ENABLED"),
            patient_messaging_operationally_activated=_env_bool(
                env, "COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED"
            ),
            cab_deploy_enabled=_env_bool(env, "CAB_DEPLOY_ENABLED"),
            athena_mode=(env.get("COSMANEURA_ATHENA_MODE") or "disabled")
            .strip().lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.chase_transport,
            "scheduler_enabled": self.chase_scheduler_enabled,
            "patient_messaging_enabled": self.patient_messaging_enabled,
            "patient_messaging_operationally_activated": (
                self.patient_messaging_operationally_activated
            ),
            "cab_deploy_enabled": self.cab_deploy_enabled,
            "athena_mode": self.athena_mode,
        }


class OutreachReason(str, Enum):
    MISSING_CARD = "missing_insurance_card"
    EXPIRED_CARD = "expired_insurance_card"
    COVERAGE_ISSUE = "coverage_issue"


class PhoneTaskStatus(str, Enum):
    OPEN = "open"
    COMPLETED = "completed"


class PhoneOutcome(str, Enum):
    REACHED = "reached"
    NO_ANSWER = "no_answer"
    LEFT_MESSAGE = "left_message"
    WRONG_NUMBER = "wrong_number"
    DECLINED = "declined"


_PRIORITY = {
    OutreachReason.MISSING_CARD: 80,
    OutreachReason.EXPIRED_CARD: 90,
    OutreachReason.COVERAGE_ISSUE: 70,
}


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SyntheticRosterRejected(f"{field} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise SyntheticRosterRejected(f"{field} must be an ISO 8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SyntheticRosterRejected(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SyntheticRosterRejected(f"{field} must be non-empty")
    value = value.strip()
    if len(value) > 128 or any(ord(char) < 32 for char in value):
        raise SyntheticRosterRejected(f"{field} is invalid")
    return value


@dataclass(frozen=True)
class SyntheticRosterPerson:
    person_ref: str
    display_label: str
    phone: str
    outreach_reason: OutreachReason
    due_at: str
    voice_consent: bool

    @classmethod
    def parse(cls, row: Any, index: int) -> "SyntheticRosterPerson":
        if not isinstance(row, Mapping) or set(row) != _PERSON_KEYS:
            raise SyntheticRosterRejected(f"people[{index}] fields are invalid")
        if row.get("synthetic") is not True:
            raise SyntheticRosterRejected(f"people[{index}] must be synthetic")
        if row.get("requested_channel") != "phone":
            raise SyntheticRosterRejected(f"people[{index}] channel must be phone")
        if row.get("voice_consent") is not True:
            raise SyntheticRosterRejected(f"people[{index}] requires voice consent")
        ref = _text(row, "person_ref")
        label = _text(row, "display_label")
        phone = _text(row, "phone")
        if not _PERSON_REF.fullmatch(ref):
            raise SyntheticRosterRejected(f"people[{index}] ref must use SYN-MCD")
        if not _LABEL.fullmatch(label):
            raise SyntheticRosterRejected(f"people[{index}] label must be synthetic")
        if not _PHONE.fullmatch(phone):
            raise SyntheticRosterRejected(f"people[{index}] phone must use 555-01xx")
        try:
            reason = OutreachReason(row.get("outreach_reason"))
        except (TypeError, ValueError):
            raise SyntheticRosterRejected(f"people[{index}] reason is unsupported") from None
        return cls(
            ref, label, phone, reason, _timestamp(row.get("due_at"), "due_at"), True
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "synthetic": True, "person_ref": self.person_ref,
            "display_label": self.display_label, "phone": self.phone,
            "outreach_reason": self.outreach_reason.value, "due_at": self.due_at,
            "voice_consent": self.voice_consent, "requested_channel": "phone",
        }


@dataclass(frozen=True)
class SyntheticRoster:
    roster_id: str
    people: tuple[SyntheticRosterPerson, ...]

    @classmethod
    def parse(cls, document: str | bytes | Mapping[str, Any]) -> "SyntheticRoster":
        if isinstance(document, bytes):
            try:
                document = document.decode()
            except UnicodeDecodeError:
                raise SyntheticRosterRejected("roster must be UTF-8") from None
        if isinstance(document, str):
            try:
                document = json.loads(document)
            except json.JSONDecodeError:
                raise SyntheticRosterRejected("roster must be valid JSON") from None
        if not isinstance(document, Mapping) or set(document) != _ROSTER_KEYS:
            raise SyntheticRosterRejected("roster envelope fields are invalid")
        if document.get("schema") != ROSTER_SCHEMA:
            raise SyntheticRosterRejected("roster schema is unsupported")
        if document.get("synthetic") is not True or document.get("source") != SYNTHETIC_SOURCE:
            raise SyntheticRosterRejected("roster must be an explicit synthetic fixture")
        roster_id, rows = document.get("roster_id"), document.get("people")
        if not isinstance(roster_id, str) or not _ROSTER_ID.fullmatch(roster_id):
            raise SyntheticRosterRejected("roster_id must use SYN-MCD-ROSTER")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
            raise SyntheticRosterRejected("roster must contain 1 through 500 people")
        people = tuple(SyntheticRosterPerson.parse(row, i) for i, row in enumerate(rows))
        if len({person.person_ref for person in people}) != len(people):
            raise SyntheticRosterRejected("roster has duplicate person_ref values")
        return cls(roster_id, people)

    @property
    def payload_hash(self) -> str:
        payload = {
            "schema": ROSTER_SCHEMA, "synthetic": True, "source": SYNTHETIC_SOURCE,
            "roster_id": self.roster_id,
            "people": [person.to_dict() for person in self.people],
        }
        return _hash(payload)


@dataclass(frozen=True)
class PhoneAction:
    action_id: str
    actor_ref: str
    outcome: PhoneOutcome
    minutes_spent: float
    occurred_at: str


@dataclass(frozen=True)
class PhoneOutreachTask:
    task_id: str
    roster_id: str
    person: SyntheticRosterPerson
    created_at: str
    direct_contract: dict[str, Any]
    status: PhoneTaskStatus = PhoneTaskStatus.OPEN
    actions: tuple[PhoneAction, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "roster_id": self.roster_id,
            "task_type": "insurance_card_phone_outreach", "channel": "phone",
            "performed_by": "staff", "automated": False, "transport": "record_only",
            "status": self.status.value, "priority": _PRIORITY[self.person.outreach_reason],
            "created_at": self.created_at, "person": self.person.to_dict(),
            "direct_contract": dict(self.direct_contract),
            "actions": [
                {"action_id": action.action_id, "task_id": self.task_id,
                 "channel": "phone", "performed_by": "staff",
                 "automated_outbound_initiated": False,
                 "manual_outreach_recorded": True,
                 "actor_ref": action.actor_ref, "outcome": action.outcome.value,
                 "minutes_spent": action.minutes_spent, "occurred_at": action.occurred_at}
                for action in self.actions
            ],
        }


@dataclass(frozen=True)
class RosterIngestResult:
    roster_id: str
    task_ids: tuple[str, ...]
    created_count: int
    replayed: bool


@dataclass(frozen=True)
class PhoneActionResult:
    action: PhoneAction
    task: PhoneOutreachTask
    replayed: bool


def _hash(value: Mapping[str, Any]) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _task_id(roster_id: str, person: SyntheticRosterPerson) -> str:
    seed = f"mcd-phone.v1\0{roster_id}\0{person.person_ref}\0{person.outreach_reason.value}"
    return f"mcd-phone-{hashlib.sha256(seed.encode()).hexdigest()[:24]}"


class McdAdminChaseStore:
    """Queryable, thread-safe task/action ledger with idempotent writes."""

    def __init__(self) -> None:
        self._rosters: dict[str, str] = {}
        self._receipts: dict[str, tuple[str, str]] = {}
        self._tasks: dict[str, PhoneOutreachTask] = {}
        self._actions: dict[str, tuple[str, PhoneAction]] = {}
        self._lock = threading.RLock()

    def ingest(
        self, roster: SyntheticRoster, *, key: str, at: str,
        direct_contract_checks: Mapping[str, DirectContractCheck],
    ) -> RosterIngestResult:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("idempotency key is invalid")
        if set(direct_contract_checks) != {person.person_ref for person in roster.people}:
            raise SyntheticRosterRejected("direct-contract checks are incomplete")
        if any(check.blocked or check.patient_ref != ref
               for ref, check in direct_contract_checks.items()):
            raise SyntheticRosterRejected("direct-contract gate blocked roster ingest")
        ids = tuple(_task_id(roster.roster_id, person) for person in roster.people)
        with self._lock:
            if receipt := self._receipts.get(key):
                if receipt != (roster.payload_hash, roster.roster_id):
                    raise IdempotencyConflict("idempotency key content conflict")
                return RosterIngestResult(roster.roster_id, ids, 0, True)
            prior = self._rosters.get(roster.roster_id)
            if prior and prior != roster.payload_hash:
                raise IdempotencyConflict("roster_id content conflict")
            if not prior:
                self._tasks.update({
                    task_id: PhoneOutreachTask(
                        task_id, roster.roster_id, person, at,
                        direct_contract_checks[person.person_ref].to_dict(),
                    )
                    for task_id, person in zip(ids, roster.people)
                })
                self._rosters[roster.roster_id] = roster.payload_hash
            self._receipts[key] = (roster.payload_hash, roster.roster_id)
            return RosterIngestResult(
                roster.roster_id, ids, len(ids) if not prior else 0, bool(prior)
            )

    def get_task(self, task_id: str) -> PhoneOutreachTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(
        self, *, status: PhoneTaskStatus | str | None = None,
        roster_id: str = "", person_ref: str = "",
    ) -> list[PhoneOutreachTask]:
        try:
            wanted = PhoneTaskStatus(status) if status is not None else None
        except (TypeError, ValueError):
            raise ValueError("status must be open or completed") from None
        with self._lock:
            tasks = [
                task for task in self._tasks.values()
                if (wanted is None or task.status is wanted)
                and (not roster_id or task.roster_id == roster_id)
                and (not person_ref or task.person.person_ref == person_ref)
            ]
        return sorted(tasks, key=lambda task: (
            -_PRIORITY[task.person.outreach_reason], task.person.due_at, task.task_id,
        ))

    def counts(self) -> dict[str, int]:
        with self._lock:
            counts = {
                state.value: sum(task.status is state for task in self._tasks.values())
                for state in PhoneTaskStatus
            }
        return {**counts, "total": sum(counts.values())}

    def record_action(
        self, *, task_id: str, action_id: str, actor_ref: str,
        outcome: PhoneOutcome | str, minutes_spent: float, at: str,
    ) -> PhoneActionResult:
        if not isinstance(action_id, str) or not _KEY.fullmatch(action_id):
            raise ValueError("action_id is invalid")
        if not isinstance(actor_ref, str) or not _STAFF.fullmatch(actor_ref):
            raise SyntheticRosterRejected("actor_ref must use SYN-STAFF")
        try:
            outcome = PhoneOutcome(outcome)
        except (TypeError, ValueError):
            raise ValueError("phone outcome is unsupported") from None
        if (isinstance(minutes_spent, bool) or not isinstance(minutes_spent, (int, float))
                or not 0 <= float(minutes_spent) <= 480):
            raise ValueError("minutes_spent must be from 0 through 480")
        minutes = round(float(minutes_spent), 2)
        # The server timestamp is intentionally outside the idempotency
        # fingerprint: a network retry can arrive in a later clock second and
        # must still return the originally recorded action.
        fingerprint = _hash({"task": task_id, "actor": actor_ref,
                             "outcome": outcome.value, "minutes": minutes})
        with self._lock:
            if prior := self._actions.get(action_id):
                if prior[0] != fingerprint:
                    raise IdempotencyConflict("action_id content conflict")
                return PhoneActionResult(prior[1], self._tasks[task_id], True)
            task = self._tasks.get(task_id)
            if task is None:
                raise LookupError("phone task was not found")
            if task.status is not PhoneTaskStatus.OPEN:
                raise TaskStateConflict("phone task is already completed")
            if task.person.voice_consent is not True:
                raise TaskStateConflict("phone task no longer has recorded voice consent")
            action = PhoneAction(action_id, actor_ref, outcome, minutes, at)
            status = (PhoneTaskStatus.COMPLETED if outcome in {
                PhoneOutcome.REACHED, PhoneOutcome.DECLINED,
            } else PhoneTaskStatus.OPEN)
            task = replace(task, status=status,
                           actions=task.actions + (action,))
            self._tasks[task_id] = task
            self._actions[action_id] = (fingerprint, action)
            return PhoneActionResult(action, task, False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class McdAdminChaseService:
    """Application API suitable for later authenticated route wiring."""

    store: McdAdminChaseStore
    direct_contract_roster: DirectContractList = field(default_factory=lambda: (
        DirectContractList(refs=[], version="mcd-admin-synthetic-v1")
    ))
    config: McdAdminChaseConfig = McdAdminChaseConfig()
    now: Callable[[], str] = _utc_now

    def ingest_roster(
        self, document: str | bytes | Mapping[str, Any], *, key: str = "",
    ) -> RosterIngestResult:
        roster = SyntheticRoster.parse(document)
        at = _timestamp(self.now(), "ingest timestamp")
        checks = {
            person.person_ref: check_direct_contract(
                person.person_ref, roster=self.direct_contract_roster, now=lambda: at,
            )
            for person in roster.people
        }
        if any(check.blocked for check in checks.values()):
            raise SyntheticRosterRejected("direct-contract gate blocked roster ingest")
        return self.store.ingest(
            roster, key=key or roster.roster_id, at=at,
            direct_contract_checks=checks,
        )

    def list_phone_tasks(self, **filters: Any) -> list[PhoneOutreachTask]:
        return self.store.list_tasks(**filters)

    def get_phone_task(self, task_id: str) -> PhoneOutreachTask | None:
        return self.store.get_task(task_id)

    def record_phone_action(
        self, *, task_id: str, action_id: str, actor_ref: str,
        outcome: PhoneOutcome | str, minutes_spent: float,
    ) -> PhoneActionResult:
        return self.store.record_action(
            task_id=task_id, action_id=action_id, actor_ref=actor_ref,
            outcome=outcome, minutes_spent=minutes_spent,
            at=_timestamp(self.now(), "action timestamp"),
        )

    def worklist(self) -> dict[str, Any]:
        return {
            "mode": "synthetic_only", "safety": self.config.to_dict(),
            "counts": self.store.counts(),
            "tasks": [task.to_dict() for task in self.list_phone_tasks()],
        }


__all__ = [
    "IdempotencyConflict", "McdAdminChaseConfig", "McdAdminChaseService",
    "McdAdminChaseStore", "PhoneOutcome", "PhoneTaskStatus", "ROSTER_SCHEMA",
    "SyntheticRosterRejected", "TaskStateConflict", "UnsafeMcdAdminConfiguration",
]
