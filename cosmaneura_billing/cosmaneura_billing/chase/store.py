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

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from typing import Any, Optional, Protocol

from .models import Attempt, ChaseItem
from .worklist import ChaseActionConflict, Worklist
from ..settings import Settings


class ChaseStore(Protocol):
    """Storage surface consumed by the API and future scheduler."""

    def upsert(self, item: ChaseItem) -> ChaseItem: ...
    def extend(self, items: Iterable[ChaseItem], **kwargs) -> list[ChaseItem]: ...
    def get(self, chase_id: str) -> Optional[ChaseItem]: ...
    def mutate(
        self, chase_id: str, change: Callable[[ChaseItem], Any], **kwargs,
    ) -> Optional[ChaseItem]: ...
    def rows(self, **filters: Any) -> list[ChaseItem]: ...
    def counts(self) -> dict[str, Any]: ...
    def all_items(self) -> list[ChaseItem]: ...
    def healthcheck(self) -> None: ...
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
        self._database_error = psycopg.Error
        self._dict_row = dict_row
        self._jsonb = Jsonb

    def __repr__(self) -> str:
        return f"PostgresChaseStore(practice_id={self.practice_id!r}, dsn='<redacted>')"

    @contextmanager
    def _connection(self):
        try:
            with self._connect(
                self._dsn, row_factory=self._dict_row, connect_timeout=3,
            ) as conn:
                conn.execute(
                    "SELECT set_config('cosmaneura.practice_id', %s, true)",
                    (self.practice_id,),
                )
                yield conn
        except self._database_error as exc:
            from ..persistence import PersistenceUnavailable
            raise PersistenceUnavailable("Postgres chase storage is unavailable") from exc

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

    def healthcheck(self) -> None:
        """Prove the configured database and chase schema are reachable."""
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM chase_items LIMIT 1")

    def get(self, chase_id: str) -> Optional[ChaseItem]:
        with self._connection() as conn:
            items = self._load_items(conn, [chase_id])
            return items[0] if items else None

    def audit_resource_id(self, chase_id: str) -> str:
        """Return the random DB identity; never audit the patient-derived ID."""
        with self._connection() as conn:
            row = conn.execute(
                """SELECT chase_uid FROM chase_items
                    WHERE practice_id = %s AND chase_id = %s""",
                (self.practice_id, chase_id),
            ).fetchone()
        return str(row["chase_uid"]) if row else ""

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

    def _upsert(self, conn, item: ChaseItem) -> ChaseItem:
        """Insert/refresh one item inside a caller-owned transaction."""
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

    def _commit_audit(self, conn, event) -> None:
        if event is None:
            return
        if event.practice_id != self.practice_id or event.outcome != "allowed":
            raise ValueError("chase audit completion does not match the mutation")
        # Local import avoids coupling the deterministic chase domain to the
        # HTTP audit types while still sharing one database transaction.
        from ..persistence import PostgresAccessAuditStore
        PostgresAccessAuditStore._insert_completion(conn, event)

    def _claim_action(
        self, conn, *, chase_id: str, action_id: str, payload_hash: str,
    ) -> bool:
        """Return true for a new action and false for an exact replay."""
        row = conn.execute(
            """INSERT INTO chase_action_receipts (
                   practice_id, action_id, chase_id, payload_hash
               ) VALUES (%s, %s::uuid, %s, %s)
               ON CONFLICT (practice_id, action_id) DO NOTHING
               RETURNING action_id""",
            (self.practice_id, action_id, chase_id, payload_hash),
        ).fetchone()
        if row is not None:
            return True
        existing = conn.execute(
            """SELECT chase_id, payload_hash FROM chase_action_receipts
                WHERE practice_id = %s AND action_id = %s::uuid""",
            (self.practice_id, action_id),
        ).fetchone()
        if (
            existing is None
            or existing["chase_id"] != chase_id
            or existing["payload_hash"] != payload_hash
        ):
            raise ChaseActionConflict(
                "action ID conflicts with an existing staff action"
            )
        return False

    def upsert(self, item: ChaseItem) -> ChaseItem:
        """Atomically insert or refresh one patient/problem pair."""
        with self._connection() as conn:
            return self._upsert(conn, item)

    def extend(self, items: Iterable[ChaseItem], *, audit_event=None) -> list[ChaseItem]:
        """Commit one detection batch and its DECIDE audit completion together."""
        with self._connection() as conn:
            rows = [self._upsert(conn, item) for item in items]
            self._commit_audit(conn, audit_event)
            return rows

    def extend_and_snapshot(
        self, items: Iterable[ChaseItem], *, audit_event,
    ) -> dict[str, Any]:
        """Return the committed response view without a post-audit DB read."""
        with self._connection() as conn:
            for item in items:
                self._upsert(conn, item)
            snapshot = Worklist()
            for item in self._load_items(conn):
                snapshot.items[Worklist._key(item)] = item
            payload = {
                "counts": snapshot.counts(),
                "rows": [item.to_dict() for item in snapshot.rows()],
            }
            self._commit_audit(conn, audit_event)
            return payload

    def mutate(
        self, chase_id: str, change: Callable[[ChaseItem], Any], *,
        audit_event=None, action_id: str | None = None,
        payload_hash: str | None = None,
        transaction_check: Callable[[Any], None] | None = None,
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
            if action_id:
                if not payload_hash:
                    raise ValueError("an idempotent action requires a payload hash")
                if not self._claim_action(
                    conn, chase_id=chase_id, action_id=action_id,
                    payload_hash=payload_hash,
                ):
                    self._commit_audit(conn, audit_event)
                    return item
            if transaction_check is not None:
                transaction_check(conn)
            previous_attempts = list(item.attempts)
            change(item)
            self._update_item(conn, item)
            self._sync_related(conn, item, previous_attempts=previous_attempts)
            self._commit_audit(conn, audit_event)
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


def chase_store_from_settings(settings: Settings) -> ChaseStore:
    """Build a chase store from one validated application configuration."""
    if settings.chase_store == "memory":
        if settings.environment == "production":
            raise RuntimeError(
                "in-memory chase storage is forbidden in production; configure "
                "COSMANEURA_CHASE_STORE=postgres and DATABASE_URL"
            )
        return Worklist()
    return PostgresChaseStore(settings.chase_dsn, practice_id=settings.practice_id)


def chase_store_from_env(
    environ: Mapping[str, str] | None = None,
) -> ChaseStore:
    """Compatibility wrapper for CLI callers and focused configuration tests."""
    return chase_store_from_settings(Settings.from_env(environ))


__all__ = [
    "ChaseActionConflict", "ChaseStore", "PostgresChaseStore", "chase_store_from_env",
    "chase_store_from_settings",
]
