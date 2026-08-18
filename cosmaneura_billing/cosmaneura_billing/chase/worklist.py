"""The master worklist — one integrated list, not three dashboards.

The staff who do this job today work from whatever they can reconstruct across
Acuity, Athena and memory. Grace's Aug 13 walkthrough described the shape they
want: one place showing patients with eligibility issues, expired cards, or
missing payment information, with follow-ups tracked rather than remembered.

So the three buckets are views over one list, not separate systems. A row is a
patient's insurance-data problem regardless of which trigger found it, and it
stays on the list until a verified card closes it or a person closes it with a
reason. Nothing falls off silently — that is the failure the current process has.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .models import TERMINAL, ChaseItem, State, Trigger

BUCKETS = ("missing", "expired", "eligibility")


@dataclass
class Worklist:
    """An in-memory worklist. Persistence is a storage concern, not a queue one."""
    items: dict[str, ChaseItem] = field(default_factory=dict)

    # ------------------------------------------------------------------ load #
    def upsert(self, item: ChaseItem) -> ChaseItem:
        """Add or refresh a row, keyed on patient + trigger.

        Re-running the detector must not create a second row for the same
        problem — daily detection over the same unresolved patient would
        otherwise inflate the backlog and the metrics with duplicates. An
        existing open row keeps its contact history and is re-prioritised;
        a closed row is left closed.
        """
        key = self._key(item)
        existing = self.items.get(key)
        if existing is None:
            self.items[key] = item
            return item
        if not existing.is_open:
            return existing
        existing.priority = item.priority
        existing.appointment_at = item.appointment_at or existing.appointment_at
        existing.direct_contract = item.direct_contract or existing.direct_contract
        return existing

    def extend(self, items: Iterable[ChaseItem]) -> list[ChaseItem]:
        return [self.upsert(i) for i in items]

    @staticmethod
    def _key(item: ChaseItem) -> str:
        return f"{item.patient_ref}:{item.trigger.value}"

    def get(self, chase_id: str) -> Optional[ChaseItem]:
        return next((i for i in self.items.values() if i.chase_id == chase_id), None)

    def mutate(
        self, chase_id: str, change: Callable[[ChaseItem], Any],
    ) -> Optional[ChaseItem]:
        """Apply one state transition.

        The in-memory implementation needs no transaction. The Postgres
        implementation exposes the same method while holding a row lock, so API
        handlers do not have a read/mutate/write race between replicas.
        """
        item = self.get(chase_id)
        if item is not None:
            change(item)
        return item

    def all_items(self) -> list[ChaseItem]:
        return list(self.items.values())

    def healthcheck(self) -> None:
        """The in-process development store has no external dependency."""

    # ------------------------------------------------------------------ views #
    def rows(
        self,
        *,
        bucket: str = "",
        state: Optional[State] = None,
        assignee: str = "",
        open_only: bool = True,
        needs_person: bool = False,
    ) -> list[ChaseItem]:
        """The list, filtered. Highest priority first, then oldest first.

        Ordering puts the work that expires soonest at the top, and breaks ties
        toward whoever has been waiting longest — the row nobody picked up
        yesterday should not stay invisible today.
        """
        out = list(self.items.values())
        if open_only:
            out = [i for i in out if i.is_open]
        if bucket:
            out = [i for i in out if i.bucket == bucket]
        if state is not None:
            out = [i for i in out if i.state == state]
        if assignee:
            out = [i for i in out if i.assignee == assignee]
        if needs_person:
            out = [i for i in out if i.state == State.NEEDS_REVIEW]
        return sorted(out, key=lambda i: (-i.priority, i.detected_at, i.patient_ref))

    def counts(self) -> dict[str, Any]:
        """The header numbers: what is outstanding, and what is stuck on a human."""
        open_items = [i for i in self.items.values() if i.is_open]
        return {
            "open": len(open_items),
            "by_bucket": {b: sum(1 for i in open_items if i.bucket == b)
                          for b in BUCKETS},
            "by_state": {s.value: sum(1 for i in open_items if i.state == s)
                         for s in State if s not in TERMINAL},
            "needs_person": sum(1 for i in open_items if i.state == State.NEEDS_REVIEW),
            "unassigned": sum(1 for i in open_items if not i.assignee),
            "closed": sum(1 for i in self.items.values() if not i.is_open),
        }

    def assign(self, chase_id: str, *, to: str) -> Optional[ChaseItem]:
        item = self.get(chase_id)
        if item is not None:
            item.assignee = to
        return item

    def to_dict(self, **filters) -> dict[str, Any]:
        return {
            "counts": self.counts(),
            "rows": [i.to_dict() for i in self.rows(**filters)],
        }
