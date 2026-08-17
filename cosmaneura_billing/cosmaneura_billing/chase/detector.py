"""F9a — the missing-insurance detector.

One deterministic rule per trigger, evaluated over a ``CoverageSnapshot``. No
model call: whether a card is on file is a fact the practice-management system
knows, not something to infer.

Every detected item is routed through the direct-contract gate before it can be
sequenced. A direct-contract patient — or one whose status is unknown — goes to
``NEEDS_REVIEW`` rather than into an automated sequence, and never disappears.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from ..direct_contract import DirectContractList, chase_routing
from .models import (
    TRIGGER_BUCKET, ChaseItem, CoverageSnapshot, State, Trigger,
)

# Rules are evaluated in this order and the first match wins. Ordering is a
# statement about which fact is most actionable: a payer telling us the coverage
# is inactive is more specific than us noticing there is no card image, so it
# leads — chasing a new card for an inactive plan asks the patient the wrong
# question.
_ORDER = (
    Trigger.DENIAL_COVERAGE,
    Trigger.ELIGIBILITY_INACTIVE,
    Trigger.ELIGIBILITY_TIMEOUT,
    Trigger.EXPIRED,
    Trigger.PLAN_YEAR_ROLLOVER,
    Trigger.MANUAL,
    Trigger.NO_CARD_ON_FILE,
)


def _expired(snap: CoverageSnapshot, today: str) -> bool:
    return bool(snap.coverage_end_date and snap.coverage_end_date < today)


def _rules(snap: CoverageSnapshot, today: str) -> dict[Trigger, bool]:
    return {
        Trigger.DENIAL_COVERAGE: bool(snap.denial_reason_code),
        Trigger.ELIGIBILITY_INACTIVE: snap.eligibility_status.upper() == "INACTIVE",
        Trigger.ELIGIBILITY_TIMEOUT: snap.eligibility_status.upper() == "TIMEOUT",
        Trigger.EXPIRED: _expired(snap, today),
        Trigger.PLAN_YEAR_ROLLOVER: snap.plan_year_rollover,
        Trigger.MANUAL: bool(snap.staff_flag),
        # The baseline case: nothing usable on file. Either no package at all, or
        # a package with no card image — the Aug 13 call was explicit that the
        # image has to be in the right encounter, so a package alone is not done.
        Trigger.NO_CARD_ON_FILE: not (snap.has_insurance_package and snap.card_image_on_file),
    }


def detect(
    snap: CoverageSnapshot,
    *,
    today: str,
    now: Optional[Callable[[], str]] = None,
    roster: Optional[DirectContractList] = None,
    chase_id: str = "",
) -> Optional[ChaseItem]:
    """One snapshot in, at most one worklist item out.

    ``today`` is passed in rather than read from a clock so detection is
    reproducible and testable.
    """
    fired = _rules(snap, today)
    trigger = next((t for t in _ORDER if fired[t]), None)
    if trigger is None:
        return None

    detected_at = (now or (lambda: today))()
    check, route = chase_routing(snap.patient_ref, roster=roster, now=now or (lambda: today))

    item = ChaseItem(
        chase_id=chase_id or f"chase-{snap.patient_ref}",
        patient_ref=snap.patient_ref,
        trigger=trigger,
        bucket=TRIGGER_BUCKET[trigger],
        detected_at=detected_at,
        appointment_at=snap.appointment_at,
        priority=priority_for(snap, today),
        direct_contract=check.to_dict(),
    )
    if route == "review":
        item.state = State.NEEDS_REVIEW
        item.review_reason = (
            "patient is on the direct-contract list — the practice does not bill "
            "their insurance, so confirm whether a card should be chased at all"
            if check.is_direct_contract else
            "direct-contract status could not be determined; a person must confirm "
            "before this patient is contacted"
        )
    return item


def priority_for(snap: CoverageSnapshot, today: str) -> int:
    """Higher is more urgent. Driven by appointment proximity.

    A missing card matters most just before the visit and after it has already
    happened — before, because it can still be fixed at check-in; after, because
    the claim is already being held. An appointment three weeks out is real work
    but it is not today's work.
    """
    if not snap.appointment_at:
        return 40                      # no appointment: steady backlog
    appt = snap.appointment_at[:10]
    if appt < today:
        return 90                      # visit happened, claim is held
    if appt == today:
        return 100                     # today — fixable at check-in
    days = _days_between(today, appt)
    if days <= 2:
        return 80
    if days <= 7:
        return 60
    return 30


def _days_between(a: str, b: str) -> int:
    from datetime import date
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except ValueError:
        return 999


def detect_all(
    snapshots: Iterable[CoverageSnapshot], *, today: str, **kw
) -> list[ChaseItem]:
    """Run the detector over a batch, highest priority first."""
    items = []
    for i, snap in enumerate(snapshots):
        item = detect(snap, today=today, chase_id=f"chase-{today}-{i:04d}", **kw)
        if item is not None:
            items.append(item)
    return sorted(items, key=lambda x: (-x.priority, x.patient_ref))
