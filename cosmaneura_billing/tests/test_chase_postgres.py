"""PostgreSQL integration tests for multi-replica chase persistence.

Set ``COSMANEURA_TEST_DATABASE_URL`` to a disposable database whose name
contains ``test``. CI provisions one; local runs skip this module when unset.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from fastapi.testclient import TestClient

from cosmaneura_billing.api import app, get_chase_context
from cosmaneura_billing.chase import Attempt, Channel, ChaseItem, State, Trigger
from cosmaneura_billing.chase.store import PostgresChaseStore


TEST_DSN = os.environ.get("COSMANEURA_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="COSMANEURA_TEST_DATABASE_URL is not configured",
)


def _store(practice_id: str = "practice-a") -> PostgresChaseStore:
    return PostgresChaseStore(TEST_DSN, practice_id=practice_id)


def _item(patient_ref: str = "SYNTHETIC-1", chase_id: str = "chase-pg-1") -> ChaseItem:
    return ChaseItem(
        chase_id=chase_id,
        patient_ref=patient_ref,
        trigger=Trigger.NO_CARD_ON_FILE,
        detected_at="2026-08-17T09:00:00+00:00",
        appointment_at="2026-08-18",
        priority=80,
        direct_contract={"is_direct_contract": False, "source": "test"},
    )


@pytest.fixture(scope="session", autouse=True)
def _schema():
    if not TEST_DSN:
        yield
        return
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        if "test" not in (conn.info.dbname or "").lower():
            pytest.fail(
                "COSMANEURA_TEST_DATABASE_URL must name a disposable test database"
            )
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations" / "002_chase_persistence.up.sql"
        ).read_text(encoding="utf-8")
        conn.execute(migration, prepare=False)
    yield


@pytest.fixture(autouse=True)
def _empty_tables():
    if not TEST_DSN:
        yield
        return
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE chase_items CASCADE")
    yield
    get_chase_context.cache_clear()


def test_rows_survive_store_reconstruction_with_related_state():
    first_replica = _store()
    first_replica.upsert(_item())

    def transition(item: ChaseItem) -> None:
        item.state = State.SUBMITTED
        item.assignee = "cindy"
        item.attempts.append(Attempt(
            channel=Channel.PHONE,
            template_id="phone.call",
            at="2026-08-17T10:00:00+00:00",
            actor="cindy",
            outcome="reached",
            minutes_spent=3.5,
        ))
        item.pending_task = {"channel": "phone", "created_at": item.detected_at}
        item.resolution = {"outcome": "submitted", "note": "synthetic upload"}

    first_replica.mutate("chase-pg-1", transition)
    restored = _store().get("chase-pg-1")

    assert restored is not None
    assert restored.state is State.SUBMITTED
    assert restored.assignee == "cindy"
    assert restored.staff_minutes == 3.5
    assert restored.pending_task["channel"] == "phone"
    assert restored.resolution["outcome"] == "submitted"


def test_practice_scope_isolation_is_enforced_by_every_key():
    practice_a = _store("practice-a")
    practice_b = _store("practice-b")
    practice_a.upsert(_item())
    practice_b.upsert(_item())

    practice_a.mutate("chase-pg-1", lambda item: setattr(item, "assignee", "cindy"))

    assert practice_a.get("chase-pg-1").assignee == "cindy"
    assert practice_b.get("chase-pg-1").assignee == ""
    assert len(practice_a.all_items()) == len(practice_b.all_items()) == 1


def test_concurrent_replicas_append_without_losing_an_attempt():
    _store().upsert(_item())

    def append(actor: str) -> None:
        def transition(item: ChaseItem) -> None:
            item.attempts.append(Attempt(
                channel=Channel.PHONE,
                template_id="phone.call",
                at="2026-08-17T10:00:00+00:00",
                actor=actor,
                outcome="left_message",
                minutes_spent=1.0,
            ))
        _store().mutate("chase-pg-1", transition)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(append, ["cindy", "maggie"]))

    restored = _store().get("chase-pg-1")
    assert restored is not None
    assert {attempt.actor for attempt in restored.attempts} == {"cindy", "maggie"}
    assert restored.touches == 2


def test_failed_transition_rolls_back_item_and_contact_log():
    _store().upsert(_item())

    def fail(item: ChaseItem) -> None:
        item.assignee = "should-not-commit"
        item.attempts.append(Attempt(
            channel=Channel.PHONE,
            template_id="phone.call",
            at="2026-08-17T10:00:00+00:00",
            actor="cindy",
        ))
        raise RuntimeError("synthetic transition failure")

    with pytest.raises(RuntimeError, match="synthetic transition failure"):
        _store().mutate("chase-pg-1", fail)

    restored = _store().get("chase-pg-1")
    assert restored is not None
    assert restored.assignee == ""
    assert restored.attempts == []


def test_redetection_preserves_closed_state_and_history():
    store = _store()
    store.upsert(_item())

    def close(item: ChaseItem) -> None:
        item.state = State.VERIFIED_CLOSED
        item.closed_at = "2026-08-17T11:00:00+00:00"
        item.resolution = {"outcome": "verified_closed", "verified_by": "cindy"}

    store.mutate("chase-pg-1", close)
    refreshed = _item(chase_id="a-different-candidate-id")
    refreshed.priority = 100
    result = store.upsert(refreshed)

    assert result.chase_id == "chase-pg-1"
    assert result.state is State.VERIFIED_CLOSED
    assert result.priority == 80


def test_api_restart_reads_the_same_postgres_worklist(monkeypatch):
    monkeypatch.setenv("COSMANEURA_ENV", "test")
    monkeypatch.setenv("COSMANEURA_CHASE_STORE", "postgres")
    monkeypatch.setenv("COSMANEURA_CHASE_DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("COSMANEURA_PRACTICE_ID", "api-restart")
    get_chase_context.cache_clear()

    first_process = TestClient(app)
    detected = first_process.post("/api/chase/detect", json={
        "today": "2026-08-17",
        "snapshots": [{
            "patient_ref": "SYNTHETIC-API",
            "has_insurance_package": False,
            "email": "synthetic@example.invalid",
        }],
    })
    assert detected.status_code == 200
    assert detected.json()["counts"]["open"] == 1

    # A cleared application context represents a restart or another replica.
    get_chase_context.cache_clear()
    second_process = TestClient(app)
    restored = second_process.get("/api/chase/worklist")

    assert restored.status_code == 200
    assert [row["patient_ref"] for row in restored.json()["rows"]] == [
        "SYNTHETIC-API"
    ]
