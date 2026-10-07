"""Bounded, read-only presentation of a case's recorded contact attempts.

The legacy projection reads Attempt records; the communications adapter adds
one entry per durable dispatch start. A recorded outcome is not confirmation of patient contact
or delivery. Unusable records make the projection unavailable rather than
producing partial counts; unknown outcome text is never reflected to the UI.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
import math
import re
from typing import Any

from .models import Attempt, Channel


SCHEMA_VERSION = "chase-activity.v1"
HISTORY_LIMIT = 50
CHANNELS = ("phone", "email", "sms", "portal")
OUTCOMES = frozenset({
    "reached", "left_message", "no_answer", "wrong_number", "declined",
    "sent", "accepted", "delivered", "bounced", "recorded", "unknown",
})
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _timestamp(value: Any) -> datetime | None:
    # An offset is required: assigning a timezone to a legacy naive value
    # would invent ordering. RFC 3339's -00:00 also means an unknown offset.
    # The bounded grammar prevents raw text echo.
    if (
        not isinstance(value, str) or len(value) > 32
        or not _TIMESTAMP.fullmatch(value) or value.endswith("-00:00")
    ):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def build_activity(attempts: Sequence[Attempt]) -> dict[str, Any] | None:
    """Return all-attempt totals and at most 50 entries, newest instant first.

    Exact timestamp ties preserve stored order. All records are validated before
    truncation. Returning None means unavailable; an empty list means zero
    recorded attempts. No stored field or ordering is modified.
    """
    if not isinstance(attempts, (list, tuple)):
        return None
    attempts = tuple(attempts)
    counts = dict.fromkeys(CHANNELS, 0)
    projected: list[tuple[datetime, dict[str, Any]]] = []
    minutes: list[float] = []
    for attempt in attempts:
        if not isinstance(attempt, Attempt):
            return None
        channel = attempt.channel.value if isinstance(attempt.channel, Channel) else attempt.channel
        at = _timestamp(attempt.at)
        spent = attempt.minutes_spent
        if (
            not isinstance(channel, str) or channel not in counts
            or at is None
            or not isinstance(attempt.actor, str) or not attempt.actor.strip()
            or isinstance(spent, bool) or not isinstance(spent, (int, float))
            or not 0 <= spent <= 1440 or not math.isfinite(spent)
        ):
            return None
        outcome = (
            attempt.outcome
            if isinstance(attempt.outcome, str) and attempt.outcome in OUTCOMES
            else "unknown"
        )
        counts[channel] += 1
        minutes.append(float(spent))
        projected.append((at, {
            "at": at.isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "performer": "system" if attempt.actor == "system" else "staff",
            "outcome": outcome,
            "minutes": float(spent),
        }))
    projected.sort(key=lambda entry: entry[0], reverse=True)
    history = [entry for _, entry in projected[:HISTORY_LIMIT]]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "totalAttempts": len(attempts),
        "countsByChannel": counts,
        "staffMinutes": round(sum(minutes), 2),
        "lastAttempt": dict(history[0]) if history else None,
        "history": history,
        "truncated": len(attempts) > HISTORY_LIMIT,
    }


def combine_activity(legacy: dict[str, Any] | None, dispatch: dict[str, Any]) -> dict[str, Any] | None:
    """Merge trusted read projections without changing legacy scheduler records.

    Fifty entries from each source suffice for the combined latest fifty. Full
    counts and staff minutes come from each source's untruncated totals.
    """
    if legacy is None:
        return None
    history = sorted((dict(row) for row in legacy["history"] + dispatch["history"]),
                     key=lambda row: _timestamp(row["at"]), reverse=True)[:HISTORY_LIMIT]
    total = legacy["totalAttempts"] + dispatch["totalAttempts"]
    return {"schemaVersion": SCHEMA_VERSION, "totalAttempts": total,
            "countsByChannel": {channel: legacy["countsByChannel"][channel] + dispatch["countsByChannel"][channel]
                                for channel in CHANNELS},
            "staffMinutes": legacy["staffMinutes"],
            "lastAttempt": dict(history[0]) if history else None,
            "history": history, "truncated": total > HISTORY_LIMIT}
