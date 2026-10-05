"""Structured capture of the practice's Card Chase baseline (B5).

The Aug 12 synthesis (§8 Q2/Q9) reserved a one-time month-end window to capture
the practice's prior numbers — cards chased per month, touches per card, minutes
per card, cards outstanding — and it passed uninstrumented (biller follow-up,
Aug 13). :func:`cosmaneura_billing.chase.metrics.baseline_comparison` already
accepts ad-hoc baseline kwargs; this module gives those numbers a validated,
PHI-free record with provenance so they can be captured once, stored, and
compared against the tool's own measured metrics.

No number is invented. Any metric the practice does not provide stays ``None``
and its delta stays ``None``, so a missing baseline remains visibly missing
rather than becoming a figure someone quotes back later. For minutes, touches,
and outstanding, a lower measured number than baseline is the improvement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .metrics import summarize
from .models import ChaseItem


class BaselineValidationError(ValueError):
    """The captured baseline is empty or malformed."""


def _non_negative_int(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BaselineValidationError(f"{field} must be a non-negative integer")
    return value


def _non_negative_number(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise BaselineValidationError(f"{field} must be a non-negative number")
    return float(value)


@dataclass(frozen=True)
class ChaseBaseline:
    """The practice's prior card-chase numbers, captured once with provenance.

    All four metrics are optional so a practice can report whatever it has, but
    at least one must be present (an all-empty baseline is meaningless).
    """

    captured_at: str
    captured_by: str
    cards_chased_per_month: Optional[int] = None
    touches_per_card: Optional[float] = None
    minutes_per_card: Optional[float] = None
    cards_outstanding: Optional[int] = None
    source: str = "practice_report"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "captured_by": self.captured_by,
            "source": self.source,
            "notes": self.notes,
            "cards_chased_per_month": self.cards_chased_per_month,
            "touches_per_card": self.touches_per_card,
            "minutes_per_card": self.minutes_per_card,
            "cards_outstanding": self.cards_outstanding,
        }

    def to_baseline_comparison_kwargs(self) -> dict[str, Any]:
        """Feed :func:`metrics.baseline_comparison` without duplicating it."""
        return {
            "baseline_minutes_per_card": self.minutes_per_card,
            "baseline_claims_held_monthly": self.cards_chased_per_month,
        }


def _default_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_baseline(
    *,
    captured_by: str,
    cards_chased_per_month: Any = None,
    touches_per_card: Any = None,
    minutes_per_card: Any = None,
    cards_outstanding: Any = None,
    source: str = "practice_report",
    notes: str = "",
    captured_at: Optional[str] = None,
    now: Optional[Any] = None,
) -> ChaseBaseline:
    """Validate and capture a practice baseline.

    ``captured_by`` is a PHI-free actor label (an Entra object ID or role), never
    a patient reference. ``now`` is injectable for deterministic capture.
    """
    if not isinstance(captured_by, str) or not captured_by.strip():
        raise BaselineValidationError("captured_by is required")
    if not isinstance(source, str) or not source.strip():
        raise BaselineValidationError("source is required")
    if not isinstance(notes, str):
        raise BaselineValidationError("notes must be a string")

    cards = _non_negative_int(cards_chased_per_month, "cards_chased_per_month")
    touches = _non_negative_number(touches_per_card, "touches_per_card")
    minutes = _non_negative_number(minutes_per_card, "minutes_per_card")
    outstanding = _non_negative_int(cards_outstanding, "cards_outstanding")

    if all(v is None for v in (cards, touches, minutes, outstanding)):
        raise BaselineValidationError(
            "a baseline must carry at least one metric; the month-end window "
            "(synthesis §8 Q2/Q9) numbers are still required")

    resolved_at = captured_at if captured_at is not None else (now or _default_now)()
    if not isinstance(resolved_at, str) or not resolved_at.strip():
        raise BaselineValidationError("captured_at must be a non-empty timestamp")

    return ChaseBaseline(
        captured_at=resolved_at,
        captured_by=captured_by,
        cards_chased_per_month=cards,
        touches_per_card=touches,
        minutes_per_card=minutes,
        cards_outstanding=outstanding,
        source=source,
        notes=notes,
    )


def _delta(measured: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if measured is None or baseline is None:
        return None
    return round(measured - baseline, 2)


def compare(baseline: ChaseBaseline, measured: dict[str, Any]) -> dict[str, Any]:
    """Compare a captured baseline against a ``metrics.summarize`` result.

    Every delta is ``None`` unless both sides are present. For minutes, touches,
    and outstanding, a negative delta means the tool improved on the baseline.
    """
    minutes_delta = _delta(
        measured.get("staff_minutes_per_card"), baseline.minutes_per_card)
    touches_delta = _delta(
        measured.get("median_touches_to_card"), baseline.touches_per_card)
    outstanding_delta = _delta(
        measured.get("outstanding"), baseline.cards_outstanding)
    return {
        "baseline": baseline.to_dict(),
        "measured": measured,
        "deltas": {
            # lower is better for all three
            "minutes_per_card_delta": minutes_delta,
            "touches_per_card_delta": touches_delta,
            "cards_outstanding_delta": outstanding_delta,
        },
        # windows differ (baseline is monthly; measured is the observed set), so
        # this is echoed for context rather than differenced.
        "cards_chased_per_month_baseline": baseline.cards_chased_per_month,
        "measured_chases_in_window": measured.get("chases"),
    }


def compare_items(baseline: ChaseBaseline, items: Iterable[ChaseItem]) -> dict[str, Any]:
    """Convenience: summarize the contact log, then compare."""
    return compare(baseline, summarize(items))


__all__ = [
    "BaselineValidationError",
    "ChaseBaseline",
    "compare",
    "compare_items",
    "record_baseline",
]
