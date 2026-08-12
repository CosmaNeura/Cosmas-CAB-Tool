"""F9 metrics — instrumented from day one so tomorrow's month-end close
becomes the baseline these are measured against (synthesis §F9c)."""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Iterable

from .chassis import STATE_CLOSED
from .detector import ChaseItem, STATE_HUMAN_REVIEW


def summarize(items: Iterable[ChaseItem], staff_actions: list[dict]) -> dict:
    items = list(items)
    active = [i for i in items if i.sequence_state not in
              (STATE_CLOSED, "escalated_to_staff", "unreachable",
               STATE_HUMAN_REVIEW)]
    closed = [i for i in items if i.sequence_state == STATE_CLOSED]
    review = [i for i in items if i.sequence_state == STATE_HUMAN_REVIEW]
    chased = [i for i in items if i.attempts]

    hours_to_card = []
    for i in closed:
        if i.resolution and i.attempts:
            t0 = datetime.fromisoformat(i.detected_at)
            t1 = datetime.fromisoformat(i.resolution["closed_at"])
            hours_to_card.append((t1 - t0).total_seconds() / 3600)

    by_trigger: dict[str, int] = {}
    for i in items:
        by_trigger[i.trigger] = by_trigger.get(i.trigger, 0) + 1

    return {
        "cards_outstanding": len(active),
        "in_human_review": len(review),
        "completed": len(closed),
        "completion_rate": (len(closed) / len(chased)) if chased else 0.0,
        "attempts_per_completion": (
            sum(len(i.attempts) for i in closed) / len(closed)) if closed else 0.0,
        "median_hours_to_card": (statistics.median(hours_to_card)
                                 if hours_to_card else None),
        "staff_touches": len(staff_actions),
        "by_trigger": by_trigger,
        "baseline_note": ("Baseline (cards chased/month, minutes per card) "
                          "arrives with the Aug 13 month-end close."),
    }


def pretty_metrics(m: dict) -> str:
    lines = [
        "chase metrics",
        f"  cards outstanding      : {m['cards_outstanding']}",
        f"  in human review (F3)   : {m['in_human_review']}",
        f"  completed              : {m['completed']}",
        f"  completion rate        : {m['completion_rate']:.0%}",
        f"  attempts / completion  : {m['attempts_per_completion']:.1f}",
        f"  median hours to card   : "
        + (f"{m['median_hours_to_card']:.1f}" if m["median_hours_to_card"]
           is not None else "—"),
        f"  staff touches          : {m['staff_touches']}",
        "  by trigger             : "
        + ", ".join(f"{k}={v}" for k, v in sorted(m["by_trigger"].items())),
        f"  note: {m['baseline_note']}",
    ]
    return "\n".join(lines)
