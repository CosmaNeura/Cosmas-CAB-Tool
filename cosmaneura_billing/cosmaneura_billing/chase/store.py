"""Transactional storage for the insurance-data chase.

The chase state machine remains deterministic Python. This module is the
transaction boundary beneath it: API replicas lock one row, apply one domain
transition, and persist the item plus its append-only contact log before the
lock is released.

``Worklist`` remains the zero-setup test/demo implementation. It is rejected in
production so a deployment cannot silently lose its rows on restart or split
them across Container Apps replicas.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Optional, Protocol

from .models import Attempt, ChaseItem
from .worklist import Worklist


class ChaseStore(Protocol):
    """Storage surface consumed by the API and future scheduler."""

    def upsert(self, item: ChaseItem) -> ChaseItem: ...
    def extend(self, items: Iterable[ChaseItem]) -> list[ChaseItem]: ...
    def get(self, chase_id: str) -> Optional[ChaseItem]: ...
    def mutate(
        self, chase_id: str, change: Callable[[ChaseItem], Any],
    ) -> Optional[ChaseItem]: ...
    def rows(self, **filters: Any) -> list[ChaseItem]: ...
    def counts(self) -> dict[str, Any]: ...
    def all_items(self) -> list[ChaseItem]: ...
    def to_dict(self, **filters: Any) -> dict[str, Any]: ...


class PostgresChaseStore:
    """Practice-scoped chase storage backed by PostgreSQL.

    Connections are intentionally short-lived. A deployment-side pool can be
    added later without changing the transaction semantics; correctness across
    replicas never depends on process memory.
    """

    def __init__(self, dsn: str, *, practice_id: str) -> None:
        if not dsn.strip():
            raise ValueError("Postgres chase storage requires a database URL")
        practice_id = practice_id.strip()
        if not practice_id or len(practice_id) > 128:
            raise ValueError(
                "Postgres chase storage requires COSMANEURA_PRACTICE_ID "
                "(1-128 characters)"
            )
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - deployment wiring check
            raise RuntimeError(
                "Postgres chase storage requires the 'postgres' package extra"
            ) from exc

        self._dsn = dsn
        self.practice_id = practice_id
        self._connect = psycopg.connect
        self._dict_row = dict_row
        self._jsonb = Jsonb

    def __repr__(self) -> str:
        return f"PostgresChaseStore(practice_id={self.practice_id!r}, dsn='<redacted>')"

    def _connection(self):
        return self._connect(self._dsn, row_factory=self._dict_row)

    # --------------------------------------------------------------- loading #
    def _load_items(self, conn, chase_ids: list[str] | None = None) -> list[ChaseItem]:
        params: list[Any] = [self.practice_id]
        where = "practice_id = %s"
        if chase_ids is not None:
            if not chase_ids:
                return []
            where += " AND chase_id = ANY(%s)"
            params.append(chase_ids)

        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT chase_id, patient_ref, trigger, detected_at,
                           appointment_at, state, priority, bucket,
                           direct_contract, review_reason, assignee, closed_at
                      FROM chase_items
                     WHERE {where}
                     ORDER BY priority DESC, detected_at, patient_ref""",
                params,
            )
            rows = cur.fetchall()
            if not rows:
                return []

            ids = [row["chase_id"] for row in rows]
            cur.execute(
                """SELECT chase_id, channel, template_id, attempted_at AS at,
                          actor, outcome, minutes_spent, note
                     FROM chase_attempts
                    WHERE practice_id = %s AND chase_id = ANY(%s)
                    ORDER BY chase_id, attempt_no""",
                (self.practice_id, ids),
            )
            attempts: dict[str, list[dict[str, Any]]] = {}
            for row in cur.fetchall():
                attempts.setdefault(row["chase_id"], []).append(dict(row))

            cur.execute(
                """SELECT chase_id, payload
                     FROM chase_pending_tasks
                    WHERE practice_id = %s AND chase_id = ANY(%s)""",
                (self.practice_id, ids),
            )
            pending = {row["chase_id"]: row["payload"] for row in cur.fetchall()}

            cur.execute(
                """SELECT chase_id, payload
                     FROM chase_resolutions
                    WHERE practice_id = %s AND chase_id = ANY(%s)""",
                (self.practice_id, ids),
            )
            resolutions = {
                row["chase_id"]: row["payload"] for row in cur.fetchall()
            }

        return [ChaseItem.from_dict({
            **dict(row),
            "attempts": attempts.get(row["chase_id"], []),
            "pending_task": pending.get(row["chase_id"]),
            "resolution": resolutions.get(row["chase_id"]),
        }) for row in rows]

    def all_items(self) -> list[ChaseItem]:
        with self._connection() as conn:
            return self._load_items(conn)

    def get(self, chase_id: str) -> Optional[ChaseItem]:
        with self._connection() as conn:
            items = self._load_items(conn, [chase_id])
            return items[0] if items else None

    # --------------------------------------------------------------- writing #
    def _item_values(self, item: ChaseItem) -> tuple[Any, ...]:
        return (
            item.patient_ref,
            item.trigger.value,
            item.detected_at,
            item.appointment_at,
            item.state.value,
            item.priority,
            item.bucket,
            self._jsonb(item.direct_contract),
            item.review_reason,
            item.assignee,
            item.closed_at,
        )

    def _update_item(self, conn, item: ChaseItem) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE chase_items
                      SET patient_ref = %s, trigger = %s, detected_at = %s,
                          appointment_at = %s, state = %s, priority = %s,
                          bucket = %s, direct_contract = %s,
                          review_reason = %s, assignee = %s, closed_at = %s,
                          version = version + 1, updated_at = now()
                    WHERE practice_id = %s AND chase_id = %s""",
                (*self._item_values(item), self.practice_id, item.chase_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("chase row disappeared during a locked update")

    def _sync_related(
        self, conn, item: ChaseItem, *, previous_attempts: list[Attempt],
    ) -> None:
        old = [attempt.to_dict() for attempt in previous_attempts]
        current_prefix = [attempt.to_dict() for attempt in item.attempts[:len(old)]]
        if current_prefix != old or len(item.attempts) < len(old):
            raise ValueError("the chase contact log is append-only")

        with conn.cursor() as cur:
            for index, attempt in enumerate(
                item.attempts[len(old):], start=len(old) + 1,
            ):
                cur.execute(
                    """INSERT INTO chase_attempts (
                           practice_id, chase_id, attempt_no, channel,
                           template_id, attempted_at, actor, outcome,
                           minutes_spent, note
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        self.practice_id, item.chase_id, index,
                        attempt.channel.value, attempt.template_id, attempt.at,
                        attempt.actor, attempt.outcome, attempt.minutes_spent,
                        attempt.note,
                    ),
                )

            if item.pending_task is None:
                cur.execute(
                    """DELETE FROM chase_pending_tasks
                        WHERE practice_id = %s AND chase_id = %s""",
                    (self.practice_id, item.chase_id),
                )
            else:
                cur.execute(
                    """INSERT INTO chase_pending_tasks
                           (practice_id, chase_id, payload, updated_at)
                       VALUES (%s, %s, %s, now())
                       ON CONFLICT (practice_id, chase_id) DO UPDATE
                           SET payload = EXCLUDED.payload, updated_at = now()""",
                    (self.practice_id, item.chase_id,
                     self._jsonb(item.pending_task)),
                )

            if item.resolution is None:
                cur.execute(
                    """DELETE FROM chase_resolutions
                        WHERE practice_id = %s AND chase_id = %s""",
                    (self.practice_id, item.chase_id),
                )
            else:
                cur.execute(
                    """INSERT INTO chase_resolutions
                           (practice_id, chase_id, payload, updated_at)
                       VALUES (%s, %s, %s, now())
                       ON CONFLICT (practice_id, chase_id) DO UPDATE
                           SET payload = EXCLUDED.payload, updated_at = now()""",
                    (self.practice_id, item.chase_id,
                     self._jsonb(item.resolution)),
                )

    def upsert(self, item: ChaseItem) -> ChaseItem:
        """Atomically insert or refresh one patient/problem pair."""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT chase_id
                         FROM chase_items
                        WHERE practice_id = %s
                          AND patient_ref = %s AND trigger = %s
                        FOR UPDATE""",
                    (self.practice_id, item.patient_ref, item.trigger.value),
                )
                existing = cur.fetchone()

            if existing is None:
                # A concurrent first detection can win after our SELECT. The
                # unique key makes that safe; retry the locked read on conflict.
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO chase_items (
                               practice_id, chase_id, patient_ref, trigger,
                               detected_at, appointment_at, state, priority,
                               bucket, direct_contract, review_reason, assignee,
                               closed_at
                           ) VALUES (
                               %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s
                           )
                           ON CONFLICT (practice_id, patient_ref, trigger)
                           DO NOTHING
                           RETURNING chase_id""",
                        (
                            self.practice_id, item.chase_id,
                            *self._item_values(item),
                        ),
                    )
                    inserted = cur.fetchone()
                if inserted is not None:
                    self._sync_related(conn, item, previous_attempts=[])
                    return item
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT chase_id FROM chase_items
                            WHERE practice_id = %s
                              AND patient_ref = %s AND trigger = %s
                            FOR UPDATE""",
                        (self.practice_id, item.patient_ref, item.trigger.value),
                    )
                    existing = cur.fetchone()

            if existing is None:  # defensive: unique conflict must resolve
                raise RuntimeError("could not resolve concurrent chase upsert")
            current = self._load_items(conn, [existing["chase_id"]])[0]
            if current.is_open:
                current.priority = item.priority
                current.appointment_at = item.appointment_at or current.appointment_at
                current.direct_contract = item.direct_contract or current.direct_contract
                self._update_item(conn, current)
            return current

    def extend(self, items: Iterable[ChaseItem]) -> list[ChaseItem]:
        return [self.upsert(item) for item in items]

    def mutate(
        self, chase_id: str, change: Callable[[ChaseItem], Any],
    ) -> Optional[ChaseItem]:
        """Lock, transition, and persist exactly one chase row."""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT chase_id FROM chase_items
                        WHERE practice_id = %s AND chase_id = %s
                        FOR UPDATE""",
                    (self.practice_id, chase_id),
                )
                if cur.fetchone() is None:
                    return None

            item = self._load_items(conn, [chase_id])[0]
            previous_attempts = list(item.attempts)
            change(item)
            self._update_item(conn, item)
            self._sync_related(conn, item, previous_attempts=previous_attempts)
            return item

    # ---------------------------------------------------------------- views #
    def _snapshot(self) -> Worklist:
        snapshot = Worklist()
        for item in self.all_items():
            snapshot.items[Worklist._key(item)] = item
        return snapshot

    def rows(self, **filters: Any) -> list[ChaseItem]:
        return self._snapshot().rows(**filters)

    def counts(self) -> dict[str, Any]:
        return self._snapshot().counts()

    def to_dict(self, **filters: Any) -> dict[str, Any]:
        snapshot = self._snapshot()
        return {
            "counts": snapshot.counts(),
            "rows": [item.to_dict() for item in snapshot.rows(**filters)],
        }


def chase_store_from_env(
    environ: Mapping[str, str] | None = None,
) -> ChaseStore:
    """Build the configured chase store without reading environment at import."""
    env = os.environ if environ is None else environ
    backend = (env.get("COSMANEURA_CHASE_STORE") or "memory").strip().lower()
    environment = (env.get("COSMANEURA_ENV") or "development").strip().lower()

    if backend == "memory":
        if environment == "production":
            raise RuntimeError(
                "in-memory chase storage is forbidden in production; configure "
                "COSMANEURA_CHASE_STORE=postgres and DATABASE_URL"
            )
        return Worklist()
    if backend != "postgres":
        raise ValueError("COSMANEURA_CHASE_STORE must be 'memory' or 'postgres'")

    dsn = env.get("COSMANEURA_CHASE_DATABASE_URL") or env.get("DATABASE_URL") or ""
    practice_id = env.get("COSMANEURA_PRACTICE_ID") or ""
    return PostgresChaseStore(dsn, practice_id=practice_id)


__all__ = ["ChaseStore", "PostgresChaseStore", "chase_store_from_env"]
