"""Shared types for the insurance-data chase (F9).

Scope note
----------
This is not card *capture*. Capture — OCR, staff verification, the idempotent
write back into Athena — lives in the CardCapture workstream and already exists.
What was missing is the system that gets the card *to* capture in the first
place: a worklist of patients whose insurance data is missing, worked across
email, SMS, portal and phone until it lands.

Its users are not the coders. The corpus names Cindy and Maggie entering missing
cards into Athena after appointments; that is the job this replaces most of.
Grace's constraint — net coder minutes must go down — still binds, but it is not
the metric for *this* surface. These users are measured on cards closed and
time-to-card, which is why ``chase.metrics`` instruments those from day one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Trigger(str, Enum):
    """Why a patient landed on the worklist. One deterministic rule each."""
    NO_CARD_ON_FILE = "no_card_on_file"
    EXPIRED = "expired"
    ELIGIBILITY_INACTIVE = "eligibility_inactive"
    ELIGIBILITY_TIMEOUT = "eligibility_timeout"
    PLAN_YEAR_ROLLOVER = "plan_year_rollover"
    MANUAL = "manual"
    DENIAL_COVERAGE = "denial_coverage"


# What the front desk and billing staff already call these buckets, per the
# dashboard Grace saw on 2026-08-13 ("eligibility issues, expired cards, or
# missing payment information"). The worklist groups by these.
TRIGGER_BUCKET = {
    Trigger.NO_CARD_ON_FILE: "missing",
    Trigger.MANUAL: "missing",
    Trigger.EXPIRED: "expired",
    Trigger.PLAN_YEAR_ROLLOVER: "expired",
    Trigger.ELIGIBILITY_INACTIVE: "eligibility",
    Trigger.ELIGIBILITY_TIMEOUT: "eligibility",
    Trigger.DENIAL_COVERAGE: "eligibility",
}


class Channel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    PORTAL = "portal"
    PHONE = "phone"


# Which channels the system may send itself, and which are tasks for a person.
#
# Phone is a staff task, not an automated call. An outbound robocall about
# insurance is a TCPA problem and is not what "automated" should mean here; the
# integration people asked for is one list that also *tracks* calls, so the
# system schedules the call, a human makes it, and the outcome is logged in the
# same contact history as everything else. A telephony adapter can be added
# later without changing the state machine.
SYSTEM_SENDS = frozenset({Channel.EMAIL, Channel.PORTAL, Channel.SMS})
STAFF_TASK = frozenset({Channel.PHONE})

# Channels that require recorded consent before the first touch. SMS and voice
# carry TCPA exposure; email and portal messaging to an established patient do
# not, on the same footing. Consent policy is an Aug 12 open question (§8 Q10)
# the follow-up did not answer, so this fails closed.
REQUIRES_CONSENT = frozenset({Channel.SMS, Channel.PHONE})


class State(str, Enum):
    """Sequence state. Terminal states are the four at the end."""
    DETECTED = "detected"                # on the list, nothing sent
    NEEDS_REVIEW = "needs_review"        # a human must decide (direct-contract, no consent)
    READY = "ready"                      # policy approved, cleared to send
    IN_PROGRESS = "in_progress"          # at least one touch made
    SUBMITTED = "submitted"              # patient sent something; awaiting verification
    VERIFIED_CLOSED = "verified_closed"  # card verified and written — the win
    ESCALATED = "escalated"              # exhausted the sequence, needs a person
    UNREACHABLE = "unreachable"          # no working channel
    RESOLVED_OTHER = "resolved_other"    # closed for another reason (self-pay, cancelled)


TERMINAL = frozenset({State.VERIFIED_CLOSED, State.ESCALATED,
                      State.UNREACHABLE, State.RESOLVED_OTHER})


@dataclass(frozen=True)
class CoverageSnapshot:
    """What the practice-management system says about one patient + appointment.

    This is the adapter boundary. athenaNet, Acuity and the eligibility (271)
    response all reduce to this shape, so the detector is testable without
    credentials and the real integration is one adapter rather than a rewrite.
    """
    patient_ref: str
    appointment_at: str = ""             # ISO 8601; "" when not scheduled
    has_insurance_package: bool = False
    card_image_on_file: bool = False
    coverage_end_date: str = ""          # ISO date; "" when open-ended
    eligibility_status: str = ""         # ACTIVE | INACTIVE | TIMEOUT | ""
    plan_year_rollover: bool = False
    staff_flag: str = ""                 # free text, e.g. "patient says new insurance"
    denial_reason_code: str = ""         # CARC/RARC suggesting a coverage problem
    # Contact channels the practice actually holds for this patient.
    email: str = ""
    phone: str = ""
    portal_enrolled: bool = False
    sms_consent: bool = False
    voice_consent: bool = False

    def consented(self, channel: Channel) -> bool:
        if channel == Channel.SMS:
            return bool(self.sms_consent and self.phone)
        if channel == Channel.PHONE:
            return bool(self.voice_consent and self.phone)
        if channel == Channel.EMAIL:
            return bool(self.email)
        if channel == Channel.PORTAL:
            return bool(self.portal_enrolled)
        return False

    def reachable_on(self) -> list[Channel]:
        return [c for c in (Channel.EMAIL, Channel.PORTAL, Channel.SMS, Channel.PHONE)
                if self.consented(c)]


@dataclass
class Attempt:
    """One touch. The contact log is a list of these, and it is the audit trail."""
    channel: Channel
    template_id: str
    at: str
    # Who or what performed it: "system" for a send, a staff id for a call.
    actor: str = "system"
    outcome: str = "sent"          # sent | delivered | no_answer | left_message | bounced | declined
    # Staff-reported minutes, for the staff-minutes-per-card metric.
    minutes_spent: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value, "template_id": self.template_id,
            "at": self.at, "actor": self.actor, "outcome": self.outcome,
            "minutes_spent": self.minutes_spent, "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Attempt":
        """Rehydrate one persisted contact-log row.

        Persistence adapters deliberately go through this constructor instead
        of assigning raw database values to the dataclass.  Invalid enum values
        therefore fail closed at the storage boundary.
        """
        return cls(
            channel=Channel(value["channel"]),
            template_id=str(value["template_id"]),
            at=str(value["at"]),
            actor=str(value.get("actor") or "system"),
            outcome=str(value.get("outcome") or "sent"),
            minutes_spent=float(value.get("minutes_spent") or 0.0),
            note=str(value.get("note") or ""),
        )


@dataclass
class ChaseItem:
    """One patient's insurance-data chase — a row on the master worklist."""
    chase_id: str
    patient_ref: str
    trigger: Trigger
    detected_at: str
    appointment_at: str = ""
    state: State = State.DETECTED
    # Priority is computed from appointment proximity, not stored as an opinion.
    priority: int = 0
    bucket: str = "missing"
    direct_contract: dict[str, Any] = field(default_factory=dict)
    review_reason: str = ""
    assignee: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    # An open task waiting on a person (the phone step). While this is set the
    # runner will not issue another touch: the row is waiting on a human, and
    # re-issuing would put the same call on the worklist again tomorrow.
    pending_task: Optional[dict[str, Any]] = None
    resolution: Optional[dict[str, Any]] = None
    closed_at: str = ""

    @property
    def touches(self) -> int:
        return len(self.attempts)

    @property
    def staff_minutes(self) -> float:
        return round(sum(a.minutes_spent for a in self.attempts), 2)

    @property
    def is_open(self) -> bool:
        return self.state not in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "chase_id": self.chase_id,
            "patient_ref": self.patient_ref,
            "trigger": self.trigger.value,
            "bucket": self.bucket,
            "detected_at": self.detected_at,
            "appointment_at": self.appointment_at,
            "state": self.state.value,
            "priority": self.priority,
            "direct_contract": self.direct_contract,
            "review_reason": self.review_reason,
            "assignee": self.assignee,
            "touches": self.touches,
            "staff_minutes": self.staff_minutes,
            "pending_task": self.pending_task,
            "attempts": [a.to_dict() for a in self.attempts],
            "resolution": self.resolution,
            "closed_at": self.closed_at,
            "open": self.is_open,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChaseItem":
        """Rehydrate a chase row without trusting derived client fields."""
        return cls(
            chase_id=str(value["chase_id"]),
            patient_ref=str(value["patient_ref"]),
            trigger=Trigger(value["trigger"]),
            detected_at=str(value["detected_at"]),
            appointment_at=str(value.get("appointment_at") or ""),
            state=State(value.get("state") or State.DETECTED.value),
            priority=int(value.get("priority") or 0),
            bucket=str(value.get("bucket") or "missing"),
            direct_contract=dict(value.get("direct_contract") or {}),
            review_reason=str(value.get("review_reason") or ""),
            assignee=str(value.get("assignee") or ""),
            attempts=[Attempt.from_dict(row) for row in value.get("attempts", [])],
            pending_task=(dict(value["pending_task"])
                          if value.get("pending_task") is not None else None),
            resolution=(dict(value["resolution"])
                        if value.get("resolution") is not None else None),
            closed_at=str(value.get("closed_at") or ""),
        )
