"""F9a — missing-insurance detector. Deterministic; every trigger is a pure
rule with tests. Runs against the athenaNet interface (mock today).

Triggers (synthesis §F9a):
  * ``no_card_on_file``      new/upcoming appointment, no insurance package
  * ``expired``              coverage effective_to in the past / plan rollover
  * ``eligibility_inactive`` 271 returned INACTIVE
  * ``eligibility_timeout``  271 timed out at the payer
  * ``manual``               staff flag ("patient says new insurance")
  * ``denial_coverage``      (later) DenialRiskAgent-fed reason codes

F3 consult at detection: a direct-contract match (or an unverifiable list —
fail closed) routes the item to a **human-review bucket**, never the
auto-sequence, until the Aug 13 policy answer (§8 Q11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..athena_client import (
    ELIGIBILITY_INACTIVE, ELIGIBILITY_TIMEOUT, MockAthenaNet, PatientRecord,
)
from ..guards import PracticeListSource, check_direct_contract

TRIGGER_NO_CARD = "no_card_on_file"
TRIGGER_EXPIRED = "expired"
TRIGGER_INACTIVE = "eligibility_inactive"
TRIGGER_TIMEOUT = "eligibility_timeout"
TRIGGER_MANUAL = "manual"

STATE_OPEN = "open"
STATE_HUMAN_REVIEW = "human_review"


@dataclass
class ChaseItem:
    """Queue-item contract (synthesis §F9a)."""
    chase_id: str
    patient_ref: str
    trigger: str
    detected_at: str
    appointment_at: Optional[str]
    priority: str                      # P0 <2d | P1 <7d | P2 else/none
    direct_contract: dict = field(default_factory=dict)
    sequence_state: str = STATE_OPEN
    attempts: list[dict] = field(default_factory=list)
    resolution: Optional[dict] = None
    queue_type: str = "card_chase"

    def to_json(self) -> dict:
        return {
            "chase_id": self.chase_id, "patient_ref": self.patient_ref,
            "trigger": self.trigger, "detected_at": self.detected_at,
            "appointment_at": self.appointment_at, "priority": self.priority,
            "direct_contract": self.direct_contract,
            "sequence_state": self.sequence_state, "attempts": self.attempts,
            "resolution": self.resolution,
        }


# --------------------------------------------------------------------- #
# pure rules — each returns a trigger name or None
# --------------------------------------------------------------------- #
def rule_no_card(p: PatientRecord, now: datetime) -> Optional[str]:
    if p.insurance is None and _next_appointment(p, now) is not None:
        return TRIGGER_NO_CARD
    return None


def rule_expired(p: PatientRecord, now: datetime) -> Optional[str]:
    ins = p.insurance
    if ins is None:
        return None
    if ins.status == "expired":
        return TRIGGER_EXPIRED
    if ins.effective_to is not None and ins.effective_to < now.date().isoformat():
        return TRIGGER_EXPIRED
    return None


def rule_eligibility(p: PatientRecord, athena: MockAthenaNet,
                     now: datetime) -> Optional[str]:
    ins = p.insurance
    if ins is None or ins.status == "expired":
        return None                      # covered by the earlier rules
    resp = athena.check_eligibility(p.patient_ref, now=now)
    if resp.status == ELIGIBILITY_INACTIVE:
        return TRIGGER_INACTIVE
    if resp.status == ELIGIBILITY_TIMEOUT:
        return TRIGGER_TIMEOUT
    return None


def _next_appointment(p: PatientRecord, now: datetime) -> Optional[str]:
    upcoming = sorted(a.scheduled_at for a in p.appointments
                      if a.scheduled_at >= now.isoformat())
    return upcoming[0] if upcoming else None


def _priority(appointment_at: Optional[str], now: datetime) -> str:
    if appointment_at is None:
        return "P2"
    days = (datetime.fromisoformat(appointment_at) - now).total_seconds() / 86400
    if days < 2:
        return "P0"
    if days < 7:
        return "P1"
    return "P2"


# --------------------------------------------------------------------- #
# the detector
# --------------------------------------------------------------------- #
def detect(athena: MockAthenaNet, *, list_source: PracticeListSource | None = None,
           now: Optional[datetime] = None,
           manual_flags: tuple[str, ...] = ()) -> list[ChaseItem]:
    """One pass over the practice. Precedence per patient:
    manual > no_card > expired > eligibility. One item per patient."""
    now = now or athena.now
    source = list_source if list_source is not None else PracticeListSource()
    items: list[ChaseItem] = []

    for p in athena.list_patients():
        trigger = None
        if p.patient_ref in manual_flags:
            trigger = TRIGGER_MANUAL
        trigger = trigger or rule_no_card(p, now) or rule_expired(p, now) \
            or rule_eligibility(p, athena, now)
        if trigger is None:
            continue

        gate = check_direct_contract(p.patient_ref, source, now=now)
        routed_to_review = gate.blocked   # match OR fail-closed ⇒ human review
        appt = _next_appointment(p, now)
        items.append(ChaseItem(
            chase_id=f"CH-{p.patient_ref}-{trigger}",
            patient_ref=p.patient_ref,
            trigger=trigger,
            detected_at=now.isoformat(),
            appointment_at=appt,
            priority=_priority(appt, now),
            direct_contract={
                "is_direct_contract": gate.is_direct_contract,
                "status": gate.status,
                "routed_to_review": routed_to_review,
            },
            sequence_state=STATE_HUMAN_REVIEW if routed_to_review else STATE_OPEN,
        ))

    order = {"P0": 0, "P1": 1, "P2": 2}
    items.sort(key=lambda i: (order[i.priority], i.appointment_at or "9999",
                              i.patient_ref))
    return items
