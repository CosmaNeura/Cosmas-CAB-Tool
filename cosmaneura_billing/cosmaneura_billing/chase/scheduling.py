"""Pure scheduling rules for the insurance-data chase worker.

This module decides *when* one configured policy step may run and whether that
step is an automated transport or a staff task.  It deliberately performs no
I/O: persistence owns leases and idempotency, while a dispatcher owns provider
calls.  Keeping calendar and eligibility decisions here makes both layers use
the same fail-closed rules.

Cadence offsets are absolute local calendar days after detection.  They are not
cumulative delays from the previous touch.  For example, the default 0/1/3/5
cadence is always calculated from the detection date in the practice's IANA
time zone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
import re
from typing import Optional, Tuple, Union
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import TERMINAL, ChaseItem, Channel, State
from .policy import ApprovalMode, ChasePolicy, Step
from .policy_fingerprint import DeliveryConfiguration, policy_fingerprint


Instant = Union[str, datetime]
QuietHours = Tuple[int, int]


class ScheduleConfigurationError(ValueError):
    """The scheduler cannot safely interpret its time configuration."""


class ScheduleAction(str, Enum):
    """The scheduler's pure decision for one policy step."""

    BLOCKED = "blocked"
    WAIT = "wait"
    TRANSPORT = "transport"
    STAFF_TASK = "staff_task"


class ScheduleReason(str, Enum):
    """Stable machine reasons; persistence must never branch on prose."""

    ITEM_TERMINAL = "item_terminal"
    AWAITING_VERIFICATION = "awaiting_verification"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    STAFF_TASK_PENDING = "staff_task_pending"
    DURABLE_EVIDENCE_MISSING = "durable_evidence_missing"
    POLICY_INACTIVE = "policy_inactive"
    EVIDENCE_MALFORMED = "evidence_malformed"
    POLICY_FINGERPRINT_MISMATCH = "policy_fingerprint_mismatch"
    SCHEDULER_DISABLED = "scheduler_disabled"
    STEP_IDENTITY_MISMATCH = "step_identity_mismatch"
    GATE_EVIDENCE_CHANGED = "gate_evidence_changed"
    GATE_RELEASE_INVALID = "gate_release_invalid"
    GATE_NOT_CLEARED = "gate_not_cleared"
    MESSAGE_RELEASE_MISSING = "message_release_missing"
    MESSAGE_RELEASE_UNEXPECTED = "message_release_unexpected"
    CONTACT_TIMESTAMP_FUTURE = "contact_timestamp_future"
    CONTACT_SNAPSHOT_STALE = "contact_snapshot_stale"
    NOT_DUE = "not_due"
    STAFF_TASK_READY = "staff_task_ready"
    MESSAGING_DISABLED = "messaging_disabled"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    CONSENT_REQUIRED = "consent_required"
    CHANNEL_NOT_AUTOMATED = "channel_not_automated"
    TRANSPORT_READY = "transport_ready"


@dataclass(frozen=True)
class ScheduleDecision:
    """A side-effect-free scheduling decision.

    ``TRANSPORT`` is the only action an automated dispatcher may submit to a
    provider.  ``STAFF_TASK`` is intentionally separate so a phone step can be
    persisted on the staff worklist without ever reaching a voice transport.
    """

    action: ScheduleAction
    reason_code: ScheduleReason
    reason: str
    cadence_due_at: Optional[datetime] = None
    next_permitted_at: Optional[datetime] = None

    @property
    def automated_dispatch_eligible(self) -> bool:
        return self.action is ScheduleAction.TRANSPORT

    @property
    def staff_task_due(self) -> bool:
        return self.action is ScheduleAction.STAFF_TASK

    @property
    def skippable_without_touch(self) -> bool:
        """True only for a cadence rung that cannot use a deployed route.

        Approval, direct-contract, contact freshness, and kill-switch failures
        are intentionally absent. A worker may move past an unavailable channel
        without counting a touch, but it may never skip a safety gate.
        """
        return (
            self.action is ScheduleAction.BLOCKED
            and self.reason_code in {
                ScheduleReason.CHANNEL_UNAVAILABLE,
                ScheduleReason.CONSENT_REQUIRED,
            }
        )


@dataclass(frozen=True)
class DurableScheduleEvidence:
    """Typed evidence loaded from the durable approval/contact/gate joins."""

    policy_revision_id: str
    policy_step_ordinal: int
    approval_event_id: str
    approved_policy_fingerprint: str
    contact_snapshot_id: str
    contact_verified_at: Instant
    current_gate_fingerprint: str
    reserved_gate_fingerprint: str
    gate_disposition: str
    review_release_id: str = ""
    message_release_id: str = ""
    active: bool = False


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _uuid(value: str) -> bool:
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _practice_zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name or name != name.strip():
        raise ScheduleConfigurationError(
            "practice_timezone must be a non-empty IANA time-zone name"
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleConfigurationError(
            f"unknown IANA practice timezone: {name!r}"
        ) from exc


def _aware(value: Instant, *, field: str) -> datetime:
    if isinstance(value, str):
        encoded = value[:-1] + "+00:00" if value.endswith("Z") else value
        parse_failed = False
        try:
            parsed = datetime.fromisoformat(encoded)
        except ValueError:
            parse_failed = True
            parsed = None
        if parse_failed:
            # A malformed persisted/request value can contain identifiers. Do
            # not retain it through the parser exception's context chain.
            raise ScheduleConfigurationError(
                f"{field} must be an ISO-8601 instant with an offset"
            ) from None
        assert parsed is not None
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ScheduleConfigurationError(
            f"{field} must be a datetime or ISO-8601 string"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScheduleConfigurationError(
            f"{field} must include a UTC offset; naive wall times are unsafe"
        )
    return parsed


def _quiet_hours(value: QuietHours) -> QuietHours:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ScheduleConfigurationError(
            "quiet_hours must contain start and end hours"
        )
    start, end = value
    if (type(start) is not int or type(end) is not int
            or not 0 <= start <= 23 or not 0 <= end <= 23):
        raise ScheduleConfigurationError(
            "quiet_hours values must be integer hours from 0 through 23"
        )
    if start == end:
        raise ScheduleConfigurationError(
            "equal quiet-hours boundaries are ambiguous; configure a real window"
        )
    return start, end


def _valid_wall_time(wall: datetime, zone: ZoneInfo, fold: int) -> Optional[datetime]:
    candidate = wall.replace(tzinfo=zone, fold=fold)
    round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
    if (round_trip.replace(tzinfo=None) == wall and round_trip.fold == fold):
        return candidate
    return None


def _resolve_wall_time(day: date, clock: time, zone: ZoneInfo) -> datetime:
    """Attach an IANA zone without manufacturing an imaginary DST instant.

    Ambiguous fall-back times select their first occurrence.  A spring-forward
    wall time that does not exist is shifted forward by the size of the gap,
    matching calendar scheduling semantics (02:30 becomes 03:30 in a one-hour
    gap).
    """

    wall = datetime.combine(day, clock.replace(tzinfo=None))
    candidates = [
        candidate
        for fold in (0, 1)
        if (candidate := _valid_wall_time(wall, zone, fold)) is not None
    ]
    if candidates:
        return min(candidates, key=lambda value: value.astimezone(timezone.utc))

    # zoneinfo intentionally does not reject imaginary wall times.  Round-trip
    # both possible folds and select the first real local time after the gap.
    resolved = []
    for fold in (0, 1):
        value = (wall.replace(tzinfo=zone, fold=fold)
                 .astimezone(timezone.utc).astimezone(zone))
        if value.replace(tzinfo=None) > wall:
            resolved.append(value)
    if not resolved:  # Defensive: every IANA forward transition has an end.
        raise ScheduleConfigurationError(
            f"could not resolve local time {wall.isoformat()} in {zone.key}"
        )
    return min(resolved, key=lambda value: value.replace(tzinfo=None))


def _inside_quiet_hours(local: datetime, quiet_hours: QuietHours) -> bool:
    start, end = quiet_hours
    clock = local.time().replace(tzinfo=None)
    start_at, end_at = time(start), time(end)
    if start < end:  # e.g. 08:00 through 17:00
        return start_at <= clock < end_at
    # Wraparound, e.g. 21:00 through 08:00 the next morning.
    return clock >= start_at or clock < end_at


def _after_quiet_hours(
    local: datetime, *, zone: ZoneInfo, quiet_hours: QuietHours,
) -> datetime:
    if not _inside_quiet_hours(local, quiet_hours):
        return local

    start, end = quiet_hours
    end_day = local.date()
    if start > end and local.time().replace(tzinfo=None) >= time(start):
        end_day += timedelta(days=1)
    return _resolve_wall_time(end_day, time(end), zone)


def cadence_due_at(
    detected_at: Instant,
    *,
    day_offset: int,
    practice_timezone: str,
    quiet_hours: QuietHours,
) -> datetime:
    """Return the stable UTC due instant for an absolute cadence offset.

    The local wall-clock time of detection is retained on each configured
    calendar day, then moved to the end of quiet hours if necessary.  Offset
    zero preserves the exact detection instant (including the fold of an
    ambiguous fall-back time).
    """

    if type(day_offset) is not int or day_offset < 0:
        raise ScheduleConfigurationError(
            "cadence day_offset must be a non-negative integer"
        )
    zone = _practice_zone(practice_timezone)
    quiet = _quiet_hours(quiet_hours)
    detected = _aware(detected_at, field="detected_at").astimezone(zone)

    if day_offset == 0:
        local_due = detected
    else:
        local_due = _resolve_wall_time(
            detected.date() + timedelta(days=day_offset),
            detected.time().replace(tzinfo=None),
            zone,
        )
    permitted = _after_quiet_hours(local_due, zone=zone, quiet_hours=quiet)
    return permitted.astimezone(timezone.utc)


def next_permitted_due_at(
    due_at: Instant,
    *,
    as_of: Instant,
    practice_timezone: str,
    quiet_hours: QuietHours,
) -> datetime:
    """Return the first UTC instant at or after both ``due_at`` and ``as_of``.

    This second quiet-hours check matters for overdue work: a step due at noon
    must not be sent by a retry that happens to run at 22:00.
    """

    zone = _practice_zone(practice_timezone)
    quiet = _quiet_hours(quiet_hours)
    due = _aware(due_at, field="due_at").astimezone(timezone.utc)
    current = _aware(as_of, field="as_of").astimezone(timezone.utc)
    candidate = max(due, current).astimezone(zone)
    permitted = _after_quiet_hours(candidate, zone=zone, quiet_hours=quiet)
    return permitted.astimezone(timezone.utc)


def evaluate_step(
    item: ChaseItem,
    policy: ChasePolicy,
    step: Step,
    *,
    now: Instant,
    practice_timezone: str,
    authorization: DurableScheduleEvidence | None = None,
    delivery: DeliveryConfiguration | None = None,
    authoritative_sms_consent: bool = False,
) -> ScheduleDecision:
    """Decide whether one configured step may run now.

    ``authoritative_sms_consent`` must come from a durable, authoritative opt-in
    feed.  It intentionally does not accept a ``CoverageSnapshot``: the current
    request payload's ``sms_consent`` flag is not such a feed and must not turn
    SMS on accidentally.  With the default ``False``, email remains usable and
    SMS remains fail-closed.
    """

    if item.state in TERMINAL:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.ITEM_TERMINAL,
            f"item is terminal ({item.state.value})",
        )
    if item.state is State.SUBMITTED:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.AWAITING_VERIFICATION,
            "patient submission is awaiting verification",
        )
    if item.state is State.NEEDS_REVIEW:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.HUMAN_REVIEW_REQUIRED,
            "item requires human review",
        )
    if item.pending_task is not None:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.STAFF_TASK_PENDING,
            "item already has a pending staff task",
        )
    if authorization is None:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.DURABLE_EVIDENCE_MISSING,
            "durable policy, contact, and gate evidence is absent",
        )
    if authorization.active is not True:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.POLICY_INACTIVE,
            "policy version is not active",
        )
    if (
        not _uuid(authorization.policy_revision_id)
        or not _uuid(authorization.approval_event_id)
        or not _uuid(authorization.contact_snapshot_id)
        or not _SHA256.fullmatch(authorization.approved_policy_fingerprint)
        or not _SHA256.fullmatch(authorization.current_gate_fingerprint)
        or not _SHA256.fullmatch(authorization.reserved_gate_fingerprint)
    ):
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.EVIDENCE_MALFORMED,
            "durable scheduling evidence is malformed",
        )

    delivery = delivery or DeliveryConfiguration()
    executable_fingerprint = policy_fingerprint(
        policy, practice_timezone=practice_timezone, delivery=delivery,
    )
    if executable_fingerprint != authorization.approved_policy_fingerprint:
        return ScheduleDecision(
            ScheduleAction.BLOCKED,
            ScheduleReason.POLICY_FINGERPRINT_MISMATCH,
            "current executable policy does not match the exact approval",
        )
    if delivery.scheduler_enabled is not True:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.SCHEDULER_DISABLED,
            "durable chase scheduler is disabled",
        )
    if (
        type(authorization.policy_step_ordinal) is not int
        or not 0 <= authorization.policy_step_ordinal < len(policy.steps)
        or authorization.policy_step_ordinal >= policy.max_touches
        or policy.steps[authorization.policy_step_ordinal] != step
    ):
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.STEP_IDENTITY_MISMATCH,
            "policy step is outside the approved cadence",
        )
    if (
        authorization.current_gate_fingerprint
        != authorization.reserved_gate_fingerprint
    ):
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.GATE_EVIDENCE_CHANGED,
            "direct-contract gate evidence changed",
        )
    if authorization.gate_disposition == "clear":
        if authorization.review_release_id:
            return ScheduleDecision(
                ScheduleAction.BLOCKED, ScheduleReason.GATE_RELEASE_INVALID,
                "clear gate has an invalid review release",
            )
    elif authorization.gate_disposition == "released":
        if not _uuid(authorization.review_release_id):
            return ScheduleDecision(
                ScheduleAction.BLOCKED, ScheduleReason.GATE_RELEASE_INVALID,
                "blocked gate has no exact review release",
            )
    else:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.GATE_NOT_CLEARED,
            "direct-contract gate is not cleared",
        )
    if policy.approval_mode is ApprovalMode.PER_MESSAGE:
        if not _uuid(authorization.message_release_id):
            return ScheduleDecision(
                ScheduleAction.BLOCKED, ScheduleReason.MESSAGE_RELEASE_MISSING,
                "per-message policy step has no durable exact release",
            )
    elif authorization.message_release_id:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.MESSAGE_RELEASE_UNEXPECTED,
            "per-policy cadence has an unexpected message release",
        )

    current = _aware(now, field="now").astimezone(timezone.utc)
    verified_at = _aware(
        authorization.contact_verified_at, field="contact_verified_at",
    ).astimezone(timezone.utc)
    if verified_at > current:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.CONTACT_TIMESTAMP_FUTURE,
            "contact snapshot timestamp is in the future",
        )
    if current - verified_at > timedelta(
        hours=delivery.contact_snapshot_max_age_hours,
    ):
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.CONTACT_SNAPSHOT_STALE,
            "verified contact snapshot is stale",
        )
    cadence_due = cadence_due_at(
        item.detected_at,
        day_offset=step.day_offset,
        practice_timezone=practice_timezone,
        quiet_hours=policy.quiet_hours,
    )
    permitted = next_permitted_due_at(
        cadence_due,
        as_of=current,
        practice_timezone=practice_timezone,
        quiet_hours=policy.quiet_hours,
    )
    if current < permitted:
        return ScheduleDecision(
            ScheduleAction.WAIT, ScheduleReason.NOT_DUE,
            "step is not due outside quiet hours",
            cadence_due_at=cadence_due,
            next_permitted_at=permitted,
        )

    if step.channel is Channel.PHONE:
        return ScheduleDecision(
            ScheduleAction.STAFF_TASK, ScheduleReason.STAFF_TASK_READY,
            "phone contact is a staff task and never an automated transport",
            cadence_due_at=cadence_due,
            next_permitted_at=permitted,
        )
    if step.channel not in frozenset(delivery.enabled_channels):
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.CHANNEL_UNAVAILABLE,
            f"{step.channel.value} has no enabled production transport",
            cadence_due_at=cadence_due,
            next_permitted_at=permitted,
        )
    if delivery.operational_enabled is not True:
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.MESSAGING_DISABLED,
            "patient messaging operational latch is off",
            cadence_due_at=cadence_due,
            next_permitted_at=permitted,
        )
    if (step.channel is Channel.SMS
            and authoritative_sms_consent is not True):
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.CONSENT_REQUIRED,
            "SMS has no authoritative explicit consent record",
            cadence_due_at=cadence_due,
            next_permitted_at=permitted,
        )
    if not policy.is_system_send(step.channel):
        return ScheduleDecision(
            ScheduleAction.BLOCKED, ScheduleReason.CHANNEL_NOT_AUTOMATED,
            f"{step.channel.value} is not an automated transport channel",
            cadence_due_at=cadence_due,
            next_permitted_at=permitted,
        )
    return ScheduleDecision(
        ScheduleAction.TRANSPORT, ScheduleReason.TRANSPORT_READY,
        f"{step.channel.value} step is due",
        cadence_due_at=cadence_due,
        next_permitted_at=permitted,
    )
