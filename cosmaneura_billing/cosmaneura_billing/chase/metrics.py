"""F9 metrics — instrumented from day one so tomorrow's month-end close
becomes the baseline these are measured against (synthesis §F9c)."""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Iterable, Optional

from .chassis import STATE_CLOSED
from .detector import ChaseItem, STATE_HUMAN_REVIEW


def summarize(items: Iterable[ChaseItem], staff_actions: list[dict],
              machine_actions: Optional[list[dict]] = None) -> dict:
    """Chase metrics. Two definitions worth stating plainly, because the Aug 13
    baseline comparison depends on them:

    * ``completion_rate`` is measured over **chased** items only — items that
      received at least one outreach attempt. A patient who submits a card
      before any outreach closes the item but is counted separately as
      ``closed_without_outreach``, so self-serve submissions can never push
      the rate of the chase itself above 100%.
    * ``staff_touches`` counts **human** actions only. System-initiated
      auto-continue sends and auto-escalations are reported as
      ``machine_touches`` — the whole point of the mode is that staff minutes
      go down, so machine work must never be booked as staff work.
    """
    items = list(items)
    active = [i for i in items if i.sequence_state not in
              (STATE_CLOSED, "escalated_to_staff", "unreachable",
               STATE_HUMAN_REVIEW)]
    closed = [i for i in items if i.sequence_state == STATE_CLOSED]
    review = [i for i in items if i.sequence_state == STATE_HUMAN_REVIEW]
    chased = [i for i in items if i.attempts]
    chased_closed = [i for i in closed if i.attempts]

    hours_to_card = []
    for i in chased_closed:
        if i.resolution:
            t0 = datetime.fromisoformat(i.detected_at)
            t1 = datetime.fromisoformat(i.resolution["closed_at"])
            hours_to_card.append((t1 - t0).total_seconds() / 3600)

    by_trigger: dict[str, int] = {}
    for i in items:
        by_trigger[i.trigger] = by_trigger.get(i.trigger, 0) + 1

    human = [a for a in staff_actions
             if not str(a.get("staff_id", "")).startswith("auto:")]
    machine = [a for a in staff_actions
               if str(a.get("staff_id", "")).startswith("auto:")]
    machine += list(machine_actions or [])

    return {
        "cards_outstanding": len(active),
        "in_human_review": len(review),
        "completed": len(closed),
        "closed_without_outreach": len(closed) - len(chased_closed),
        "completion_rate": (len(chased_closed) / len(chased)) if chased else 0.0,
        "attempts_per_completion": (
            sum(len(i.attempts) for i in chased_closed) / len(chased_closed)
        ) if chased_closed else 0.0,
        "median_hours_to_card": (statistics.median(hours_to_card)
                                 if hours_to_card else None),
        "staff_touches": len(human),
        "machine_touches": len(machine),
        "by_trigger": by_trigger,
        "baseline_note": ("Baseline (cards chased/month, minutes per card) "
                          "arrives with the Aug 13 month-end close."),
    }


def pretty_metrics(m: dict) -> str:
    lines = [
        "chase metrics",
        f"  cards outstanding      : {m['cards_outstanding']}",
        f"  in human review (F3)   : {m['in_human_review']}",
        f"  completed              : {m['completed']}"
        + (f" ({m['closed_without_outreach']} self-serve, no outreach needed)"
           if m.get("closed_without_outreach") else ""),
        f"  completion rate        : {m['completion_rate']:.0%} (of chased)",
        f"  attempts / completion  : {m['attempts_per_completion']:.1f}",
        f"  median hours to card   : "
        + (f"{m['median_hours_to_card']:.1f}" if m["median_hours_to_card"]
           is not None else "—"),
        f"  staff touches (human)  : {m['staff_touches']}",
        f"  machine touches (auto) : {m.get('machine_touches', 0)}",
        "  by trigger             : "
        + ", ".join(f"{k}={v}" for k, v in sorted(m["by_trigger"].items())),
        f"  note: {m['baseline_note']}",
    ]
    return "\n".join(lines)
