"""F5 — self-pay chase, step-0 ONLY (synthesis §F5).

The billing team raised self-pay recovery unprompted (8/6), but the Aug 13
follow-up defines the mechanics — sequencing, tone, escalation, who approves
sends. Building behavior tonight would be invalidated tomorrow, so this module
deliberately contains **no outreach mechanics and no templates**: only the
data contract, the synthetic-balance generator, and the safety rails.

What IS locked tonight:
  * the ``SelfPayBalance`` wire contract (groundwork-brief-aligned);
  * F5 runs as a second queue type on the F9 chassis — same state machine,
    same contact log, same metrics;
  * **F3 is blocking here**: a direct-contract patient can never enter a
    self-pay sequence. The chassis re-checks at the door and refuses; this
    module's enqueue helper reports the refusal instead of raising. An
    unverifiable practice list blocks everything (fail closed).
  * items begin at ``draft`` — nothing moves without explicit staff approval.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..guards import PracticeListSource, check_direct_contract
from .chassis import ChaseQueue, QueueType, SequenceError, STATE_DRAFT
from .detector import ChaseItem

TRIGGER_SELF_PAY = "self_pay_balance"


@dataclass
class SelfPayBalance:
    """The §F5 draft contract, verbatim fields."""
    balance_id: str
    patient_ref: str
    amount_cents: int
    aging_days: int
    direct_contract_check: dict
    sequence_state: str = STATE_DRAFT
    contact_log: list = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "balance_id": self.balance_id, "patient_ref": self.patient_ref,
            "amount_cents": self.amount_cents, "aging_days": self.aging_days,
            "direct_contract_check": self.direct_contract_check,
            "sequence_state": self.sequence_state,
            "contact_log": self.contact_log,
        }


def generate_synthetic_balances(
    n: int, *, seed: int = 20260813, now: datetime,
    list_source: Optional[PracticeListSource] = None,
    include_direct_contract_ref: str = "PT-DC-0003",
) -> list[SelfPayBalance]:
    """Synthetic outstanding balances (mirrors the groundwork brief's mock
    plan). Deliberately seeds one direct-contract patient so every demo and
    test exercises the gate. Deterministic per seed."""
    rng = random.Random(seed)
    balances = []
    refs = [f"PT-{2000 + i}" for i in range(1, n)] + [include_direct_contract_ref]
    for i, ref in enumerate(refs, start=1):
        check = check_direct_contract(ref, list_source, now=now)
        balances.append(SelfPayBalance(
            balance_id=f"BAL-{i:04d}",
            patient_ref=ref,
            amount_cents=rng.randrange(2500, 40000, 25),
            aging_days=rng.randint(5, 120),
            direct_contract_check=check.to_json(),
        ))
    return balances


def enqueue_balances(
    queue: ChaseQueue, balances: list[SelfPayBalance], *, now: datetime,
) -> tuple[list[ChaseItem], list[dict]]:
    """Stage balances onto the shared chassis. Returns (accepted, blocked):
    blocked entries never entered the queue — the F3 hard stop, surfaced for
    humans rather than raised."""
    if queue.queue_type != QueueType.SELF_PAY:
        raise SequenceError("enqueue_balances requires a self_pay queue")
    accepted: list[ChaseItem] = []
    blocked: list[dict] = []
    for b in balances:
        item = ChaseItem(
            chase_id=f"CH-{b.patient_ref}-{TRIGGER_SELF_PAY}-{b.balance_id}",
            patient_ref=b.patient_ref,
            trigger=TRIGGER_SELF_PAY,
            detected_at=now.isoformat(),
            appointment_at=None,
            priority="P2",                     # priority weights: Aug 13 input
            direct_contract=b.direct_contract_check,
            sequence_state=STATE_DRAFT,
        )
        try:
            accepted.append(queue.enqueue(item, now=now))
            b.sequence_state = item.sequence_state
        except SequenceError as exc:
            blocked.append({"balance_id": b.balance_id,
                            "patient_ref": b.patient_ref,
                            "reason": str(exc)})
    return accepted, blocked
