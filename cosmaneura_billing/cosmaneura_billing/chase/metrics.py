"""Chase metrics — the numbers the month-end close was supposed to produce.

The Aug 12 plan reserved a one-time window at month-end to capture the baseline:
cards chased per month, touches per card, minutes per card, cards outstanding.
The Aug 13 follow-up came back with the workflow but none of the numbers, and the
window passed. So these are instrumented from the first row rather than measured
retrospectively — after a week of real use the tool has its own baseline, and the
comparison stops depending on anyone remembering.

Every figure here is derived from the contact log. Nothing is self-reported by the
system about its own effectiveness: ``verified_closed`` counts rows a human
verified, and ``staff_minutes_per_card`` counts minutes a human entered.
"""
from __future__ import annotations

from statistics import median
from typing import Any, Iterable, Optional

from .models import ChaseItem, State


def _days_between(a: str, b: str) -> Optional[float]:
    """Elapsed days, or None when either end is unusable.

    Timestamps reach this from two places — a detector given a bare date and a
    runner given a full offset-aware clock — so awareness is normalised rather
    than assumed. A metric must never be the thing that raises: an unparseable
    pair drops out of the median instead of taking down the summary.
    """
    from datetime import datetime, timezone
    try:
        start, end = datetime.fromisoformat(a), datetime.fromisoformat(b)
        if (start.tzinfo is None) != (end.tzinfo is None):
            start = start.replace(tzinfo=start.tzinfo or timezone.utc)
            end = end.replace(tzinfo=end.tzinfo or timezone.utc)
        return round((end - start).total_seconds() / 86400, 2)
    except (ValueError, TypeError):
        return None


def summarize(items: Iterable[ChaseItem]) -> dict[str, Any]:
    items = list(items)
    if not items:
        return {"chases": 0}

    closed_verified = [i for i in items if i.state == State.VERIFIED_CLOSED]
    open_items = [i for i in items if i.is_open]
    resolved = [i for i in items if not i.is_open]

    times = [d for i in closed_verified
             if (d := _days_between(i.detected_at, i.closed_at)) is not None]
    touches = [i.touches for i in closed_verified]
    minutes = [i.staff_minutes for i in items]

    per_channel: dict[str, int] = {}
    for item in items:
        for attempt in item.attempts:
            key = attempt.channel.value
            per_channel[key] = per_channel.get(key, 0) + 1

    return {
        "chases": len(items),
        # The headline: how many are still waiting on insurance information.
        "outstanding": len(open_items),
        "verified_closed": len(closed_verified),
        "escalated": sum(1 for i in items if i.state == State.ESCALATED),
        "unreachable": sum(1 for i in items if i.state == State.UNREACHABLE),
        # Of the rows that reached an outcome, how many ended in a verified card.
        "completion_rate": (round(len(closed_verified) / len(resolved), 3)
                            if resolved else None),
        "median_days_to_card": round(median(times), 2) if times else None,
        "median_touches_to_card": round(median(touches), 2) if touches else None,
        "staff_minutes_total": round(sum(minutes), 2),
        # The number that says whether this took work off the two people who do
        # this job today.
        "staff_minutes_per_card": (round(sum(minutes) / len(closed_verified), 2)
                                   if closed_verified else None),
        "touches_by_channel": per_channel,
        # How much of the work the system did unattended. Reported as a share of
        # touches, not as a claim about time saved.
        "automated_touch_share": _automated_share(items),
        "needs_person": sum(1 for i in open_items if i.state == State.NEEDS_REVIEW),
    }


def _automated_share(items: list[ChaseItem]) -> Optional[float]:
    total = sum(i.touches for i in items)
    if not total:
        return None
    system = sum(1 for i in items for a in i.attempts if a.actor == "system")
    return round(system / total, 3)


def baseline_comparison(
    items: Iterable[ChaseItem],
    *,
    baseline_minutes_per_card: Optional[float] = None,
    baseline_claims_held_monthly: Optional[int] = None,
) -> dict[str, Any]:
    """Compare measured performance against the practice's own prior numbers.

    Both baselines are optional and both are currently unknown — the only figure
    in the corpus is 300–500 claims held monthly, from the Aug 6 call. This
    returns ``None`` for anything it cannot compute rather than estimating, so a
    missing baseline stays visibly missing instead of becoming a number somebody
    quotes back later.
    """
    measured = summarize(items)
    out: dict[str, Any] = {"measured": measured, "baseline_available": False}
    if baseline_minutes_per_card and measured.get("staff_minutes_per_card") is not None:
        out["baseline_available"] = True
        out["minutes_per_card_delta"] = round(
            measured["staff_minutes_per_card"] - baseline_minutes_per_card, 2)
    if baseline_claims_held_monthly:
        out["baseline_available"] = True
        out["claims_held_baseline"] = baseline_claims_held_monthly
    if not out["baseline_available"]:
        out["note"] = (
            "No practice baseline on record. The month-end window reserved for it "
            "(requirements synthesis §8 Q2, Q9) passed without the numbers being "
            "captured — re-ask for cards chased per month, touches per card, "
            "minutes per card, and cards outstanding right now.")
    return out
