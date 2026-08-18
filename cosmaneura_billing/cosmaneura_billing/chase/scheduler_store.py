"""Durable PostgreSQL repository for the chase scheduler and delivery outbox.

The repository deliberately stops at transaction boundaries.  It reserves work,
leases one exact delivery, and records the result, but it never calls a transport.
In particular, an expired ``submitting`` lease is quarantined as ``ambiguous``
and can only be reconciled; it is never returned by :meth:`claim_delivery`.

There are two authority lanes in this module:

* detection, policy, release, and staff-task methods are authenticated API input
  transactions and check the named human actor inside the transaction;
* scheduling, reservation, claim, and finalization are the narrow worker effect
  lane and touch no identity table directly.

Callers must not give either lane owner credentials merely to make a method
available. The deployment provisions separate least-privilege roles.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Iterable, Iterator, Literal, Optional
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .models import ChaseItem, Channel, State, Trigger
from .policy import ChasePolicy, Step
from .policy_fingerprint import (
    DeliveryConfiguration,
    policy_fingerprint,
    policy_step_records,
)
from .scheduling import (
    DurableScheduleEvidence,
    ScheduleAction,
    ScheduleDecision,
    ScheduleReason,
    cadence_due_at,
    evaluate_step,
)


Instant = datetime | str
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_CONSENT = frozenset({"not_required", "granted", "denied", "unknown"})
_DIRECT_KNOWN_REVIEW_REASON = (
    "patient is on the direct-contract list — the practice does not bill "
    "their insurance, so confirm whether a card should be chased at all"
)
_DIRECT_UNKNOWN_REVIEW_REASON = (
    "direct-contract status could not be determined; a person must confirm "
    "before this patient is contacted"
)
_DIRECT_REVIEW_REASONS = frozenset({
    _DIRECT_KNOWN_REVIEW_REASON,
    _DIRECT_UNKNOWN_REVIEW_REASON,
})
_CADENCE_ACTIONABLE_STATES = frozenset({
    State.DETECTED.value,
    State.READY.value,
    State.IN_PROGRESS.value,
})
_CADENCE_PROTECTED_STATES = frozenset({
    State.NEEDS_REVIEW.value,
    State.SUBMITTED.value,
    State.VERIFIED_CLOSED.value,
    State.ESCALATED.value,
    State.UNREACHABLE.value,
    State.RESOLVED_OTHER.value,
})
_CADENCE_NONRESUMABLE_STATES = _CADENCE_PROTECTED_STATES - frozenset({
    State.NEEDS_REVIEW.value,
})
_RETRY_BLOCK_REASONS = frozenset({
    ScheduleReason.SCHEDULER_DISABLED,
    ScheduleReason.MESSAGING_DISABLED,
    ScheduleReason.CONTACT_TIMESTAMP_FUTURE,
    ScheduleReason.CONTACT_SNAPSHOT_STALE,
})
_INVARIANT_BLOCK_REASONS = frozenset({
    ScheduleReason.DURABLE_EVIDENCE_MISSING,
    ScheduleReason.POLICY_INACTIVE,
    ScheduleReason.EVIDENCE_MALFORMED,
    ScheduleReason.POLICY_FINGERPRINT_MISMATCH,
    ScheduleReason.STEP_IDENTITY_MISMATCH,
    ScheduleReason.MESSAGE_RELEASE_UNEXPECTED,
})


class SchedulerConflict(RuntimeError):
    """A durable idempotency identity or state no longer matches the request."""


class SchedulerLeaseExpired(SchedulerConflict):
    """A claimed provider submission must be quarantined before more work."""


class SchedulerAuthorizationError(PermissionError):
    """The named human is not currently allowed to perform an admin action."""


class SchedulerNotFound(LookupError):
    """An exact durable scheduling identity does not exist."""


@dataclass(frozen=True, repr=False)
class ContactRouteInput:
    """One typed contact route; repr never includes its PHI destination."""

    channel: Channel
    destination: str
    consent_status: str
    consent_source: str = ""
    consent_recorded_at: Optional[Instant] = None

    def __repr__(self) -> str:
        return (
            "ContactRouteInput("
            f"channel={self.channel.value!r}, destination='<redacted>', "
            f"consent_status={self.consent_status!r}, "
            f"consent_source={self.consent_source!r}, "
            f"consent_recorded_at={self.consent_recorded_at!r})"
        )


@dataclass(frozen=True)
class ContactSnapshotInput:
    """Immutable, caller-identified contact evidence for one detection."""

    contact_snapshot_id: str
    source_system: str
    source_version: str
    observed_at: Instant
    verified_at: Instant
    verification_method: Literal["staff", "authoritative_source"]
    routes: tuple[ContactRouteInput, ...]
    verified_by_tenant_id: str = ""
    verified_by_user_id: str = ""


@dataclass(frozen=True)
class DetectionPersistenceResult:
    chase_id: str
    contact_snapshot_id: str


@dataclass(frozen=True)
class PolicyRevisionRecord:
    policy_revision_id: str
    policy_name: str
    revision_no: int
    policy_fingerprint: str
    approval_mode: str


@dataclass(frozen=True)
class ApprovalRecord:
    approval_event_id: str
    policy_revision_id: str
    policy_fingerprint: str
    event_type: Literal["approved", "revoked"]
    occurred_at: datetime
    payload_hash: str


@dataclass(frozen=True)
class ReleaseRecord:
    release_id: str
    chase_id: str
    payload_hash: str


@dataclass(frozen=True, repr=False)
class DueReservation:
    """Safe reservation result; the patient-derived chase ID is redacted."""

    chase_id: str
    action: ScheduleAction
    reason_code: ScheduleReason
    reason: str
    policy_step_ordinal: int
    delivery_id: str = ""

    def __repr__(self) -> str:
        return (
            "DueReservation(chase_id='<redacted>', "
            f"action={self.action.value!r}, "
            f"reason_code={self.reason_code.value!r}, reason={self.reason!r}, "
            f"policy_step_ordinal={self.policy_step_ordinal!r}, "
            f"delivery_id={self.delivery_id!r})"
        )


@dataclass(frozen=True, repr=False)
class DispatchContext:
    """The sole recipient-bearing result, returned only by a new DB claim."""

    delivery_id: str
    lease_token: str
    provider_operation_id: str
    policy_revision_id: str
    policy_fingerprint: str
    policy_step_ordinal: int
    channel: Channel
    template_id: str
    recipient: str
    patient_ref: str
    gate_fingerprint: str
    gate_disposition: Literal["clear", "released"] = "clear"
    review_release_id: str = ""

    def __repr__(self) -> str:
        return (
            "DispatchContext("
            f"delivery_id={self.delivery_id!r}, lease_token={self.lease_token!r}, "
            f"provider_operation_id={self.provider_operation_id!r}, "
            f"policy_revision_id={self.policy_revision_id!r}, "
            f"policy_fingerprint={self.policy_fingerprint!r}, "
            f"policy_step_ordinal={self.policy_step_ordinal!r}, "
            f"channel={self.channel.value!r}, template_id={self.template_id!r}, "
            "recipient='<redacted>', patient_ref='<redacted>', "
            f"gate_fingerprint={self.gate_fingerprint!r}, "
            f"gate_disposition={self.gate_disposition!r}, "
            f"review_release_id={self.review_release_id!r})"
        )


@dataclass(frozen=True)
class AmbiguousDelivery:
    delivery_id: str
    provider_operation_id: str
    policy_fingerprint: str
    ambiguous_at: datetime
    error_code: str


@dataclass(frozen=True)
class FinalizationResult:
    delivery_id: str
    state: Literal["accepted", "permanent_failure"]
    attempt_recorded: bool
    cadence_advanced: bool


@dataclass(frozen=True)
class StaffTaskCompletionResult:
    chase_id: str
    attempt_recorded: bool
    cadence_advanced: bool


def _instant(value: Instant, *, field: str) -> datetime:
    if isinstance(value, str):
        encoded = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(encoded)
        except ValueError:
            raise ValueError(f"{field} must be an ISO-8601 instant with an offset") from None
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _uuid(value: str, *, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{field} must be a UUID") from None


def _fingerprint(value: str, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _bounded(value: str, *, field: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if required and not value.strip():
        raise ValueError(f"{field} is required")
    if len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} is invalid")
    return value


def _payload_hash(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _route_is_usable(channel: Channel, status: str) -> bool:
    if channel is Channel.SMS:
        return status == "granted"
    return status in {"not_required", "granted"}


def _automated_due_after_local_day(
    detected_at: Instant,
    *,
    day_offset: int,
    after: datetime,
    practice_timezone: str,
    quiet_hours: tuple[int, int],
) -> datetime:
    """Return the absolute due time, moved past ``after``'s local day.

    A delayed worker may accept one automated touch after a later cadence rung
    is already overdue.  Replaying the original absolute due time would make
    the next automated rung collide with the outbox's one-touch-per-local-day
    invariant.  Preserve the policy's detection-relative wall clock while
    advancing only as many calendar days as that invariant requires.
    """
    due = cadence_due_at(
        detected_at,
        day_offset=day_offset,
        practice_timezone=practice_timezone,
        quiet_hours=quiet_hours,
    )
    zone = ZoneInfo(practice_timezone)
    after_day = after.astimezone(zone).date()
    if due.astimezone(zone).date() > after_day:
        return due
    detected_day = _instant(
        detected_at, field="detected_at",
    ).astimezone(zone).date()
    minimum_offset = max(0, (after_day + timedelta(days=1) - detected_day).days)
    return cadence_due_at(
        detected_at,
        day_offset=max(day_offset, minimum_offset),
        practice_timezone=practice_timezone,
        quiet_hours=quiet_hours,
    )


class PostgresChaseSchedulerRepository:
    """Practice-scoped implementation of migration 004's transaction model."""

    def __init__(self, dsn: str, *, practice_id: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("scheduler repository requires a database URL")
        if (
            not isinstance(practice_id, str)
            or not practice_id.strip()
            or len(practice_id) > 128
        ):
            raise ValueError("scheduler repository requires a practice ID")
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - deployment wiring
            raise RuntimeError(
                "PostgreSQL chase scheduling requires the 'postgres' package extra"
            ) from exc
        self._dsn = dsn
        self.practice_id = practice_id.strip()
        self._connect = psycopg.connect
        self._database_error = psycopg.Error
        self._dict_row = dict_row
        self._jsonb = Jsonb

    def __repr__(self) -> str:
        return (
            "PostgresChaseSchedulerRepository("
            f"practice_id={self.practice_id!r}, dsn='<redacted>')"
        )

    @contextmanager
    def _connection(self):
        try:
            with self._connect(
                self._dsn, row_factory=self._dict_row, connect_timeout=3,
            ) as conn:
                conn.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, true)",
                    (self.practice_id,),
                )
                yield conn
        except self._database_error as exc:
            from ..persistence import PersistenceUnavailable

            raise PersistenceUnavailable(
                "PostgreSQL chase scheduling is unavailable"
            ) from exc

    # ------------------------------------------------------ authorization --
    def _require_admin(self, conn, tenant_id: str, user_id: str) -> tuple[str, str]:
        tenant = _uuid(tenant_id, field="actor tenant ID")
        user = _uuid(user_id, field="actor user ID")
        row = conn.execute(
            "SELECT cosmaneura_chase_admin_allowed(%s::uuid, %s::uuid) AS allowed",
            (tenant, user),
        ).fetchone()
        if row is None or row["allowed"] is not True:
            raise SchedulerAuthorizationError(
                "an active practice administrator must authorize this action"
            )
        return tenant, user

    def _require_staff(self, conn, tenant_id: str, user_id: str) -> tuple[str, str]:
        """API lane authorization through the existing security-definer check."""
        tenant = _uuid(tenant_id, field="actor tenant ID")
        user = _uuid(user_id, field="actor user ID")
        row = conn.execute(
            """SELECT cosmaneura_staff_assignee_allowed(
                       %s::uuid, %s::uuid
                   ) AS allowed""",
            (tenant, user),
        ).fetchone()
        if row is None or row["allowed"] is not True:
            raise SchedulerAuthorizationError(
                "an active administrator or billing staff member must authorize "
                "this release"
            )
        return tenant, user

    @staticmethod
    def _database_now(conn) -> datetime:
        return conn.execute(
            "SELECT statement_timestamp() AS now",
        ).fetchone()["now"]

    def _commit_audit(
        self,
        conn,
        audit_event,
        *,
        actor_tenant_id: str = "",
        actor_user_id: str = "",
    ) -> None:
        if audit_event is None:
            return
        if (
            audit_event.practice_id != self.practice_id
            or audit_event.outcome != "allowed"
            or (
                actor_tenant_id
                and audit_event.actor_tenant_id != actor_tenant_id
            )
            or (actor_user_id and audit_event.actor_user_id != actor_user_id)
        ):
            raise ValueError("chase audit identity does not match the mutation")
        from ..persistence import PostgresAccessAuditStore

        PostgresAccessAuditStore._insert_completion(conn, audit_event)

    # ------------------------------------------------ contact + detection --
    def _validated_contact(
        self, contact: ContactSnapshotInput,
    ) -> tuple[str, datetime, datetime, list[tuple[Any, ...]]]:
        snapshot_id = _uuid(
            contact.contact_snapshot_id, field="contact snapshot ID",
        )
        _bounded(contact.source_system, field="contact source", maximum=128)
        _bounded(contact.source_version, field="contact source version", maximum=256)
        observed = _instant(contact.observed_at, field="contact observed_at")
        verified = _instant(contact.verified_at, field="contact verified_at")
        if observed > verified:
            raise ValueError("contact observed_at cannot follow verified_at")
        if contact.verification_method != "staff":
            raise ValueError(
                "posted detection contact evidence requires staff verification; "
                "authoritative feeds use a separate trusted adapter"
            )
        _uuid(contact.verified_by_tenant_id, field="verifier tenant ID")
        _uuid(contact.verified_by_user_id, field="verifier user ID")
        if not contact.routes:
            raise ValueError("a contact snapshot requires at least one route")
        channels = [route.channel for route in contact.routes]
        if len(channels) != len(set(channels)):
            raise ValueError("a contact snapshot cannot repeat a channel")
        rows: list[tuple[Any, ...]] = []
        for route in contact.routes:
            destination = _bounded(
                route.destination, field="contact destination", maximum=512,
            )
            if destination != destination.strip():
                raise ValueError("contact destination cannot have surrounding whitespace")
            if route.consent_status not in _CONSENT:
                raise ValueError("unsupported contact consent status")
            if (
                route.channel is Channel.SMS
                and route.consent_status == "granted"
            ):
                raise ValueError(
                    "staff verification cannot create SMS opt-in evidence; "
                    "SMS consent requires the trusted authoritative feed"
                )
            if route.consent_status in {"granted", "denied"}:
                source = _bounded(
                    route.consent_source, field="consent source", maximum=128,
                )
                consent_at = _instant(
                    route.consent_recorded_at, field="consent recorded_at",
                )
            else:
                if route.consent_source or route.consent_recorded_at is not None:
                    raise ValueError(
                        "not-required/unknown consent cannot carry an assertion"
                    )
                source = None
                consent_at = None
            rows.append((
                route.channel.value, destination, route.consent_status,
                source, consent_at,
            ))
        return snapshot_id, observed, verified, rows

    def persist_detection(
        self, item: ChaseItem, contact: ContactSnapshotInput, *, audit_event=None,
    ) -> DetectionPersistenceResult:
        """Commit one detector upsert and its immutable contact snapshot together."""
        snapshot_id, observed, verified, routes = self._validated_contact(
            contact
        )
        with self._connection() as conn:
            tenant, user = self._require_staff(
                conn,
                contact.verified_by_tenant_id,
                contact.verified_by_user_id,
            )
            # Gate refresh can retire queued work. Serialize it before taking
            # the item lock so claim/guard/finalize never acquire these locks
            # in the opposite order.
            self._lock_active_policy(conn)
            recorded = self._database_now(conn)
            if verified > recorded:
                raise ValueError("contact verified_at cannot be in the future")
            # This narrow upsert intentionally does not reuse PostgresChaseStore's
            # general mutation helper.  That helper synchronizes/deletes pending
            # tasks and resolutions, authority the worker must not have merely
            # because it persists a fresh detector snapshot.
            existing_item = conn.execute(
                """SELECT chase_id, state, priority, appointment_at,
                          direct_contract, review_reason
                     FROM chase_items
                    WHERE practice_id = %s
                      AND patient_ref = %s AND trigger = %s
                    FOR UPDATE""",
                (self.practice_id, item.patient_ref, item.trigger.value),
            ).fetchone()
            if existing_item is None:
                inserted = conn.execute(
                    """INSERT INTO chase_items (
                           practice_id, chase_id, patient_ref, trigger,
                           detected_at, appointment_at, state, priority, bucket,
                           direct_contract, review_reason, assignee, closed_at
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s
                       ) ON CONFLICT (practice_id, patient_ref, trigger)
                         DO NOTHING RETURNING chase_id, state, priority,
                                              appointment_at, direct_contract,
                                              review_reason""",
                    (
                        self.practice_id, item.chase_id, item.patient_ref,
                        item.trigger.value, item.detected_at, item.appointment_at,
                        item.state.value, item.priority, item.bucket,
                        self._jsonb(item.direct_contract), item.review_reason,
                        item.assignee, item.closed_at,
                    ),
                ).fetchone()
                existing_item = inserted
                if existing_item is None:
                    existing_item = conn.execute(
                        """SELECT chase_id, state, priority, appointment_at,
                                  direct_contract, review_reason
                             FROM chase_items
                            WHERE practice_id = %s
                              AND patient_ref = %s AND trigger = %s
                            FOR UPDATE""",
                        (
                            self.practice_id, item.patient_ref,
                            item.trigger.value,
                        ),
                    ).fetchone()
            if existing_item is None:  # defensive: unique conflict must resolve
                raise SchedulerConflict("could not resolve concurrent detection")
            persisted_chase_id = existing_item["chase_id"]
            refreshed_appointment = (
                item.appointment_at or existing_item["appointment_at"]
            )
            refreshed_gate = (
                item.direct_contract or dict(existing_item["direct_contract"] or {})
            )
            if refreshed_gate.get("patient_ref") != item.patient_ref:
                raise ValueError(
                    "direct-contract gate does not belong to the detected patient"
                )
            for field in ("known", "is_direct_contract", "blocked"):
                if type(refreshed_gate.get(field)) is not bool:
                    raise ValueError(
                        "direct-contract gate has invalid boolean evidence"
                    )
            if refreshed_gate["blocked"] != (
                refreshed_gate["is_direct_contract"]
                or not refreshed_gate["known"]
            ):
                raise ValueError(
                    "direct-contract gate has inconsistent blocked evidence"
                )
            refreshed_gate_fingerprint = _fingerprint(
                refreshed_gate.get("gate_fingerprint", ""),
                field="direct-contract gate fingerprint",
            )
            previous_gate = dict(existing_item["direct_contract"] or {})
            gate_changed = (
                previous_gate.get("gate_fingerprint")
                != refreshed_gate_fingerprint
            )
            if not gate_changed and previous_gate != refreshed_gate:
                semantic_fields = (
                    "patient_ref", "known", "is_direct_contract", "blocked",
                )
                if any(
                    previous_gate.get(field) != refreshed_gate.get(field)
                    for field in semantic_fields
                ):
                    raise SchedulerConflict(
                        "different direct-contract semantics reused one fingerprint"
                    )
            refreshed_state = existing_item["state"]
            refreshed_review_reason = existing_item["review_reason"]
            if gate_changed and refreshed_gate.get("blocked") is True:
                if (
                    refreshed_state in _CADENCE_ACTIONABLE_STATES
                    or (
                        refreshed_state == State.NEEDS_REVIEW.value
                        and refreshed_review_reason in _DIRECT_REVIEW_REASONS
                    )
                ):
                    refreshed_state = State.NEEDS_REVIEW.value
                    refreshed_review_reason = (
                        _DIRECT_KNOWN_REVIEW_REASON
                        if (
                            refreshed_gate.get("known") is True
                            and refreshed_gate.get("is_direct_contract") is True
                        )
                        else _DIRECT_UNKNOWN_REVIEW_REASON
                    )
            elif gate_changed and (
                refreshed_gate.get("known") is True
                and refreshed_gate.get("is_direct_contract") is False
                and refreshed_gate.get("blocked") is False
                and refreshed_state == State.NEEDS_REVIEW.value
                and refreshed_review_reason in _DIRECT_REVIEW_REASONS
            ):
                refreshed_state = State.READY.value
                refreshed_review_reason = ""
            if existing_item["state"] not in {
                State.VERIFIED_CLOSED.value,
                State.ESCALATED.value,
                State.UNREACHABLE.value,
                State.RESOLVED_OTHER.value,
            } and (
                existing_item["priority"] != item.priority
                or existing_item["appointment_at"] != refreshed_appointment
                or previous_gate != refreshed_gate
                or existing_item["state"] != refreshed_state
                or existing_item["review_reason"] != refreshed_review_reason
            ):
                conn.execute(
                    """UPDATE chase_items
                          SET priority = %s,
                              appointment_at = CASE WHEN %s = ''
                                                    THEN appointment_at ELSE %s END,
                              direct_contract = CASE WHEN %s = '{}'::jsonb
                                                     THEN direct_contract ELSE %s END,
                              state = %s, review_reason = %s,
                              version = version + 1, updated_at = now()
                        WHERE practice_id = %s AND chase_id = %s""",
                    (
                        item.priority, refreshed_appointment,
                        refreshed_appointment, self._jsonb(refreshed_gate),
                        self._jsonb(refreshed_gate), refreshed_state,
                        refreshed_review_reason, self.practice_id,
                        persisted_chase_id,
                    ),
                )
                if gate_changed:
                    cleanup = conn.execute(
                        """SELECT cosmaneura_cancel_stale_chase_work(%s)
                                  AS retired_count""",
                        (persisted_chase_id,),
                    ).fetchone()
                    if (
                        cleanup is None
                        or type(cleanup["retired_count"]) is not int
                        or cleanup["retired_count"] < 0
                    ):
                        raise SchedulerConflict(
                            "stale chase work cleanup did not complete"
                        )
            existing = conn.execute(
                """SELECT chase_id, source_system, source_version, observed_at,
                          verified_at, verification_method,
                          verified_by_tenant_id, verified_by_user_id, created_at
                     FROM chase_contact_snapshots
                    WHERE practice_id = %s AND contact_snapshot_id = %s::uuid""",
                (self.practice_id, snapshot_id),
            ).fetchone()
            expected = {
                "chase_id": persisted_chase_id,
                "source_system": contact.source_system,
                "source_version": contact.source_version,
                "observed_at": observed,
                "verified_at": verified,
                "verification_method": contact.verification_method,
                "verified_by_tenant_id": (
                    UUID(contact.verified_by_tenant_id)
                    if contact.verified_by_tenant_id else None
                ),
                "verified_by_user_id": (
                    UUID(contact.verified_by_user_id)
                    if contact.verified_by_user_id else None
                ),
            }
            created_snapshot = False
            if existing is None:
                inserted_snapshot = conn.execute(
                    """INSERT INTO chase_contact_snapshots (
                           practice_id, contact_snapshot_id, chase_id,
                           source_system, source_version, observed_at, verified_at,
                           verification_method, verified_by_tenant_id,
                           verified_by_user_id, created_at
                       ) VALUES (
                           %s, %s::uuid, %s, %s, %s, %s, %s, %s,
                           %s::uuid, %s::uuid, %s
                       ) ON CONFLICT (practice_id, contact_snapshot_id)
                         DO NOTHING RETURNING contact_snapshot_id""",
                    (
                        self.practice_id, snapshot_id, persisted_chase_id,
                        contact.source_system, contact.source_version,
                        observed, verified, contact.verification_method,
                        tenant, user, recorded,
                    ),
                ).fetchone()
                created_snapshot = inserted_snapshot is not None
                if not created_snapshot:
                    existing = conn.execute(
                        """SELECT chase_id, source_system, source_version,
                                  observed_at, verified_at, verification_method,
                                  verified_by_tenant_id, verified_by_user_id,
                                  created_at
                             FROM chase_contact_snapshots
                            WHERE practice_id = %s
                              AND contact_snapshot_id = %s::uuid""",
                        (self.practice_id, snapshot_id),
                    ).fetchone()
            if created_snapshot:
                for channel, destination, status, source, consent_at in routes:
                    conn.execute(
                        """INSERT INTO chase_contact_routes (
                               practice_id, contact_snapshot_id, channel,
                               destination, consent_status, consent_source,
                               consent_recorded_at
                           ) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s)""",
                        (
                            self.practice_id, snapshot_id, channel, destination,
                            status, source, consent_at,
                        ),
                    )
            else:
                if existing is None:  # defensive after a resolved conflict
                    raise SchedulerConflict("contact snapshot conflict disappeared")
                if any(existing[key] != value for key, value in expected.items()):
                    raise SchedulerConflict(
                        "contact snapshot ID conflicts with different evidence"
                    )
                stored_routes = conn.execute(
                    """SELECT channel, destination, consent_status, consent_source,
                              consent_recorded_at
                         FROM chase_contact_routes
                        WHERE practice_id = %s
                          AND contact_snapshot_id = %s::uuid
                        ORDER BY channel""",
                    (self.practice_id, snapshot_id),
                ).fetchall()
                expected_routes = sorted(routes, key=lambda row: row[0])
                actual_routes = [tuple(row.values()) for row in stored_routes]
                if actual_routes != expected_routes:
                    raise SchedulerConflict(
                        "contact snapshot ID conflicts with different routes"
                    )
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=tenant, actor_user_id=user,
            )
        return DetectionPersistenceResult(persisted_chase_id, snapshot_id)

    # ----------------------------------------------------------- policies --
    @staticmethod
    def _policy_row(row: dict[str, Any]) -> PolicyRevisionRecord:
        return PolicyRevisionRecord(
            policy_revision_id=str(row["policy_revision_id"]),
            policy_name=row["policy_name"],
            revision_no=row["revision_no"],
            policy_fingerprint=row["policy_fingerprint"],
            approval_mode=row["approval_mode"],
        )

    def _validate_policy_revision(
        self,
        conn,
        policy: ChasePolicy,
        delivery: DeliveryConfiguration,
        *,
        practice_timezone: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
    ) -> str:
        """Recompute and compare every durable policy field after sealing."""
        revision_id = _uuid(policy_revision_id, field="policy revision ID")
        expected = policy_fingerprint(
            policy, practice_timezone=practice_timezone, delivery=delivery,
        )
        if expected != _fingerprint(
            expected_policy_fingerprint, field="policy fingerprint",
        ):
            raise SchedulerConflict(
                "executable policy does not match the requested fingerprint"
            )
        revision = conn.execute(
            """SELECT policy_name, policy_fingerprint, approval_mode,
                      practice_name, practice_timezone, quiet_hours_start,
                      quiet_hours_end, max_touches
                 FROM chase_policy_revisions
                WHERE practice_id = %s AND policy_revision_id = %s::uuid""",
            (self.practice_id, revision_id),
        ).fetchone()
        if revision is None:
            raise SchedulerNotFound("the exact policy revision does not exist")
        expected_revision = (
            policy.name,
            expected,
            policy.approval_mode.value,
            policy.practice_name,
            practice_timezone,
            time(policy.quiet_hours[0]),
            time(policy.quiet_hours[1]),
            policy.max_touches,
        )
        actual_revision = tuple(revision.values())
        if actual_revision != expected_revision:
            raise SchedulerConflict(
                "stored policy revision differs from executable configuration"
            )
        durable_steps = conn.execute(
            """SELECT step_ordinal, channel, template_id, day_offset,
                      performer, template_fingerprint
                 FROM chase_policy_steps
                WHERE practice_id = %s AND policy_revision_id = %s::uuid
                ORDER BY step_ordinal""",
            (self.practice_id, revision_id),
        ).fetchall()
        expected_steps = [
            (
                step.ordinal,
                step.channel.value,
                step.template_id,
                step.day_offset,
                step.performer,
                step.template_fingerprint,
            )
            for step in policy_step_records(policy)
        ]
        if [tuple(row.values()) for row in durable_steps] != expected_steps:
            raise SchedulerConflict(
                "stored policy steps differ from executable configuration"
            )
        return expected

    def create_policy(
        self,
        policy: ChasePolicy,
        delivery: DeliveryConfiguration,
        *,
        practice_timezone: str,
        policy_revision_id: str,
        actor_tenant_id: str,
        actor_user_id: str,
        audit_event=None,
    ) -> PolicyRevisionRecord:
        """Create one immutable exact revision (migration-owner/admin lane)."""
        revision_id = _uuid(policy_revision_id, field="policy revision ID")
        exact = policy_fingerprint(
            policy, practice_timezone=practice_timezone, delivery=delivery,
        )
        steps = policy_step_records(policy)
        with self._connection() as conn:
            tenant, user = self._require_admin(
                conn, actor_tenant_id, actor_user_id,
            )
            created = self._database_now(conn)
            # Immutable policy rows cannot be locked by the insert-only API
            # role. A transaction advisory lock serializes revision-number
            # allocation for this practice/name without broadening table grants.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{self.practice_id}\x1f{policy.name}",),
            )
            existing = conn.execute(
                """SELECT policy_revision_id, policy_name, revision_no,
                          policy_fingerprint, approval_mode
                     FROM chase_policy_revisions
                    WHERE practice_id = %s AND policy_name = %s
                      AND policy_fingerprint = %s""",
                (self.practice_id, policy.name, exact),
            ).fetchone()
            if existing is not None:
                if str(existing["policy_revision_id"]) != revision_id:
                    raise SchedulerConflict(
                        "the exact named policy already has another revision ID"
                    )
                record = self._policy_row(existing)
                self._commit_audit(
                    conn, audit_event,
                    actor_tenant_id=tenant, actor_user_id=user,
                )
                return record
            id_conflict = conn.execute(
                """SELECT 1 FROM chase_policy_revisions
                    WHERE practice_id = %s AND policy_revision_id = %s::uuid""",
                (self.practice_id, revision_id),
            ).fetchone()
            if id_conflict is not None:
                raise SchedulerConflict(
                    "policy revision ID conflicts with another policy"
                )
            revision_no = conn.execute(
                """SELECT COALESCE(max(revision_no), 0) + 1 AS revision_no
                     FROM chase_policy_revisions
                    WHERE practice_id = %s AND policy_name = %s""",
                (self.practice_id, policy.name),
            ).fetchone()["revision_no"]
            conn.execute(
                """INSERT INTO chase_policy_revisions (
                       practice_id, policy_revision_id, policy_name, revision_no,
                       policy_fingerprint, approval_mode, practice_name,
                       practice_timezone, quiet_hours_start, quiet_hours_end,
                       max_touches, created_by_tenant_id, created_by_user_id,
                       created_at
                   ) VALUES (
                       %s, %s::uuid, %s, %s, %s, %s, %s, %s,
                       make_time(%s, 0, 0), make_time(%s, 0, 0), %s,
                       %s::uuid, %s::uuid, %s
                   )""",
                (
                    self.practice_id, revision_id, policy.name, revision_no,
                    exact, policy.approval_mode.value, policy.practice_name,
                    practice_timezone, policy.quiet_hours[0], policy.quiet_hours[1],
                    policy.max_touches, tenant, user, created,
                ),
            )
            for step in steps:
                conn.execute(
                    """INSERT INTO chase_policy_steps (
                           practice_id, policy_revision_id, step_ordinal,
                           channel, template_id, day_offset, performer,
                           template_fingerprint
                       ) VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s)""",
                    (
                        self.practice_id, revision_id, step.ordinal,
                        step.channel.value, step.template_id, step.day_offset,
                        step.performer, step.template_fingerprint,
                    ),
                )
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=tenant, actor_user_id=user,
            )
        return PolicyRevisionRecord(
            revision_id, policy.name, revision_no, exact,
            policy.approval_mode.value,
        )

    @staticmethod
    def _approval_row(row: dict[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_event_id=str(row["approval_event_id"]),
            policy_revision_id=str(row["policy_revision_id"]),
            policy_fingerprint=row["policy_fingerprint"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            payload_hash=row["payload_hash"],
        )

    def _lock_policy_action(self, conn, policy_revision_id: str) -> None:
        """Serialize approval/revocation/activation before assigning DB time."""
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{self.practice_id}\x1f{policy_revision_id}",),
        )

    def _lock_active_policy(self, conn) -> None:
        """Serialize every executable-policy read with activation/revocation."""
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{self.practice_id}\x1factive-policy",),
        )

    def _append_approval_event(
        self,
        conn,
        *,
        event_type: Literal["approved", "revoked"],
        approval_event_id: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        actor_tenant_id: str,
        actor_user_id: str,
        reason: str,
    ) -> tuple[ApprovalRecord, bool]:
        event_id = _uuid(approval_event_id, field="approval event ID")
        revision_id = _uuid(policy_revision_id, field="policy revision ID")
        exact = _fingerprint(
            expected_policy_fingerprint, field="policy fingerprint",
        )
        reason = _bounded(
            reason, field="approval reason", maximum=2000,
            required=event_type == "revoked",
        )
        tenant, user = self._require_admin(conn, actor_tenant_id, actor_user_id)
        self._lock_active_policy(conn)
        self._lock_policy_action(conn, revision_id)
        revision = conn.execute(
            """SELECT policy_fingerprint
                 FROM chase_policy_revisions
                WHERE practice_id = %s AND policy_revision_id = %s::uuid""",
            (self.practice_id, revision_id),
        ).fetchone()
        if revision is None or revision["policy_fingerprint"] != exact:
            raise SchedulerNotFound("the exact policy revision does not exist")
        payload = _payload_hash({
            "schema": "cosmaneura.chase-policy-approval.v1",
            "policy_revision_id": revision_id,
            "policy_fingerprint": exact,
            "event_type": event_type,
            "actor_tenant_id": tenant,
            "actor_user_id": user,
            "reason": reason,
        })
        existing = conn.execute(
            """SELECT approval_event_id, policy_revision_id, policy_fingerprint,
                      event_type, occurred_at, payload_hash
                 FROM chase_policy_approval_events
                WHERE practice_id = %s AND approval_event_id = %s::uuid""",
            (self.practice_id, event_id),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != payload:
                raise SchedulerConflict(
                    "approval event ID conflicts with another action"
                )
            return self._approval_row(existing), False
        occurred = self._database_now(conn)
        latest = conn.execute(
            """SELECT occurred_at
                 FROM chase_policy_approval_events
                WHERE practice_id = %s AND policy_revision_id = %s::uuid
                ORDER BY occurred_at DESC, approval_event_id DESC LIMIT 1""",
            (self.practice_id, revision_id),
        ).fetchone()
        if latest is not None and occurred <= latest["occurred_at"]:
            raise SchedulerConflict(
                "policy approval events require increasing durable timestamps"
            )
        conn.execute(
            """INSERT INTO chase_policy_approval_events (
                   practice_id, approval_event_id, policy_revision_id,
                   policy_fingerprint, event_type, actor_tenant_id,
                   actor_user_id, occurred_at, reason, payload_hash
               ) VALUES (
                   %s, %s::uuid, %s::uuid, %s, %s, %s::uuid, %s::uuid,
                   %s, %s, %s
               )""",
            (
                self.practice_id, event_id, revision_id, exact, event_type,
                tenant, user, occurred, reason, payload,
            ),
        )
        return ApprovalRecord(
            event_id, revision_id, exact, event_type, occurred, payload,
        ), True

    def approve_policy(
        self,
        policy: ChasePolicy,
        delivery: DeliveryConfiguration,
        *,
        practice_timezone: str,
        approval_event_id: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        actor_tenant_id: str,
        actor_user_id: str,
        reason: str = "",
        activate: bool = False,
        audit_event=None,
    ) -> ApprovalRecord:
        """Append an exact approval and optionally activate it atomically."""
        with self._connection() as conn:
            record, inserted = self._append_approval_event(
                conn,
                event_type="approved",
                approval_event_id=approval_event_id,
                policy_revision_id=policy_revision_id,
                expected_policy_fingerprint=expected_policy_fingerprint,
                actor_tenant_id=actor_tenant_id,
                actor_user_id=actor_user_id,
                reason=reason,
            )
            # The approval insert's migration trigger now holds the revision
            # serialization lock and seals further step inserts. Revalidate the
            # complete server-configured policy before activation; a mismatch
            # rolls the approval event back with this transaction.
            self._validate_policy_revision(
                conn,
                policy,
                delivery,
                practice_timezone=practice_timezone,
                policy_revision_id=record.policy_revision_id,
                expected_policy_fingerprint=record.policy_fingerprint,
            )
            if activate:
                self._activate_policy(
                    conn,
                    policy_revision_id=record.policy_revision_id,
                    expected_policy_fingerprint=record.policy_fingerprint,
                    approval_event_id=record.approval_event_id,
                    activated_at=record.occurred_at,
                )
            elif not inserted:
                # Exact replay is intentionally a no-op.
                pass
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=actor_tenant_id,
                actor_user_id=actor_user_id,
            )
            return record

    def _activate_policy(
        self,
        conn,
        *,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        approval_event_id: str,
        activated_at: Instant,
    ) -> None:
        revision_id = _uuid(policy_revision_id, field="policy revision ID")
        event_id = _uuid(approval_event_id, field="approval event ID")
        exact = _fingerprint(
            expected_policy_fingerprint, field="policy fingerprint",
        )
        activated = _instant(activated_at, field="policy activated_at")
        conn.execute(
            """SELECT policy_revision_id FROM chase_policy_revisions
                WHERE practice_id = %s AND policy_revision_id = %s::uuid
                  AND policy_fingerprint = %s""",
            (self.practice_id, revision_id, exact),
        )
        latest = conn.execute(
            """SELECT approval_event_id, event_type
                 FROM chase_policy_approval_events
                WHERE practice_id = %s AND policy_revision_id = %s::uuid
                ORDER BY occurred_at DESC, approval_event_id DESC LIMIT 1""",
            (self.practice_id, revision_id),
        ).fetchone()
        if (
            latest is None
            or str(latest["approval_event_id"]) != event_id
            or latest["event_type"] != "approved"
        ):
            raise SchedulerConflict(
                "only the latest exact approval event can activate a policy"
            )
        active = conn.execute(
            """SELECT policy_revision_id, policy_fingerprint, approval_event_id
                 FROM chase_active_policies
                WHERE practice_id = %s FOR UPDATE""",
            (self.practice_id,),
        ).fetchone()
        changing_revision = active is not None and not (
            str(active["policy_revision_id"]) == revision_id
            and active["policy_fingerprint"] == exact
        )
        changing_approval = active is not None and (
            str(active["policy_revision_id"]) == revision_id
            and active["policy_fingerprint"] == exact
            and str(active["approval_event_id"]) != event_id
        )
        if changing_approval:
            queued = conn.execute(
                """SELECT 1 FROM chase_delivery_outbox
                    WHERE practice_id = %s
                      AND state IN ('reserved', 'submitting', 'ambiguous')
                    UNION ALL
                   SELECT 1 FROM chase_pending_tasks
                    WHERE practice_id = %s
                    LIMIT 1""",
                (self.practice_id, self.practice_id),
            ).fetchone()
            if queued is not None:
                raise SchedulerConflict(
                    "a new approval event cannot replace queued exact work"
                )
        if changing_revision:
            cadence_exists = conn.execute(
                """SELECT 1 FROM chase_cadence_states
                    WHERE practice_id = %s LIMIT 1""",
                (self.practice_id,),
            ).fetchone()
            if cadence_exists is not None:
                raise SchedulerConflict(
                    "a different policy revision cannot strand active cadences"
                )
        elif active is None:
            foreign_cadence = conn.execute(
                """SELECT 1 FROM chase_cadence_states
                    WHERE practice_id = %s
                      AND policy_revision_id <> %s::uuid
                    LIMIT 1""",
                (self.practice_id, revision_id),
            ).fetchone()
            if foreign_cadence is not None:
                raise SchedulerConflict(
                    "a different policy revision cannot strand active cadences"
                )
        if active is not None and (
            str(active["policy_revision_id"]) == revision_id
            and active["policy_fingerprint"] == exact
            and str(active["approval_event_id"]) == event_id
        ):
            return
        conn.execute(
            """INSERT INTO chase_active_policies (
                   practice_id, policy_revision_id, policy_fingerprint,
                   approval_event_id, activated_at, updated_at
               ) VALUES (%s, %s::uuid, %s, %s::uuid, %s, %s)
               ON CONFLICT (practice_id) DO UPDATE SET
                   policy_revision_id = EXCLUDED.policy_revision_id,
                   policy_fingerprint = EXCLUDED.policy_fingerprint,
                   approval_event_id = EXCLUDED.approval_event_id,
                   approval_event_type = 'approved',
                   activated_at = EXCLUDED.activated_at,
                   updated_at = EXCLUDED.updated_at,
                   version = chase_active_policies.version + 1""",
            (
                self.practice_id, revision_id, exact, event_id,
                activated, activated,
            ),
        )

    def activate_policy(
        self,
        policy: ChasePolicy,
        delivery: DeliveryConfiguration,
        *,
        practice_timezone: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        approval_event_id: str,
        actor_tenant_id: str,
        actor_user_id: str,
        audit_event=None,
    ) -> None:
        """Activate the latest approved event (migration-owner/admin lane)."""
        with self._connection() as conn:
            tenant, user = self._require_admin(
                conn, actor_tenant_id, actor_user_id,
            )
            self._lock_active_policy(conn)
            self._lock_policy_action(
                conn, _uuid(policy_revision_id, field="policy revision ID"),
            )
            self._validate_policy_revision(
                conn,
                policy,
                delivery,
                practice_timezone=practice_timezone,
                policy_revision_id=policy_revision_id,
                expected_policy_fingerprint=expected_policy_fingerprint,
            )
            self._activate_policy(
                conn,
                policy_revision_id=policy_revision_id,
                expected_policy_fingerprint=expected_policy_fingerprint,
                approval_event_id=approval_event_id,
                activated_at=self._database_now(conn),
            )
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=tenant, actor_user_id=user,
            )

    def revoke_policy(
        self,
        *,
        approval_event_id: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        actor_tenant_id: str,
        actor_user_id: str,
        reason: str,
        audit_event=None,
    ) -> ApprovalRecord:
        """Append a revocation and remove its active pointer in one transaction."""
        with self._connection() as conn:
            record, inserted = self._append_approval_event(
                conn,
                event_type="revoked",
                approval_event_id=approval_event_id,
                policy_revision_id=policy_revision_id,
                expected_policy_fingerprint=expected_policy_fingerprint,
                actor_tenant_id=actor_tenant_id,
                actor_user_id=actor_user_id,
                reason=reason,
            )
            if inserted:
                cleanup = conn.execute(
                    """SELECT cosmaneura_cancel_revoked_chase_policy(
                               %s::uuid, %s
                           ) AS retired_count""",
                    (record.policy_revision_id, record.policy_fingerprint),
                ).fetchone()
                if (
                    cleanup is None
                    or type(cleanup["retired_count"]) is not int
                    or cleanup["retired_count"] < 0
                ):
                    raise SchedulerConflict(
                        "revoked policy work was not retired atomically"
                    )
                conn.execute(
                    """DELETE FROM chase_active_policies
                        WHERE practice_id = %s
                          AND policy_revision_id = %s::uuid
                          AND policy_fingerprint = %s""",
                    (
                        self.practice_id, record.policy_revision_id,
                        record.policy_fingerprint,
                    ),
                )
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=actor_tenant_id,
                actor_user_id=actor_user_id,
            )
            return record

    # ------------------------------------------------------------ releases --
    def release_direct_contract(
        self,
        *,
        review_release_id: str,
        chase_id: str,
        gate_fingerprint: str,
        actor_tenant_id: str,
        actor_user_id: str,
        reason: str,
        audit_event=None,
    ) -> ReleaseRecord:
        """Release only the exact currently blocked gate (owner/admin lane)."""
        release_id = _uuid(review_release_id, field="review release ID")
        gate = _fingerprint(gate_fingerprint, field="gate fingerprint")
        reason = _bounded(reason, field="review release reason", maximum=2000)
        with self._connection() as conn:
            tenant, user = self._require_admin(
                conn, actor_tenant_id, actor_user_id,
            )
            released = self._database_now(conn)
            item = conn.execute(
                """SELECT direct_contract, state, review_reason
                     FROM chase_items
                    WHERE practice_id = %s AND chase_id = %s FOR UPDATE""",
                (self.practice_id, chase_id),
            ).fetchone()
            if item is None:
                raise SchedulerNotFound("chase item does not exist")
            current = item["direct_contract"] or {}
            if (
                current.get("gate_fingerprint") != gate
                or current.get("blocked") is not True
            ):
                raise SchedulerConflict(
                    "review release does not match the current blocked gate"
                )
            payload = _payload_hash({
                "schema": "cosmaneura.chase-review-release.v1",
                "chase_id": chase_id,
                "gate_fingerprint": gate,
                "actor_tenant_id": tenant,
                "actor_user_id": user,
                "reason": reason,
            })
            existing = conn.execute(
                """SELECT review_release_id, chase_id, payload_hash
                     FROM chase_review_releases
                    WHERE practice_id = %s
                      AND (review_release_id = %s::uuid
                           OR (chase_id = %s AND gate_fingerprint = %s))
                    """,
                (self.practice_id, release_id, chase_id, gate),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["review_release_id"]) != release_id
                    or existing["payload_hash"] != payload
                ):
                    raise SchedulerConflict(
                        "review release conflicts with existing exact evidence"
                    )
                record = ReleaseRecord(release_id, chase_id, payload)
                self._commit_audit(
                    conn, audit_event,
                    actor_tenant_id=tenant, actor_user_id=user,
                )
                return record
            if (
                item["state"] != State.NEEDS_REVIEW.value
                or item["review_reason"] not in _DIRECT_REVIEW_REASONS
            ):
                raise SchedulerConflict(
                    "direct-contract release cannot clear another review hold"
                )
            inserted = conn.execute(
                """INSERT INTO chase_review_releases (
                       practice_id, review_release_id, chase_id,
                       gate_fingerprint, actor_tenant_id, actor_user_id,
                       reason, released_at, payload_hash
                   ) VALUES (
                       %s, %s::uuid, %s, %s, %s::uuid, %s::uuid, %s, %s, %s
                   ) ON CONFLICT DO NOTHING
                     RETURNING review_release_id""",
                (
                    self.practice_id, release_id, chase_id, gate, tenant, user,
                    reason, released, payload,
                ),
            ).fetchone()
            if inserted is None:
                existing = conn.execute(
                    """SELECT review_release_id, chase_id, payload_hash
                         FROM chase_review_releases
                        WHERE practice_id = %s
                          AND (review_release_id = %s::uuid OR (
                               chase_id = %s AND gate_fingerprint = %s
                          ))""",
                    (self.practice_id, release_id, chase_id, gate),
                ).fetchone()
                if (
                    existing is None
                    or str(existing["review_release_id"]) != release_id
                    or existing["payload_hash"] != payload
                ):
                    raise SchedulerConflict(
                        "review release conflicts with existing exact evidence"
                    )
                self._commit_audit(
                    conn, audit_event,
                    actor_tenant_id=tenant, actor_user_id=user,
                )
                return ReleaseRecord(release_id, chase_id, payload)
            conn.execute(
                """UPDATE chase_items
                      SET state = CASE WHEN state = 'needs_review'
                                       THEN 'ready' ELSE state END,
                          review_reason = CASE WHEN state = 'needs_review'
                                               THEN '' ELSE review_reason END,
                          version = version + 1, updated_at = %s
                    WHERE practice_id = %s AND chase_id = %s""",
                (released, self.practice_id, chase_id),
            )
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=tenant, actor_user_id=user,
            )
            return ReleaseRecord(release_id, chase_id, payload)

    def release_message(
        self,
        *,
        release_id: str,
        chase_id: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        step_ordinal: int,
        contact_snapshot_id: str,
        actor_tenant_id: str,
        actor_user_id: str,
        reason: str,
        audit_event=None,
    ) -> ReleaseRecord:
        """Release one exact current per-message cadence step idempotently."""
        release_uuid = _uuid(release_id, field="message release ID")
        revision_id = _uuid(policy_revision_id, field="policy revision ID")
        snapshot_id = _uuid(contact_snapshot_id, field="contact snapshot ID")
        exact = _fingerprint(
            expected_policy_fingerprint, field="policy fingerprint",
        )
        if type(step_ordinal) is not int or step_ordinal < 0:
            raise ValueError("step ordinal must be a non-negative integer")
        reason = _bounded(reason, field="message release reason", maximum=2000)
        with self._connection() as conn:
            tenant, user = self._require_admin(
                conn, actor_tenant_id, actor_user_id,
            )
            self._lock_active_policy(conn)
            self._lock_policy_action(conn, revision_id)
            released = self._database_now(conn)
            cadence = conn.execute(
                """SELECT cadence.policy_revision_id,
                          cadence.contact_snapshot_id,
                          cadence.next_step_ordinal
                     FROM chase_cadence_states AS cadence
                     JOIN chase_active_policies AS active
                       ON active.practice_id = cadence.practice_id
                      AND active.policy_revision_id = cadence.policy_revision_id
                     JOIN chase_policy_revisions AS revision
                       ON revision.practice_id = cadence.practice_id
                      AND revision.policy_revision_id = cadence.policy_revision_id
                      AND revision.policy_fingerprint = active.policy_fingerprint
                    WHERE cadence.practice_id = %s AND cadence.chase_id = %s
                      AND cadence.policy_revision_id = %s::uuid
                      AND cadence.contact_snapshot_id = %s::uuid
                      AND cadence.next_step_ordinal = %s
                      AND active.policy_fingerprint = %s
                      AND revision.approval_mode = 'per_message'
                    """,
                (
                    self.practice_id, chase_id, revision_id, snapshot_id,
                    step_ordinal, exact,
                ),
            ).fetchone()
            if cadence is None:
                raise SchedulerConflict(
                    "message release does not match the active exact cadence"
                )
            payload = _payload_hash({
                "schema": "cosmaneura.chase-message-release.v1",
                "chase_id": chase_id,
                "policy_revision_id": revision_id,
                "policy_fingerprint": exact,
                "step_ordinal": step_ordinal,
                "contact_snapshot_id": snapshot_id,
                "actor_tenant_id": tenant,
                "actor_user_id": user,
                "reason": reason,
            })
            existing = conn.execute(
                """SELECT release_id, chase_id, payload_hash
                     FROM chase_message_releases
                    WHERE practice_id = %s
                      AND (release_id = %s::uuid OR (
                           chase_id = %s AND policy_revision_id = %s::uuid
                           AND step_ordinal = %s
                           AND contact_snapshot_id = %s::uuid
                      ))""",
                (
                    self.practice_id, release_uuid, chase_id, revision_id,
                    step_ordinal, snapshot_id,
                ),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["release_id"]) != release_uuid
                    or existing["payload_hash"] != payload
                ):
                    raise SchedulerConflict(
                        "message release conflicts with existing exact evidence"
                    )
                record = ReleaseRecord(release_uuid, chase_id, payload)
                self._commit_audit(
                    conn, audit_event,
                    actor_tenant_id=tenant, actor_user_id=user,
                )
                return record
            inserted = conn.execute(
                """INSERT INTO chase_message_releases (
                       practice_id, release_id, chase_id, policy_revision_id,
                       policy_fingerprint, step_ordinal, contact_snapshot_id,
                       actor_tenant_id, actor_user_id, reason, released_at,
                       payload_hash
                   ) VALUES (
                       %s, %s::uuid, %s, %s::uuid, %s, %s, %s::uuid,
                       %s::uuid, %s::uuid, %s, %s, %s
                   ) ON CONFLICT DO NOTHING RETURNING release_id""",
                (
                    self.practice_id, release_uuid, chase_id, revision_id, exact,
                    step_ordinal, snapshot_id, tenant, user, reason, released,
                    payload,
                ),
            ).fetchone()
            if inserted is None:
                existing = conn.execute(
                    """SELECT release_id, chase_id, payload_hash
                         FROM chase_message_releases
                        WHERE practice_id = %s
                          AND (release_id = %s::uuid OR (
                               chase_id = %s
                               AND policy_revision_id = %s::uuid
                               AND step_ordinal = %s
                               AND contact_snapshot_id = %s::uuid
                          ))""",
                    (
                        self.practice_id, release_uuid, chase_id, revision_id,
                        step_ordinal, snapshot_id,
                    ),
                ).fetchone()
                if (
                    existing is None
                    or str(existing["release_id"]) != release_uuid
                    or existing["payload_hash"] != payload
                ):
                    raise SchedulerConflict(
                        "message release conflicts with existing exact evidence"
                    )
                self._commit_audit(
                    conn, audit_event,
                    actor_tenant_id=tenant, actor_user_id=user,
                )
                return ReleaseRecord(release_uuid, chase_id, payload)
            conn.execute(
                """UPDATE chase_cadence_states
                      SET next_due_at = GREATEST(next_due_at, %s),
                          version = version + 1, updated_at = %s
                    WHERE practice_id = %s AND chase_id = %s
                      AND policy_revision_id = %s::uuid
                      AND contact_snapshot_id = %s::uuid
                      AND next_step_ordinal = %s""",
                (
                    released, released, self.practice_id, chase_id,
                    revision_id, snapshot_id, step_ordinal,
                ),
            )
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=tenant, actor_user_id=user,
            )
            return ReleaseRecord(release_uuid, chase_id, payload)

    # ------------------------------------------------------------ cadence --
    def schedule_unscheduled(
        self,
        policy: ChasePolicy,
        delivery: DeliveryConfiguration,
        *,
        practice_timezone: str,
        as_of: Instant,
        limit: int = 100,
    ) -> int:
        """Initialize bounded eligible rows from the active exact policy.

        Detection and immutable contact capture remain API-lane writes.  This
        worker-lane scan is the bridge that makes those rows schedulable after
        a restart without granting the worker patient identifiers as caller
        inputs.  Its only result is a PHI-free count.
        """
        recorded = _instant(as_of, field="cadence initialization as_of")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("cadence initialization limit must be 1-500")
        exact = policy_fingerprint(
            policy, practice_timezone=practice_timezone, delivery=delivery,
        )
        first_step = policy_step_records(policy)[0]
        with self._connection() as conn:
            self._lock_active_policy(conn)
            active = conn.execute(
                """SELECT policy_revision_id, policy_fingerprint
                     FROM chase_active_policies
                    WHERE practice_id = %s""",
                (self.practice_id,),
            ).fetchone()
            if active is None or active["policy_fingerprint"] != exact:
                raise SchedulerConflict(
                    "cadence initialization requires the active exact policy"
                )
            revision_id = str(active["policy_revision_id"])
            self._lock_policy_action(conn, revision_id)
            self._validate_policy_revision(
                conn,
                policy,
                delivery,
                practice_timezone=practice_timezone,
                policy_revision_id=revision_id,
                expected_policy_fingerprint=exact,
            )
            candidates = conn.execute(
                """SELECT item.chase_id, item.detected_at,
                          contact.contact_snapshot_id
                     FROM chase_items AS item
                     JOIN LATERAL (
                         SELECT snapshot.contact_snapshot_id
                           FROM chase_contact_snapshots AS snapshot
                          WHERE snapshot.practice_id = item.practice_id
                            AND snapshot.chase_id = item.chase_id
                          ORDER BY snapshot.verified_at DESC,
                                   snapshot.created_at DESC,
                                   snapshot.contact_snapshot_id DESC
                          LIMIT 1
                     ) AS contact ON true
                     LEFT JOIN chase_cadence_states AS cadence
                       ON cadence.practice_id = item.practice_id
                      AND cadence.chase_id = item.chase_id
                    WHERE item.practice_id = %s
                      AND item.state IN ('detected', 'ready', 'in_progress')
                      AND cadence.chase_id IS NULL
                    ORDER BY item.detected_at, item.chase_id
                    FOR UPDATE OF item SKIP LOCKED
                    LIMIT %s""",
                (self.practice_id, limit),
            ).fetchall()
            scheduled = 0
            for row in candidates:
                due = cadence_due_at(
                    row["detected_at"],
                    day_offset=first_step.day_offset,
                    practice_timezone=practice_timezone,
                    quiet_hours=policy.quiet_hours,
                )
                inserted = conn.execute(
                    """INSERT INTO chase_cadence_states (
                           practice_id, chase_id, policy_revision_id,
                           contact_snapshot_id, next_step_ordinal, next_due_at,
                           updated_at
                       ) VALUES (%s, %s, %s::uuid, %s::uuid, 0, %s, %s)
                       ON CONFLICT (practice_id, chase_id) DO NOTHING
                       RETURNING chase_id""",
                    (
                        self.practice_id, row["chase_id"], revision_id,
                        str(row["contact_snapshot_id"]), due, recorded,
                    ),
                ).fetchone()
                if inserted is not None:
                    scheduled += 1
            return scheduled

    def schedule_chase(
        self,
        *,
        chase_id: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        contact_snapshot_id: str,
        next_due_at: Instant,
        as_of: Instant,
    ) -> None:
        """Create (never silently reset) the first exact durable cadence row."""
        revision_id = _uuid(policy_revision_id, field="policy revision ID")
        snapshot_id = _uuid(contact_snapshot_id, field="contact snapshot ID")
        exact = _fingerprint(
            expected_policy_fingerprint, field="policy fingerprint",
        )
        due = _instant(next_due_at, field="next_due_at")
        recorded = _instant(as_of, field="cadence as_of")
        with self._connection() as conn:
            self._lock_active_policy(conn)
            self._lock_policy_action(conn, revision_id)
            eligible = conn.execute(
                """SELECT item.chase_id
                     FROM chase_items AS item
                     JOIN chase_contact_snapshots AS contact
                       ON contact.practice_id = item.practice_id
                      AND contact.chase_id = item.chase_id
                      AND contact.contact_snapshot_id = %s::uuid
                     JOIN chase_active_policies AS active
                       ON active.practice_id = item.practice_id
                      AND active.policy_revision_id = %s::uuid
                      AND active.policy_fingerprint = %s
                     JOIN chase_policy_steps AS step
                       ON step.practice_id = active.practice_id
                      AND step.policy_revision_id = active.policy_revision_id
                      AND step.step_ordinal = 0
                    WHERE item.practice_id = %s AND item.chase_id = %s
                    FOR UPDATE OF item""",
                (
                    snapshot_id, revision_id, exact, self.practice_id, chase_id,
                ),
            ).fetchone()
            if eligible is None:
                raise SchedulerConflict(
                    "cadence requires an active exact policy and contact snapshot"
                )
            existing = conn.execute(
                """SELECT policy_revision_id, contact_snapshot_id,
                          next_step_ordinal, next_due_at
                     FROM chase_cadence_states
                    WHERE practice_id = %s AND chase_id = %s FOR UPDATE""",
                (self.practice_id, chase_id),
            ).fetchone()
            if existing is not None:
                if not (
                    str(existing["policy_revision_id"]) == revision_id
                    and str(existing["contact_snapshot_id"]) == snapshot_id
                    and existing["next_step_ordinal"] == 0
                    and existing["next_due_at"] == due
                ):
                    raise SchedulerConflict(
                        "chase already has a different durable cadence"
                    )
                return
            conn.execute(
                """INSERT INTO chase_cadence_states (
                       practice_id, chase_id, policy_revision_id,
                       contact_snapshot_id, next_step_ordinal, next_due_at,
                       updated_at
                   ) VALUES (%s, %s, %s::uuid, %s::uuid, 0, %s, %s)""",
                (
                    self.practice_id, chase_id, revision_id, snapshot_id,
                    due, recorded,
                ),
            )

    def refresh_cadence_contact(
        self,
        *,
        chase_id: str,
        policy_revision_id: str,
        expected_policy_fingerprint: str,
        previous_contact_snapshot_id: str,
        contact_snapshot_id: str,
        as_of: Instant,
    ) -> None:
        """Attach a newer immutable contact snapshot to an unreserved step.

        The contact evidence itself is an API input committed separately with
        the detector row. This worker effect only moves the cadence pointer. A
        per-message policy consequently needs a new release for the new exact
        snapshot before reservation can proceed.
        """
        revision_id = _uuid(policy_revision_id, field="policy revision ID")
        previous_id = _uuid(
            previous_contact_snapshot_id, field="previous contact snapshot ID",
        )
        snapshot_id = _uuid(contact_snapshot_id, field="contact snapshot ID")
        exact = _fingerprint(
            expected_policy_fingerprint, field="policy fingerprint",
        )
        refreshed = _instant(as_of, field="contact refresh as_of")
        with self._connection() as conn:
            self._lock_active_policy(conn)
            self._lock_policy_action(conn, revision_id)
            cadence = conn.execute(
                """SELECT cadence.next_step_ordinal,
                          previous.verified_at AS previous_verified_at,
                          replacement.verified_at AS replacement_verified_at
                     FROM chase_cadence_states AS cadence
                     JOIN chase_active_policies AS active
                       ON active.practice_id = cadence.practice_id
                      AND active.policy_revision_id = cadence.policy_revision_id
                      AND active.policy_fingerprint = %s
                     JOIN chase_contact_snapshots AS previous
                       ON previous.practice_id = cadence.practice_id
                      AND previous.chase_id = cadence.chase_id
                      AND previous.contact_snapshot_id =
                          cadence.contact_snapshot_id
                     JOIN chase_contact_snapshots AS replacement
                       ON replacement.practice_id = cadence.practice_id
                      AND replacement.chase_id = cadence.chase_id
                      AND replacement.contact_snapshot_id = %s::uuid
                    WHERE cadence.practice_id = %s AND cadence.chase_id = %s
                      AND cadence.policy_revision_id = %s::uuid
                      AND cadence.contact_snapshot_id = %s::uuid
                    FOR UPDATE OF cadence""",
                (
                    exact, snapshot_id, self.practice_id, chase_id,
                    revision_id, previous_id,
                ),
            ).fetchone()
            if cadence is None:
                raise SchedulerConflict(
                    "contact refresh does not match the active exact cadence"
                )
            if cadence["replacement_verified_at"] > refreshed:
                raise ValueError("replacement contact snapshot is in the future")
            if (
                cadence["replacement_verified_at"]
                < cadence["previous_verified_at"]
            ):
                raise SchedulerConflict(
                    "contact refresh cannot replace newer durable evidence"
                )
            in_flight = conn.execute(
                """SELECT 1 FROM chase_delivery_outbox
                    WHERE practice_id = %s AND chase_id = %s
                      AND policy_revision_id = %s::uuid
                      AND policy_step_ordinal = %s
                      AND state IN ('reserved', 'submitting', 'ambiguous')
                    UNION ALL
                   SELECT 1 FROM chase_pending_tasks
                    WHERE practice_id = %s AND chase_id = %s
                    LIMIT 1""",
                (
                    self.practice_id, chase_id, revision_id,
                    cadence["next_step_ordinal"], self.practice_id, chase_id,
                ),
            ).fetchone()
            if in_flight is not None:
                raise SchedulerConflict(
                    "contact evidence cannot change beneath reserved work"
                )
            conn.execute(
                """UPDATE chase_cadence_states
                      SET contact_snapshot_id = %s::uuid,
                          version = version + 1, updated_at = %s
                    WHERE practice_id = %s AND chase_id = %s""",
                (snapshot_id, refreshed, self.practice_id, chase_id),
            )

    def _due_rows(self, conn, *, as_of: datetime, limit: int):
        return conn.execute(
            """SELECT cadence.chase_id, cadence.policy_revision_id,
                      cadence.contact_snapshot_id, cadence.next_step_ordinal,
                      cadence.next_due_at,
                      item.patient_ref, item.trigger, item.detected_at,
                      item.appointment_at, item.state, item.priority, item.bucket,
                      item.direct_contract, item.review_reason, item.assignee,
                      item.closed_at,
                      revision.policy_fingerprint, revision.approval_mode,
                      active.approval_event_id,
                      step.channel, step.template_id, step.day_offset,
                      contact.verified_at,
                      route.consent_status,
                      review.review_release_id,
                      message.release_id AS message_release_id
                 FROM chase_cadence_states AS cadence
                 JOIN chase_items AS item
                   ON item.practice_id = cadence.practice_id
                  AND item.chase_id = cadence.chase_id
                 JOIN chase_active_policies AS active
                   ON active.practice_id = cadence.practice_id
                  AND active.policy_revision_id = cadence.policy_revision_id
                 JOIN chase_policy_revisions AS revision
                   ON revision.practice_id = active.practice_id
                  AND revision.policy_revision_id = active.policy_revision_id
                  AND revision.policy_fingerprint = active.policy_fingerprint
                 JOIN chase_policy_steps AS step
                   ON step.practice_id = cadence.practice_id
                  AND step.policy_revision_id = cadence.policy_revision_id
                  AND step.step_ordinal = cadence.next_step_ordinal
                 JOIN chase_contact_snapshots AS contact
                   ON contact.practice_id = cadence.practice_id
                  AND contact.contact_snapshot_id = cadence.contact_snapshot_id
                  AND contact.chase_id = cadence.chase_id
                 LEFT JOIN chase_contact_routes AS route
                   ON route.practice_id = cadence.practice_id
                  AND route.contact_snapshot_id = cadence.contact_snapshot_id
                  AND route.channel = step.channel
                 LEFT JOIN chase_review_releases AS review
                   ON review.practice_id = cadence.practice_id
                  AND review.chase_id = cadence.chase_id
                  AND review.gate_fingerprint =
                      item.direct_contract ->> 'gate_fingerprint'
                 LEFT JOIN chase_message_releases AS message
                   ON message.practice_id = cadence.practice_id
                  AND message.chase_id = cadence.chase_id
                  AND message.policy_revision_id = cadence.policy_revision_id
                  AND message.policy_fingerprint = revision.policy_fingerprint
                  AND message.step_ordinal = cadence.next_step_ordinal
                  AND message.contact_snapshot_id = cadence.contact_snapshot_id
                 LEFT JOIN chase_pending_tasks AS pending
                   ON pending.practice_id = cadence.practice_id
                  AND pending.chase_id = cadence.chase_id
                WHERE cadence.practice_id = %s
                  AND cadence.next_due_at <= %s
                  AND item.state IN ('detected', 'ready', 'in_progress')
                  AND (
                      revision.approval_mode <> 'per_message'
                      OR message.release_id IS NOT NULL
                  )
                  AND pending.chase_id IS NULL
                ORDER BY cadence.next_due_at, cadence.chase_id
                FOR UPDATE OF cadence, item SKIP LOCKED
                LIMIT %s""",
            (self.practice_id, as_of, limit),
        ).fetchall()

    @staticmethod
    def _item_from_due(row: dict[str, Any]) -> ChaseItem:
        return ChaseItem(
            chase_id=row["chase_id"],
            patient_ref=row["patient_ref"],
            trigger=Trigger(row["trigger"]),
            detected_at=row["detected_at"],
            appointment_at=row["appointment_at"],
            state=State(row["state"]),
            priority=row["priority"],
            bucket=row["bucket"],
            direct_contract=dict(row["direct_contract"] or {}),
            review_reason=row["review_reason"],
            assignee=row["assignee"],
            closed_at=row["closed_at"],
        )

    @staticmethod
    def _gate_evidence(row: dict[str, Any]) -> tuple[str, str, str]:
        gate = dict(row["direct_contract"] or {})
        fingerprint = gate.get("gate_fingerprint")
        if not isinstance(fingerprint, str):
            fingerprint = ""
        if (
            gate.get("known") is True
            and gate.get("is_direct_contract") is False
            and gate.get("blocked") is False
        ):
            return fingerprint, "clear", ""
        release_id = row.get("review_release_id")
        if gate.get("blocked") is True and release_id is not None:
            return fingerprint, "released", str(release_id)
        return fingerprint, "blocked", ""

    def _skip_unavailable_step(
        self,
        conn,
        *,
        row: dict[str, Any],
        policy: ChasePolicy,
        practice_timezone: str,
        as_of: datetime,
    ) -> bool:
        """Skip one unusable channel without manufacturing a contact attempt."""
        next_ordinal = row["next_step_ordinal"] + 1
        if next_ordinal < min(policy.max_touches, len(policy.steps)):
            next_step = policy.steps[next_ordinal]
            durable = conn.execute(
                """SELECT channel, template_id, day_offset
                     FROM chase_policy_steps
                    WHERE practice_id = %s
                      AND policy_revision_id = %s::uuid
                      AND step_ordinal = %s""",
                (
                    self.practice_id, str(row["policy_revision_id"]),
                    next_ordinal,
                ),
            ).fetchone()
            if durable is None or (
                durable["channel"] != next_step.channel.value
                or durable["template_id"] != next_step.template_id
                or durable["day_offset"] != next_step.day_offset
            ):
                raise SchedulerConflict(
                    "the next durable step does not match executable policy"
                )
            next_due = cadence_due_at(
                row["detected_at"],
                day_offset=next_step.day_offset,
                practice_timezone=practice_timezone,
                quiet_hours=policy.quiet_hours,
            )
            conn.execute(
                """UPDATE chase_cadence_states
                      SET next_step_ordinal = %s, next_due_at = %s,
                          version = version + 1, updated_at = %s
                    WHERE practice_id = %s AND chase_id = %s""",
                (
                    next_ordinal, next_due, as_of,
                    self.practice_id, row["chase_id"],
                ),
            )
            return True
        # Exhausting unusable routes is not evidence that the patient ignored
        # outreach. Preserve the exact rung so a corrected route can resume it.
        conn.execute(
            """UPDATE chase_items
                  SET state = 'needs_review',
                      review_reason =
                          'cadence exhausted without an available consented route',
                      version = version + 1, updated_at = %s
                WHERE practice_id = %s AND chase_id = %s""",
            (as_of, self.practice_id, row["chase_id"]),
        )
        return False

    def _retire_protected_cadences(self, conn) -> None:
        """Remove only submission/terminal work that can never be resumed."""
        conn.execute(
            """DELETE FROM chase_cadence_states AS cadence
                USING chase_items AS item
                WHERE cadence.practice_id = %s
                  AND item.practice_id = cadence.practice_id
                  AND item.chase_id = cadence.chase_id
                  AND item.state IN (
                      'submitted', 'verified_closed', 'escalated',
                      'unreachable', 'resolved_other'
                  )""",
            (self.practice_id,),
        )

    def _retire_blocked_cadence(
        self,
        conn,
        *,
        row: dict[str, Any],
        decision: ScheduleDecision,
        as_of: datetime,
    ) -> None:
        """Pause a recoverable evidence failure at its exact durable rung."""
        gate = dict(row["direct_contract"] or {})
        if decision.reason_code in {
            ScheduleReason.GATE_EVIDENCE_CHANGED,
            ScheduleReason.GATE_RELEASE_INVALID,
            ScheduleReason.GATE_NOT_CLEARED,
        }:
            if (
                gate.get("known") is True
                and gate.get("is_direct_contract") is True
            ):
                review_reason = _DIRECT_KNOWN_REVIEW_REASON
            else:
                review_reason = _DIRECT_UNKNOWN_REVIEW_REASON
        else:
            review_reason = (
                f"chase scheduling blocked: {decision.reason_code.value}"
            )
        conn.execute(
            """UPDATE chase_items
                  SET state = 'needs_review', review_reason = %s,
                      version = version + 1, updated_at = %s
                WHERE practice_id = %s AND chase_id = %s
                  AND state IN ('detected', 'ready', 'in_progress')""",
            (
                review_reason, as_of, self.practice_id, row["chase_id"],
            ),
        )

    def reserve_due(
        self,
        policy: ChasePolicy,
        delivery: DeliveryConfiguration,
        *,
        practice_timezone: str,
        as_of: Instant,
        limit: int = 25,
    ) -> list[DueReservation]:
        """Evaluate and reserve due work under SKIP LOCKED, with no external I/O."""
        now = _instant(as_of, field="reservation as_of")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("reservation limit must be 1-500")
        results: list[DueReservation] = []
        with self._connection() as conn:
            self._lock_active_policy(conn)
            self._retire_protected_cadences(conn)
            for row in self._due_rows(conn, as_of=now, limit=limit):
                ordinal = row["next_step_ordinal"]
                self._lock_policy_action(
                    conn, str(row["policy_revision_id"]),
                )
                still_active = conn.execute(
                    """SELECT 1 FROM chase_active_policies
                        WHERE practice_id = %s
                          AND policy_revision_id = %s::uuid
                          AND policy_fingerprint = %s
                          AND approval_event_id = %s::uuid""",
                    (
                        self.practice_id, str(row["policy_revision_id"]),
                        row["policy_fingerprint"],
                        str(row["approval_event_id"]),
                    ),
                ).fetchone()
                if still_active is None:
                    raise SchedulerConflict(
                        "due cadence no longer matches the active policy"
                    )
                try:
                    configured_step = policy.steps[ordinal]
                except IndexError:
                    raise SchedulerConflict(
                        "durable cadence step is outside the executable policy"
                    )
                gate, disposition, review_id = self._gate_evidence(row)
                evidence = DurableScheduleEvidence(
                    policy_revision_id=str(row["policy_revision_id"]),
                    policy_step_ordinal=ordinal,
                    approval_event_id=str(row["approval_event_id"]),
                    approved_policy_fingerprint=row["policy_fingerprint"],
                    contact_snapshot_id=str(row["contact_snapshot_id"]),
                    contact_verified_at=row["verified_at"],
                    current_gate_fingerprint=gate,
                    reserved_gate_fingerprint=gate,
                    gate_disposition=disposition,
                    review_release_id=review_id,
                    message_release_id=(
                        str(row["message_release_id"])
                        if row["message_release_id"] is not None else ""
                    ),
                    active=True,
                )
                item = self._item_from_due(row)
                decision = evaluate_step(
                    item,
                    policy,
                    configured_step,
                    now=now,
                    practice_timezone=practice_timezone,
                    authorization=evidence,
                    delivery=delivery,
                    authoritative_sms_consent=(
                        row["channel"] == Channel.SMS.value
                        and row["consent_status"] == "granted"
                    ),
                )
                # Route availability is evaluated only after every hard policy,
                # gate, and freshness check. Otherwise a missing route could
                # accidentally skip past a stale contact or changed approval.
                if (
                    decision.action in {
                        ScheduleAction.TRANSPORT, ScheduleAction.STAFF_TASK,
                    }
                    and (
                        row["consent_status"] is None
                        or not _route_is_usable(
                            Channel(row["channel"]), row["consent_status"],
                        )
                    )
                ):
                    if row["consent_status"] is None:
                        decision = ScheduleDecision(
                            ScheduleAction.BLOCKED,
                            ScheduleReason.CHANNEL_UNAVAILABLE,
                            "no current route for the due channel",
                        )
                    else:
                        decision = ScheduleDecision(
                            ScheduleAction.BLOCKED,
                            ScheduleReason.CONSENT_REQUIRED,
                            "the due channel lacks current consent",
                        )
                if (
                    decision.cadence_due_at is not None
                    and row["next_due_at"] < decision.cadence_due_at
                ):
                    decision = ScheduleDecision(
                        ScheduleAction.BLOCKED,
                        ScheduleReason.STEP_IDENTITY_MISMATCH,
                        "persisted cadence precedes the exact policy due time",
                    )
                if decision.skippable_without_touch:
                    self._skip_unavailable_step(
                        conn,
                        row=row,
                        policy=policy,
                        practice_timezone=practice_timezone,
                        as_of=now,
                    )
                elif decision.action is ScheduleAction.BLOCKED:
                    if decision.reason_code in _INVARIANT_BLOCK_REASONS:
                        raise SchedulerConflict(
                            "durable cadence invariant failed: "
                            f"{decision.reason_code.value}"
                        )
                    if decision.reason_code in _RETRY_BLOCK_REASONS:
                        retry_at = now + timedelta(minutes=15)
                        conn.execute(
                            """UPDATE chase_cadence_states
                                  SET next_due_at = %s,
                                      version = version + 1,
                                      updated_at = %s
                                WHERE practice_id = %s AND chase_id = %s""",
                            (
                                retry_at, now, self.practice_id,
                                row["chase_id"],
                            ),
                        )
                    else:
                        self._retire_blocked_cadence(
                            conn, row=row, decision=decision, as_of=now,
                        )
                elif decision.action is ScheduleAction.WAIT:
                    assert decision.next_permitted_at is not None
                    conn.execute(
                        """UPDATE chase_cadence_states
                              SET next_due_at = %s, version = version + 1,
                                  updated_at = %s
                            WHERE practice_id = %s AND chase_id = %s""",
                        (
                            decision.next_permitted_at, now, self.practice_id,
                            row["chase_id"],
                        ),
                    )
                elif decision.action is ScheduleAction.TRANSPORT:
                    local_day = (
                        decision.next_permitted_at or now
                    ).astimezone(ZoneInfo(practice_timezone)).date()
                    existing = conn.execute(
                        """SELECT delivery_id, chase_id, policy_revision_id,
                                  policy_step_ordinal, contact_snapshot_id,
                                  state, finalized_at
                             FROM chase_delivery_outbox
                            WHERE practice_id = %s AND chase_id = %s
                              AND policy_revision_id = %s::uuid
                              AND policy_step_ordinal = %s
                              AND state NOT IN (
                                  'cancelled', 'permanent_failure'
                              )""",
                        (
                            self.practice_id, row["chase_id"],
                            str(row["policy_revision_id"]), ordinal,
                        ),
                    ).fetchone()
                    if existing is not None:
                        if existing["state"] == "accepted":
                            if existing["finalized_at"] is None:
                                raise SchedulerConflict(
                                    "accepted delivery has no durable final time"
                                )
                            self._advance_accepted(
                                conn,
                                row=existing,
                                accepted_at=existing["finalized_at"],
                            )
                            continue
                        delivery_id = str(existing["delivery_id"])
                        decision = ScheduleDecision(
                            decision.action, decision.reason_code,
                            "exact delivery step was already reserved",
                            cadence_due_at=decision.cadence_due_at,
                            next_permitted_at=decision.next_permitted_at,
                        )
                    else:
                        same_day = conn.execute(
                            """SELECT 1 FROM chase_delivery_outbox
                                WHERE practice_id = %s AND chase_id = %s
                                  AND local_delivery_date = %s
                                  AND state NOT IN (
                                      'cancelled', 'permanent_failure'
                                  )
                                LIMIT 1""",
                            (self.practice_id, row["chase_id"], local_day),
                        ).fetchone()
                        if same_day is not None:
                            deferred = _automated_due_after_local_day(
                                row["detected_at"],
                                day_offset=configured_step.day_offset,
                                after=decision.next_permitted_at or now,
                                practice_timezone=practice_timezone,
                                quiet_hours=policy.quiet_hours,
                            )
                            conn.execute(
                                """UPDATE chase_cadence_states
                                      SET next_due_at = %s,
                                          version = version + 1,
                                          updated_at = %s
                                    WHERE practice_id = %s AND chase_id = %s""",
                                (
                                    deferred, now, self.practice_id,
                                    row["chase_id"],
                                ),
                            )
                            results.append(DueReservation(
                                row["chase_id"], ScheduleAction.WAIT,
                                ScheduleReason.NOT_DUE,
                                "automated touch deferred to the next local day",
                                ordinal,
                            ))
                            continue
                        delivery_id = str(uuid4())
                        operation_id = str(uuid4())
                        conn.execute(
                            """INSERT INTO chase_delivery_outbox (
                                   practice_id, delivery_id,
                                   provider_operation_id, chase_id,
                                   policy_revision_id, policy_step_ordinal,
                                   approval_event_id, policy_fingerprint,
                                   approval_mode, message_release_id,
                                   gate_fingerprint, gate_disposition,
                                   review_release_id, contact_snapshot_id,
                                   channel, template_id, provider,
                                   local_delivery_date, state, available_at,
                                   created_at, updated_at
                               ) VALUES (
                                   %s, %s::uuid, %s::uuid, %s, %s::uuid, %s,
                                   %s::uuid, %s, %s, %s::uuid, %s, %s,
                                   %s::uuid, %s::uuid, %s, %s, %s, %s,
                                   'reserved', %s, %s, %s
                               )""",
                            (
                                self.practice_id, delivery_id, operation_id,
                                row["chase_id"], str(row["policy_revision_id"]),
                                ordinal, str(row["approval_event_id"]),
                                row["policy_fingerprint"], row["approval_mode"],
                                evidence.message_release_id or None, gate,
                                disposition, review_id or None,
                                str(row["contact_snapshot_id"]), row["channel"],
                                row["template_id"], delivery.provider,
                                local_day, decision.next_permitted_at or now,
                                now, now,
                            ),
                        )
                        self._append_delivery_event(
                            conn, delivery_id=delivery_id,
                            event_type="reserved", occurred_at=now,
                        )
                    results.append(DueReservation(
                        row["chase_id"], decision.action,
                        decision.reason_code, decision.reason,
                        ordinal, delivery_id,
                    ))
                    continue
                elif decision.action is ScheduleAction.STAFF_TASK:
                    payload = {
                        "schema": "cosmaneura.chase-staff-task.v1",
                        "policy_revision_id": str(row["policy_revision_id"]),
                        "policy_fingerprint": row["policy_fingerprint"],
                        "policy_step_ordinal": ordinal,
                        "approval_event_id": str(row["approval_event_id"]),
                        "contact_snapshot_id": str(row["contact_snapshot_id"]),
                        "gate_fingerprint": gate,
                        "gate_disposition": disposition,
                        "review_release_id": review_id or None,
                        "message_release_id": evidence.message_release_id or None,
                        "channel": row["channel"],
                        "template_id": row["template_id"],
                        "created_at": _iso(now),
                        # Rendering the call script is a service/UI seam.  No
                        # message body, route, or upload URL is persisted here.
                    }
                    inserted = conn.execute(
                        """INSERT INTO chase_pending_tasks (
                               practice_id, chase_id, payload, updated_at
                           ) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (practice_id, chase_id) DO NOTHING
                           RETURNING chase_id""",
                        (
                            self.practice_id, row["chase_id"],
                            self._jsonb(payload), now,
                        ),
                    ).fetchone()
                    if inserted is not None:
                        conn.execute(
                            """UPDATE chase_items
                                  SET state = 'in_progress', version = version + 1,
                                      updated_at = %s
                                WHERE practice_id = %s AND chase_id = %s""",
                            (now, self.practice_id, row["chase_id"]),
                        )
                    else:
                        decision = ScheduleDecision(
                            decision.action, decision.reason_code,
                            "exact staff task is already pending",
                            cadence_due_at=decision.cadence_due_at,
                            next_permitted_at=decision.next_permitted_at,
                        )
                results.append(DueReservation(
                    row["chase_id"], decision.action, decision.reason_code,
                    decision.reason, ordinal,
                ))
        return results

    def complete_staff_task(
        self,
        policy: ChasePolicy,
        delivery: DeliveryConfiguration,
        *,
        practice_timezone: str,
        chase_id: str,
        action_id: str,
        actor_tenant_id: str,
        actor_user_id: str,
        outcome: str,
        minutes_spent: float = 0.0,
        note: str = "",
        audit_event=None,
    ) -> StaffTaskCompletionResult:
        """Log one exact pending phone task and continue its durable cadence.

        This is an authenticated API/staff transaction, not worker automation.
        The call itself remains manual; the repository only makes the logged
        outcome and the next cadence rung atomic.
        """
        action_uuid = _uuid(action_id, field="staff action ID")
        outcome = _bounded(
            outcome, field="staff task outcome", maximum=64,
        )
        note = _bounded(
            note, field="staff task note", maximum=4000, required=False,
        )
        if (
            isinstance(minutes_spent, bool)
            or not isinstance(minutes_spent, (int, float))
            or not 0 <= float(minutes_spent) <= 1440
        ):
            raise ValueError("staff task minutes must be between 0 and 1440")
        executable = policy_fingerprint(
            policy, practice_timezone=practice_timezone, delivery=delivery,
        )
        payload_hash = _payload_hash({
            "schema": "cosmaneura.chase-staff-task-completion.v1",
            "chase_id": chase_id,
            "actor_tenant_id": _uuid(
                actor_tenant_id, field="actor tenant ID",
            ),
            "actor_user_id": _uuid(actor_user_id, field="actor user ID"),
            "outcome": outcome,
            "minutes_spent": float(minutes_spent),
            "note": note,
            "policy_fingerprint": executable,
        })
        with self._connection() as conn:
            self._lock_active_policy(conn)
            tenant, user = self._require_staff(
                conn, actor_tenant_id, actor_user_id,
            )
            item = conn.execute(
                """SELECT detected_at, state, direct_contract
                     FROM chase_items
                    WHERE practice_id = %s AND chase_id = %s FOR UPDATE""",
                (self.practice_id, chase_id),
            ).fetchone()
            if item is None:
                raise SchedulerNotFound("chase item does not exist")
            receipt = conn.execute(
                """SELECT chase_id, payload_hash
                     FROM chase_action_receipts
                    WHERE practice_id = %s AND action_id = %s::uuid""",
                (self.practice_id, action_uuid),
            ).fetchone()
            if receipt is not None:
                if (
                    receipt["chase_id"] != chase_id
                    or receipt["payload_hash"] != payload_hash
                ):
                    raise SchedulerConflict(
                        "staff action ID conflicts with another action"
                    )
                self._commit_audit(
                    conn, audit_event,
                    actor_tenant_id=tenant, actor_user_id=user,
                )
                return StaffTaskCompletionResult(chase_id, False, False)
            pending = conn.execute(
                """SELECT payload
                     FROM chase_pending_tasks
                    WHERE practice_id = %s AND chase_id = %s FOR UPDATE""",
                (self.practice_id, chase_id),
            ).fetchone()
            if pending is None:
                raise SchedulerConflict("chase item has no pending staff task")
            task = dict(pending["payload"] or {})
            if (
                task.get("schema") != "cosmaneura.chase-staff-task.v1"
                or task.get("channel") != Channel.PHONE.value
                or task.get("policy_fingerprint") != executable
            ):
                raise SchedulerConflict(
                    "pending staff task does not match executable policy"
                )
            self._lock_policy_action(
                conn,
                _uuid(
                    task.get("policy_revision_id"),
                    field="pending policy revision ID",
                ),
            )
            # The pending payload is only an immutable reference. Re-derive
            # the complete server-configured policy while holding the same
            # revision lock used by approval/activation before accepting a
            # staff-reported touch.
            self._validate_policy_revision(
                conn,
                policy,
                delivery,
                practice_timezone=practice_timezone,
                policy_revision_id=task.get("policy_revision_id"),
                expected_policy_fingerprint=executable,
            )
            ordinal = task.get("policy_step_ordinal")
            if (
                type(ordinal) is not int
                or not 0 <= ordinal < min(policy.max_touches, len(policy.steps))
                or policy.steps[ordinal].channel is not Channel.PHONE
                or policy.steps[ordinal].template_id != task.get("template_id")
            ):
                raise SchedulerConflict("pending staff step identity is invalid")
            cadence = conn.execute(
                """SELECT policy_revision_id, contact_snapshot_id,
                          next_step_ordinal
                     FROM chase_cadence_states
                    WHERE practice_id = %s AND chase_id = %s
                      AND policy_revision_id = %s::uuid
                      AND contact_snapshot_id = %s::uuid
                      AND next_step_ordinal = %s
                    FOR UPDATE""",
                (
                    self.practice_id, chase_id,
                    task.get("policy_revision_id"),
                    task.get("contact_snapshot_id"), ordinal,
                ),
            ).fetchone()
            if cadence is None:
                raise SchedulerConflict(
                    "pending staff task no longer matches durable cadence"
                )
            recorded = self._database_now(conn)
            authorization = conn.execute(
                """SELECT revision.approval_mode, contact.verified_at,
                          route.consent_status
                     FROM chase_active_policies AS active
                     JOIN chase_policy_revisions AS revision
                       ON revision.practice_id = active.practice_id
                      AND revision.policy_revision_id =
                          active.policy_revision_id
                      AND revision.policy_fingerprint =
                          active.policy_fingerprint
                     JOIN chase_contact_snapshots AS contact
                       ON contact.practice_id = active.practice_id
                      AND contact.contact_snapshot_id = %s::uuid
                      AND contact.chase_id = %s
                     JOIN chase_contact_routes AS route
                       ON route.practice_id = contact.practice_id
                      AND route.contact_snapshot_id =
                          contact.contact_snapshot_id
                      AND route.channel = 'phone'
                    WHERE active.practice_id = %s
                      AND active.policy_revision_id = %s::uuid
                      AND active.policy_fingerprint = %s
                      AND active.approval_event_id = %s::uuid""",
                (
                    task.get("contact_snapshot_id"), chase_id,
                    self.practice_id, task.get("policy_revision_id"),
                    executable, task.get("approval_event_id"),
                ),
            ).fetchone()
            if authorization is None:
                raise SchedulerConflict(
                    "pending staff task approval or contact route is no longer active"
                )
            historical_only = item["state"] in _CADENCE_PROTECTED_STATES
            if (
                item["state"] != State.IN_PROGRESS.value
                and not historical_only
            ):
                raise SchedulerConflict(
                    "pending staff task item is not actionable"
                )
            if not _route_is_usable(
                Channel.PHONE, authorization["consent_status"],
            ):
                raise SchedulerConflict(
                    "pending staff task no longer has a usable phone route"
                )
            if authorization["verified_at"] > recorded or (
                recorded - authorization["verified_at"]
                > timedelta(hours=delivery.contact_snapshot_max_age_hours)
            ):
                raise SchedulerConflict(
                    "pending staff task contact snapshot is no longer current"
                )
            current_gate = dict(item["direct_contract"] or {})
            if current_gate.get("gate_fingerprint") != task.get(
                "gate_fingerprint"
            ):
                raise SchedulerConflict(
                    "direct-contract gate changed after staff-task reservation"
                )
            if task.get("gate_disposition") == "clear":
                if not (
                    current_gate.get("known") is True
                    and current_gate.get("is_direct_contract") is False
                    and current_gate.get("blocked") is False
                    and task.get("review_release_id") is None
                ):
                    raise SchedulerConflict(
                        "pending staff task no longer has a clear gate"
                    )
            elif task.get("gate_disposition") == "released":
                review_id = task.get("review_release_id")
                review = conn.execute(
                    """SELECT 1 FROM chase_review_releases
                        WHERE practice_id = %s
                          AND review_release_id = %s::uuid
                          AND chase_id = %s
                          AND gate_fingerprint = %s""",
                    (
                        self.practice_id, review_id, chase_id,
                        task.get("gate_fingerprint"),
                    ),
                ).fetchone() if review_id else None
                if current_gate.get("blocked") is not True or review is None:
                    raise SchedulerConflict(
                        "pending staff task review release is no longer exact"
                    )
            else:
                raise SchedulerConflict(
                    "pending staff task direct-contract disposition is invalid"
                )
            if authorization["approval_mode"] == "per_message":
                release_id = task.get("message_release_id")
                message = conn.execute(
                    """SELECT 1 FROM chase_message_releases
                        WHERE practice_id = %s AND release_id = %s::uuid
                          AND chase_id = %s
                          AND policy_revision_id = %s::uuid
                          AND policy_fingerprint = %s
                          AND step_ordinal = %s
                          AND contact_snapshot_id = %s::uuid""",
                    (
                        self.practice_id, release_id, chase_id,
                        task.get("policy_revision_id"), executable, ordinal,
                        task.get("contact_snapshot_id"),
                    ),
                ).fetchone() if release_id else None
                if message is None:
                    raise SchedulerConflict(
                        "pending staff task message release is no longer exact"
                    )
            elif task.get("message_release_id") is not None:
                raise SchedulerConflict(
                    "per-policy staff task has an unexpected message release"
                )
            conn.execute(
                """INSERT INTO chase_action_receipts (
                       practice_id, action_id, chase_id, payload_hash, recorded_at
                   ) VALUES (%s, %s::uuid, %s, %s, %s)""",
                (
                    self.practice_id, action_uuid, chase_id, payload_hash,
                    recorded,
                ),
            )
            attempt_no = conn.execute(
                """SELECT COALESCE(max(attempt_no), 0) + 1 AS attempt_no
                     FROM chase_attempts
                    WHERE practice_id = %s AND chase_id = %s""",
                (self.practice_id, chase_id),
            ).fetchone()["attempt_no"]
            conn.execute(
                """INSERT INTO chase_attempts (
                       practice_id, chase_id, attempt_no, channel, template_id,
                       attempted_at, actor, outcome, minutes_spent, note
                   ) VALUES (%s, %s, %s, 'phone', %s, %s, %s, %s, %s, %s)""",
                (
                    self.practice_id, chase_id, attempt_no,
                    task["template_id"], _iso(recorded), user, outcome,
                    float(minutes_spent), note,
                ),
            )
            conn.execute(
                """DELETE FROM chase_pending_tasks
                    WHERE practice_id = %s AND chase_id = %s""",
                (self.practice_id, chase_id),
            )
            if historical_only:
                # The call remains historical truth even when a patient
                # submission, closure, or stronger human hold wins the race
                # before staff record it.  Never reopen/overwrite that state,
                # and discard the now-stale automation cadence.
                conn.execute(
                    """DELETE FROM chase_cadence_states
                        WHERE practice_id = %s AND chase_id = %s""",
                    (self.practice_id, chase_id),
                )
                self._commit_audit(
                    conn, audit_event,
                    actor_tenant_id=tenant, actor_user_id=user,
                )
                return StaffTaskCompletionResult(chase_id, True, False)
            next_ordinal = ordinal + 1
            cadence_advanced = next_ordinal < min(
                policy.max_touches, len(policy.steps),
            )
            if cadence_advanced:
                next_step = policy.steps[next_ordinal]
                durable = conn.execute(
                    """SELECT channel, template_id, day_offset
                         FROM chase_policy_steps
                        WHERE practice_id = %s
                          AND policy_revision_id = %s::uuid
                          AND step_ordinal = %s""",
                    (
                        self.practice_id, task["policy_revision_id"],
                        next_ordinal,
                    ),
                ).fetchone()
                if durable is None or (
                    durable["channel"] != next_step.channel.value
                    or durable["template_id"] != next_step.template_id
                    or durable["day_offset"] != next_step.day_offset
                ):
                    raise SchedulerConflict(
                        "next staff-task cadence step does not match policy"
                    )
                next_due = cadence_due_at(
                    item["detected_at"],
                    day_offset=next_step.day_offset,
                    practice_timezone=practice_timezone,
                    quiet_hours=policy.quiet_hours,
                )
                conn.execute(
                    """UPDATE chase_cadence_states
                          SET next_step_ordinal = %s, next_due_at = %s,
                              version = version + 1, updated_at = %s
                        WHERE practice_id = %s AND chase_id = %s""",
                    (
                        next_ordinal, next_due, recorded,
                        self.practice_id, chase_id,
                    ),
                )
                conn.execute(
                    """UPDATE chase_items
                          SET state = 'in_progress', version = version + 1,
                              updated_at = %s
                        WHERE practice_id = %s AND chase_id = %s""",
                    (recorded, self.practice_id, chase_id),
                )
            else:
                conn.execute(
                    """DELETE FROM chase_cadence_states
                        WHERE practice_id = %s AND chase_id = %s""",
                    (self.practice_id, chase_id),
                )
                conn.execute(
                    """UPDATE chase_items
                          SET state = 'escalated', closed_at = %s,
                              review_reason = 'approved cadence exhausted',
                              version = version + 1, updated_at = %s
                        WHERE practice_id = %s AND chase_id = %s""",
                    (
                        _iso(recorded), recorded,
                        self.practice_id, chase_id,
                    ),
                )
            self._commit_audit(
                conn, audit_event,
                actor_tenant_id=tenant, actor_user_id=user,
            )
            return StaffTaskCompletionResult(
                chase_id, True, cadence_advanced,
            )

    # ------------------------------------------------------------- outbox --
    def _append_delivery_event(
        self,
        conn,
        *,
        delivery_id: str,
        event_type: str,
        occurred_at: datetime,
        provider_message_id: str = "",
        error_code: str = "",
    ) -> None:
        conn.execute(
            """INSERT INTO chase_delivery_events (
                   practice_id, delivery_id, event_no, event_type, occurred_at,
                   provider_message_id, error_code
               ) SELECT %s, %s::uuid, COALESCE(max(event_no), 0) + 1,
                        %s, %s, %s, %s
                   FROM chase_delivery_events
                  WHERE practice_id = %s AND delivery_id = %s::uuid""",
            (
                self.practice_id, delivery_id, event_type, occurred_at,
                provider_message_id or None, error_code or None,
                self.practice_id, delivery_id,
            ),
        )

    def _quarantine_expired(
        self, conn, *, as_of: datetime, limit: int,
    ) -> list[str]:
        rows = conn.execute(
            """SELECT delivery_id
                 FROM chase_delivery_outbox
                WHERE practice_id = %s AND state = 'submitting'
                  AND lease_expires_at <= %s
                  AND lease_expires_at <= statement_timestamp()
                ORDER BY lease_expires_at, delivery_id
                FOR UPDATE SKIP LOCKED LIMIT %s""",
            (self.practice_id, as_of, limit),
        ).fetchall()
        quarantined: list[str] = []
        for row in rows:
            delivery_id = str(row["delivery_id"])
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'ambiguous', lease_token = NULL,
                          lease_expires_at = NULL, finalized_at = %s,
                          error_code = 'lease_expired_unreconciled',
                          updated_at = %s
                    WHERE practice_id = %s AND delivery_id = %s::uuid
                      AND state = 'submitting'""",
                (as_of, as_of, self.practice_id, delivery_id),
            )
            self._append_delivery_event(
                conn, delivery_id=delivery_id, event_type="ambiguous",
                occurred_at=as_of, error_code="lease_expired_unreconciled",
            )
            quarantined.append(delivery_id)
        return quarantined

    def quarantine_expired_submissions(
        self, *, as_of: Instant, limit: int = 100,
    ) -> list[str]:
        """Quarantine expired submission leases; never make them sendable again."""
        now = _instant(as_of, field="quarantine as_of")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("quarantine limit must be 1-1000")
        with self._connection() as conn:
            self._lock_active_policy(conn)
            return self._quarantine_expired(conn, as_of=now, limit=limit)

    def claim_delivery(
        self,
        *,
        expected_policy_fingerprint: str,
        contact_max_age_hours: int,
        as_of: Instant,
        lease_seconds: int,
    ) -> Optional[DispatchContext]:
        """Lease one never-submitted reservation and return its PHI once.

        The caller performs provider I/O only after this method commits.  A
        crash after commit is deliberately ambiguous: the row expires into the
        reconciliation lane and this method will never return it again.
        """
        exact = _fingerprint(
            expected_policy_fingerprint, field="policy fingerprint",
        )
        now = _instant(as_of, field="claim as_of")
        if (
            isinstance(contact_max_age_hours, bool)
            or type(contact_max_age_hours) is not int
            or not 1 <= contact_max_age_hours <= 168
        ):
            raise ValueError("contact maximum age must be 1-168 hours")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("delivery lease must be 1-3600 seconds")
        with self._connection() as conn:
            self._lock_active_policy(conn)
            self._quarantine_expired(conn, as_of=now, limit=100)
            ambiguous = conn.execute(
                """SELECT 1 FROM chase_delivery_outbox
                    WHERE practice_id = %s AND state = 'ambiguous'
                    LIMIT 1""",
                (self.practice_id,),
            ).fetchone()
            if ambiguous is not None:
                return None
            row = conn.execute(
                """SELECT outbox.delivery_id, outbox.provider_operation_id,
                          outbox.policy_revision_id,
                          outbox.policy_fingerprint,
                          outbox.policy_step_ordinal, outbox.channel,
                          outbox.template_id, outbox.gate_fingerprint,
                          outbox.gate_disposition, outbox.review_release_id,
                          outbox.approval_event_id,
                          item.patient_ref, route.destination
                     FROM chase_delivery_outbox AS outbox
                     JOIN chase_active_policies AS active
                       ON active.practice_id = outbox.practice_id
                      AND active.policy_revision_id = outbox.policy_revision_id
                      AND active.policy_fingerprint = outbox.policy_fingerprint
                      AND active.approval_event_id = outbox.approval_event_id
                     JOIN chase_cadence_states AS cadence
                       ON cadence.practice_id = outbox.practice_id
                      AND cadence.chase_id = outbox.chase_id
                      AND cadence.policy_revision_id = outbox.policy_revision_id
                      AND cadence.contact_snapshot_id = outbox.contact_snapshot_id
                      AND cadence.next_step_ordinal = outbox.policy_step_ordinal
                     JOIN chase_items AS item
                       ON item.practice_id = outbox.practice_id
                      AND item.chase_id = outbox.chase_id
                     JOIN chase_contact_snapshots AS contact
                       ON contact.practice_id = outbox.practice_id
                      AND contact.contact_snapshot_id = outbox.contact_snapshot_id
                      AND contact.chase_id = outbox.chase_id
                     JOIN chase_contact_routes AS route
                       ON route.practice_id = outbox.practice_id
                      AND route.contact_snapshot_id = outbox.contact_snapshot_id
                      AND route.channel = outbox.channel
                     LEFT JOIN chase_pending_tasks AS pending
                       ON pending.practice_id = outbox.practice_id
                      AND pending.chase_id = outbox.chase_id
                    WHERE outbox.practice_id = %s
                      AND outbox.state = 'reserved'
                      AND outbox.available_at <= %s
                      AND outbox.policy_fingerprint = %s
                      AND contact.verified_at >=
                          %s - make_interval(hours => %s)
                      AND item.direct_contract ->> 'gate_fingerprint' =
                          outbox.gate_fingerprint
                      AND item.state NOT IN (
                          'needs_review', 'submitted', 'verified_closed',
                          'escalated', 'unreachable', 'resolved_other'
                      )
                      AND pending.chase_id IS NULL
                      AND (
                          (outbox.channel IN ('email', 'portal')
                           AND route.consent_status IN ('not_required', 'granted'))
                          OR (outbox.channel IN ('sms', 'phone')
                              AND route.consent_status = 'granted')
                      )
                    ORDER BY outbox.available_at, outbox.created_at,
                             outbox.delivery_id
                    FOR UPDATE OF outbox SKIP LOCKED
                    FOR UPDATE OF item, cadence
                    LIMIT 1""",
                (
                    self.practice_id, now, exact, now,
                    contact_max_age_hours,
                ),
            ).fetchone()
            if row is None:
                return None
            self._lock_policy_action(
                conn, str(row["policy_revision_id"]),
            )
            still_active = conn.execute(
                """SELECT 1 FROM chase_active_policies
                    WHERE practice_id = %s
                      AND policy_revision_id = %s::uuid
                      AND policy_fingerprint = %s
                      AND approval_event_id = %s::uuid""",
                (
                    self.practice_id, str(row["policy_revision_id"]),
                    row["policy_fingerprint"],
                    str(row["approval_event_id"]),
                ),
            ).fetchone()
            if still_active is None:
                return None
            lease_token = str(uuid4())
            delivery_id = str(row["delivery_id"])
            lease_expires = now + timedelta(seconds=lease_seconds)
            conn.execute(
                """UPDATE chase_delivery_outbox
                      SET state = 'submitting', lease_token = %s::uuid,
                          lease_expires_at = %s, submission_started_at = %s,
                          updated_at = %s
                    WHERE practice_id = %s AND delivery_id = %s::uuid
                      AND state = 'reserved'""",
                (
                    lease_token, lease_expires, now, now,
                    self.practice_id, delivery_id,
                ),
            )
            self._append_delivery_event(
                conn, delivery_id=delivery_id, event_type="submitting",
                occurred_at=now,
            )
            return DispatchContext(
                delivery_id=delivery_id,
                lease_token=lease_token,
                provider_operation_id=str(row["provider_operation_id"]),
                policy_revision_id=str(row["policy_revision_id"]),
                policy_fingerprint=row["policy_fingerprint"],
                policy_step_ordinal=row["policy_step_ordinal"],
                channel=Channel(row["channel"]),
                template_id=row["template_id"],
                recipient=row["destination"],
                patient_ref=row["patient_ref"],
                gate_fingerprint=row["gate_fingerprint"],
                gate_disposition=row["gate_disposition"],
                review_release_id=(
                    str(row["review_release_id"])
                    if row["review_release_id"] is not None else ""
                ),
            )

    def list_ambiguous(self, *, limit: int = 100) -> list[AmbiguousDelivery]:
        """Return non-PHI provider identities for reconciliation, never sending."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("ambiguous delivery limit must be 1-1000")
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT delivery_id, provider_operation_id,
                          policy_fingerprint, finalized_at, error_code
                     FROM chase_delivery_outbox
                    WHERE practice_id = %s AND state = 'ambiguous'
                    ORDER BY finalized_at, delivery_id LIMIT %s""",
                (self.practice_id, limit),
            ).fetchall()
        return [AmbiguousDelivery(
            delivery_id=str(row["delivery_id"]),
            provider_operation_id=str(row["provider_operation_id"]),
            policy_fingerprint=row["policy_fingerprint"],
            ambiguous_at=row["finalized_at"],
            error_code=row["error_code"] or "",
        ) for row in rows]

    @contextmanager
    def guard_submission(
        self, context: DispatchContext,
    ) -> Iterator[DispatchContext]:
        """Hold exact authorization stable across the initial provider call.

        Claiming selects work; this guard is the send linearization boundary.
        The transaction-level practice lock serializes revocation while row
        locks prevent a patient submission, closure, gate refresh, or cadence
        change from committing beneath an in-flight provider request.
        """
        if not isinstance(context, DispatchContext):
            raise ValueError("submission guard requires a dispatch context")
        delivery_id = _uuid(context.delivery_id, field="delivery ID")
        lease_token = _uuid(context.lease_token, field="delivery lease token")
        operation_id = _uuid(
            context.provider_operation_id, field="provider operation ID",
        )
        revision_id = _uuid(
            context.policy_revision_id, field="policy revision ID",
        )
        exact = _fingerprint(
            context.policy_fingerprint, field="policy fingerprint",
        )
        gate = _fingerprint(
            context.gate_fingerprint, field="gate fingerprint",
        )
        if context.gate_disposition not in {"clear", "released"}:
            raise ValueError("submission guard has an invalid gate disposition")
        review_release_id = (
            _uuid(context.review_release_id, field="review release ID")
            if context.review_release_id else ""
        )
        if (
            (context.gate_disposition == "clear" and review_release_id)
            or (context.gate_disposition == "released" and not review_release_id)
        ):
            raise ValueError("submission guard has inconsistent gate evidence")
        if type(context.policy_step_ordinal) is not int:
            raise ValueError("submission guard has an invalid policy step")
        with self._connection() as conn:
            self._lock_active_policy(conn)
            lease = conn.execute(
                """SELECT lease_expires_at,
                          lease_expires_at IS NOT NULL
                          AND lease_expires_at > statement_timestamp()
                              AS lease_valid
                     FROM chase_delivery_outbox
                    WHERE practice_id = %s
                      AND delivery_id = %s::uuid
                      AND lease_token = %s::uuid
                      AND provider_operation_id = %s::uuid
                      AND state = 'submitting'
                    FOR UPDATE""",
                (
                    self.practice_id, delivery_id, lease_token, operation_id,
                ),
            ).fetchone()
            if lease is None:
                raise SchedulerConflict(
                    "delivery submission authorization is no longer exact"
                )
            if not lease["lease_valid"]:
                raise SchedulerLeaseExpired(
                    "delivery submission lease expired before provider I/O"
                )
            authorized = conn.execute(
                """SELECT 1
                     FROM chase_delivery_outbox AS outbox
                     JOIN chase_active_policies AS active
                       ON active.practice_id = outbox.practice_id
                      AND active.policy_revision_id = outbox.policy_revision_id
                      AND active.policy_fingerprint = outbox.policy_fingerprint
                      AND active.approval_event_id = outbox.approval_event_id
                     JOIN chase_cadence_states AS cadence
                       ON cadence.practice_id = outbox.practice_id
                      AND cadence.chase_id = outbox.chase_id
                      AND cadence.policy_revision_id = outbox.policy_revision_id
                      AND cadence.contact_snapshot_id = outbox.contact_snapshot_id
                      AND cadence.next_step_ordinal = outbox.policy_step_ordinal
                     JOIN chase_items AS item
                       ON item.practice_id = outbox.practice_id
                      AND item.chase_id = outbox.chase_id
                     JOIN chase_contact_snapshots AS contact
                       ON contact.practice_id = outbox.practice_id
                      AND contact.contact_snapshot_id = outbox.contact_snapshot_id
                      AND contact.chase_id = outbox.chase_id
                     JOIN chase_contact_routes AS route
                       ON route.practice_id = outbox.practice_id
                      AND route.contact_snapshot_id = outbox.contact_snapshot_id
                      AND route.channel = outbox.channel
                     LEFT JOIN chase_review_releases AS review
                       ON review.practice_id = outbox.practice_id
                      AND review.review_release_id = outbox.review_release_id
                      AND review.chase_id = outbox.chase_id
                      AND review.gate_fingerprint = outbox.gate_fingerprint
                     LEFT JOIN chase_pending_tasks AS pending
                       ON pending.practice_id = outbox.practice_id
                      AND pending.chase_id = outbox.chase_id
                    WHERE outbox.practice_id = %s
                      AND outbox.delivery_id = %s::uuid
                      AND outbox.lease_token = %s::uuid
                      AND outbox.provider_operation_id = %s::uuid
                      AND outbox.state = 'submitting'
                      AND outbox.lease_expires_at IS NOT NULL
                      AND outbox.lease_expires_at > statement_timestamp()
                      AND outbox.policy_revision_id = %s::uuid
                      AND outbox.policy_fingerprint = %s
                      AND outbox.policy_step_ordinal = %s
                      AND outbox.channel = %s
                      AND outbox.template_id = %s
                      AND outbox.gate_fingerprint = %s
                      AND outbox.gate_disposition = %s
                      AND COALESCE(outbox.review_release_id::text, '') = %s
                      AND item.patient_ref = %s
                      AND route.destination = %s
                      AND item.state IN ('detected', 'ready', 'in_progress')
                      AND item.direct_contract ->> 'gate_fingerprint' =
                          outbox.gate_fingerprint
                      AND pending.chase_id IS NULL
                      AND (
                          (
                              outbox.gate_disposition = 'clear'
                              AND outbox.review_release_id IS NULL
                              AND item.direct_contract ->> 'known' = 'true'
                              AND item.direct_contract ->> 'is_direct_contract' =
                                  'false'
                              AND item.direct_contract ->> 'blocked' = 'false'
                          ) OR (
                              outbox.gate_disposition = 'released'
                              AND review.review_release_id IS NOT NULL
                              AND item.direct_contract ->> 'blocked' = 'true'
                          )
                      )
                      AND (
                          (outbox.channel IN ('email', 'portal')
                           AND route.consent_status IN ('not_required', 'granted'))
                          OR (outbox.channel IN ('sms', 'phone')
                              AND route.consent_status = 'granted')
                      )
                    FOR UPDATE OF outbox, item, cadence""",
                (
                    self.practice_id, delivery_id, lease_token, operation_id,
                    revision_id, exact, context.policy_step_ordinal,
                    context.channel.value, context.template_id, gate,
                    context.gate_disposition, review_release_id,
                    context.patient_ref, context.recipient,
                ),
            ).fetchone()
            if authorized is None:
                lease = conn.execute(
                    """SELECT lease_expires_at IS NOT NULL
                              AND lease_expires_at > statement_timestamp()
                                  AS lease_valid
                         FROM chase_delivery_outbox
                        WHERE practice_id = %s
                          AND delivery_id = %s::uuid
                          AND lease_token = %s::uuid
                          AND provider_operation_id = %s::uuid
                          AND state = 'submitting'""",
                    (
                        self.practice_id, delivery_id, lease_token,
                        operation_id,
                    ),
                ).fetchone()
                if lease is not None and not lease["lease_valid"]:
                    raise SchedulerLeaseExpired(
                        "delivery submission lease expired before provider I/O"
                    )
                raise SchedulerConflict(
                    "delivery submission authorization is no longer exact"
                )
            yield context

    def _validate_provider_result(
        self,
        *,
        outcome: str,
        provider_message_id: str,
        error_code: str,
    ) -> tuple[str, str]:
        if outcome not in {"accepted", "permanent_failure"}:
            raise ValueError("delivery outcome must be accepted or permanent_failure")
        if provider_message_id:
            _bounded(
                provider_message_id, field="provider message ID", maximum=512,
            )
        if error_code and _SAFE_ERROR.fullmatch(error_code) is None:
            raise ValueError("delivery error code is invalid")
        if outcome == "accepted" and error_code:
            raise ValueError("accepted delivery cannot carry an error code")
        if outcome == "permanent_failure" and not error_code:
            raise ValueError("permanent failure requires a bounded error code")
        return provider_message_id, error_code

    def _advance_accepted(
        self,
        conn,
        *,
        row: dict[str, Any],
        accepted_at: datetime,
    ) -> bool:
        item = conn.execute(
            """SELECT state, detected_at
                 FROM chase_items
                WHERE practice_id = %s AND chase_id = %s
                FOR UPDATE""",
            (self.practice_id, row["chase_id"]),
        ).fetchone()
        if item is None:
            raise SchedulerNotFound("accepted delivery chase item does not exist")
        attempt = conn.execute(
            """SELECT cosmaneura_record_accepted_delivery_attempt(
                       %s::uuid
                   ) AS attempt_no""",
            (str(row["delivery_id"]),),
        ).fetchone()
        if (
            attempt is None
            or type(attempt["attempt_no"]) is not int
            or attempt["attempt_no"] <= 0
        ):
            raise SchedulerConflict(
                "accepted delivery did not produce an exact contact attempt"
            )
        review_hold = item["state"] == State.NEEDS_REVIEW.value
        if item["state"] not in _CADENCE_ACTIONABLE_STATES and not review_hold:
            # Provider acceptance is durable historical evidence, but patient
            # submission/closure and human review state outrank an in-flight
            # automation result.  Retire rather than advance stale work.
            conn.execute(
                """DELETE FROM chase_cadence_states
                    WHERE practice_id = %s AND chase_id = %s""",
                (self.practice_id, row["chase_id"]),
            )
            return False
        cadence = conn.execute(
            """SELECT next_step_ordinal
                 FROM chase_cadence_states
                WHERE practice_id = %s AND chase_id = %s
                  AND policy_revision_id = %s::uuid
                  AND contact_snapshot_id = %s::uuid
                  AND next_step_ordinal = %s
                FOR UPDATE""",
            (
                self.practice_id, row["chase_id"],
                str(row["policy_revision_id"]),
                str(row["contact_snapshot_id"]), row["policy_step_ordinal"],
            ),
        ).fetchone()
        if cadence is None:
            raise SchedulerConflict(
                "accepted delivery no longer matches the exact cadence"
            )
        conn.execute(
            """UPDATE chase_items
                  SET state = CASE WHEN state IN ('detected', 'ready')
                                   THEN 'in_progress' ELSE state END,
                      version = version + 1, updated_at = %s
                WHERE practice_id = %s AND chase_id = %s""",
            (accepted_at, self.practice_id, row["chase_id"]),
        )
        next_ordinal = row["policy_step_ordinal"] + 1
        next_step = conn.execute(
            """SELECT step.day_offset, step.performer,
                      revision.practice_timezone,
                      revision.quiet_hours_start,
                      revision.quiet_hours_end
                 FROM chase_policy_steps AS step
                 JOIN chase_policy_revisions AS revision
                   ON revision.practice_id = step.practice_id
                  AND revision.policy_revision_id = step.policy_revision_id
                WHERE step.practice_id = %s
                  AND step.policy_revision_id = %s::uuid
                  AND step.step_ordinal = %s
                  AND step.step_ordinal < revision.max_touches""",
            (self.practice_id, str(row["policy_revision_id"]), next_ordinal),
        ).fetchone()
        if next_step is None:
            if review_hold:
                # The exact accepted rung remains as a recovery marker until
                # its human hold is cleared. reserve_due then replays this
                # idempotently and closes the exhausted cadence exactly once.
                return False
            conn.execute(
                """DELETE FROM chase_cadence_states
                    WHERE practice_id = %s AND chase_id = %s""",
                (self.practice_id, row["chase_id"]),
            )
            conn.execute(
                """UPDATE chase_items
                      SET state = 'escalated', closed_at = %s,
                          review_reason = 'approved cadence exhausted',
                          version = version + 1, updated_at = %s
                    WHERE practice_id = %s AND chase_id = %s""",
                (
                    _iso(accepted_at), accepted_at,
                    self.practice_id, row["chase_id"],
                ),
            )
            return False
        quiet_hours = (
            next_step["quiet_hours_start"].hour,
            next_step["quiet_hours_end"].hour,
        )
        if next_step["performer"] == "system":
            next_due = _automated_due_after_local_day(
                item["detected_at"],
                day_offset=next_step["day_offset"],
                after=accepted_at,
                practice_timezone=next_step["practice_timezone"],
                quiet_hours=quiet_hours,
            )
        else:
            next_due = cadence_due_at(
                item["detected_at"],
                day_offset=next_step["day_offset"],
                practice_timezone=next_step["practice_timezone"],
                quiet_hours=quiet_hours,
            )
        conn.execute(
            """UPDATE chase_cadence_states
                  SET next_step_ordinal = %s, next_due_at = %s,
                      version = version + 1, updated_at = %s
                WHERE practice_id = %s AND chase_id = %s""",
            (
                next_ordinal, next_due, accepted_at,
                self.practice_id, row["chase_id"],
            ),
        )
        return True

    def _finalize_locked(
        self,
        conn,
        *,
        row: dict[str, Any],
        outcome: Literal["accepted", "permanent_failure"],
        as_of: datetime,
        provider_message_id: str,
        error_code: str,
    ) -> FinalizationResult:
        conn.execute(
            """UPDATE chase_delivery_outbox
                  SET state = %s, lease_token = NULL, lease_expires_at = NULL,
                      finalized_at = %s, provider_message_id = %s,
                      error_code = %s, updated_at = %s
                WHERE practice_id = %s AND delivery_id = %s::uuid""",
            (
                outcome, as_of, provider_message_id or None,
                error_code or None, as_of, self.practice_id,
                str(row["delivery_id"]),
            ),
        )
        cadence_advanced = False
        if outcome == "accepted":
            cadence_advanced = self._advance_accepted(
                conn, row=row, accepted_at=as_of,
            )
        else:
            # A permanent provider failure is neither a patient touch nor a
            # reason to fall through to the next channel. Keep the exact rung
            # so reviewed contact correction can reserve a replacement.
            item = conn.execute(
                """SELECT state FROM chase_items
                    WHERE practice_id = %s AND chase_id = %s FOR UPDATE""",
                (self.practice_id, row["chase_id"]),
            ).fetchone()
            if item is None:
                raise SchedulerNotFound(
                    "failed delivery chase item does not exist"
                )
            if item["state"] in _CADENCE_NONRESUMABLE_STATES:
                conn.execute(
                    """DELETE FROM chase_cadence_states
                        WHERE practice_id = %s AND chase_id = %s""",
                    (self.practice_id, row["chase_id"]),
                )
            else:
                conn.execute(
                    """UPDATE chase_items
                          SET state = 'needs_review',
                              review_reason = CASE
                                  WHEN state = 'needs_review' THEN review_reason
                                  ELSE %s
                              END,
                              version = version + 1, updated_at = %s
                        WHERE practice_id = %s AND chase_id = %s
                          AND state IN (
                              'detected', 'ready', 'in_progress', 'needs_review'
                          )""",
                    (
                        f"automated delivery failed: {error_code}", as_of,
                        self.practice_id, row["chase_id"],
                    ),
                )
        self._append_delivery_event(
            conn,
            delivery_id=str(row["delivery_id"]),
            event_type=outcome,
            occurred_at=as_of,
            provider_message_id=provider_message_id,
            error_code=error_code,
        )
        return FinalizationResult(
            delivery_id=str(row["delivery_id"]),
            state=outcome,
            attempt_recorded=outcome == "accepted",
            cadence_advanced=cadence_advanced,
        )

    def finalize_delivery(
        self,
        *,
        delivery_id: str,
        lease_token: str,
        outcome: Literal["accepted", "permanent_failure"],
        as_of: Instant,
        provider_message_id: str = "",
        error_code: str = "",
    ) -> FinalizationResult:
        """Commit a known provider result; derive any next cadence internally."""
        delivery_uuid = _uuid(delivery_id, field="delivery ID")
        lease_uuid = _uuid(lease_token, field="delivery lease token")
        finished = _instant(as_of, field="delivery finalized_at")
        provider_message_id, error_code = self._validate_provider_result(
            outcome=outcome,
            provider_message_id=provider_message_id,
            error_code=error_code,
        )
        with self._connection() as conn:
            self._lock_active_policy(conn)
            row = conn.execute(
                """SELECT delivery_id, chase_id, policy_revision_id,
                          policy_step_ordinal, contact_snapshot_id, channel,
                          template_id, state, lease_token, provider_message_id,
                          error_code
                     FROM chase_delivery_outbox
                    WHERE practice_id = %s AND delivery_id = %s::uuid
                    FOR UPDATE""",
                (self.practice_id, delivery_uuid),
            ).fetchone()
            if row is None:
                raise SchedulerNotFound("delivery does not exist")
            if row["state"] == outcome:
                if (
                    (row["provider_message_id"] or "") != provider_message_id
                    or (row["error_code"] or "") != error_code
                ):
                    raise SchedulerConflict(
                        "delivery finalization conflicts with its terminal result"
                    )
                return FinalizationResult(
                    delivery_uuid, outcome, outcome == "accepted", False,
                )
            if (
                row["state"] != "submitting"
                or str(row["lease_token"]) != lease_uuid
            ):
                raise SchedulerConflict(
                    "delivery is not owned by this active submission lease"
                )
            return self._finalize_locked(
                conn, row=row, outcome=outcome, as_of=finished,
                provider_message_id=provider_message_id,
                error_code=error_code,
            )

    def reconcile_ambiguous(
        self,
        *,
        delivery_id: str,
        outcome: Literal["accepted", "permanent_failure"],
        as_of: Instant,
        provider_message_id: str = "",
        error_code: str = "",
    ) -> FinalizationResult:
        """Resolve an ambiguous operation from provider evidence without resending."""
        delivery_uuid = _uuid(delivery_id, field="delivery ID")
        reconciled = _instant(as_of, field="reconciliation as_of")
        provider_message_id, error_code = self._validate_provider_result(
            outcome=outcome,
            provider_message_id=provider_message_id,
            error_code=error_code,
        )
        with self._connection() as conn:
            self._lock_active_policy(conn)
            row = conn.execute(
                """SELECT delivery_id, chase_id, policy_revision_id,
                          policy_step_ordinal, contact_snapshot_id, channel,
                          template_id, state, provider_message_id, error_code
                     FROM chase_delivery_outbox
                    WHERE practice_id = %s AND delivery_id = %s::uuid
                    FOR UPDATE""",
                (self.practice_id, delivery_uuid),
            ).fetchone()
            if row is None:
                raise SchedulerNotFound("delivery does not exist")
            if row["state"] == outcome:
                if (
                    (row["provider_message_id"] or "") != provider_message_id
                    or (row["error_code"] or "") != error_code
                ):
                    raise SchedulerConflict(
                        "reconciliation conflicts with its terminal result"
                    )
                return FinalizationResult(
                    delivery_uuid, outcome, outcome == "accepted", False,
                )
            if row["state"] != "ambiguous":
                raise SchedulerConflict(
                    "only an ambiguous delivery can enter reconciliation"
                )
            return self._finalize_locked(
                conn, row=row, outcome=outcome, as_of=reconciled,
                provider_message_id=provider_message_id,
                error_code=error_code,
            )


__all__ = [
    "AmbiguousDelivery",
    "ApprovalRecord",
    "ContactRouteInput",
    "ContactSnapshotInput",
    "DetectionPersistenceResult",
    "DispatchContext",
    "DueReservation",
    "FinalizationResult",
    "PolicyRevisionRecord",
    "PostgresChaseSchedulerRepository",
    "ReleaseRecord",
    "SchedulerAuthorizationError",
    "SchedulerConflict",
    "SchedulerLeaseExpired",
    "SchedulerNotFound",
]
