"""The demo seed provenance is admitted without weakening the source enum."""
from __future__ import annotations

import re
from pathlib import Path

from cosmaneura_billing.migrations import migration_body


ROOT = Path(__file__).resolve().parents[1]
UP_SQL = (
    ROOT / "migrations" / "007_synthetic_demo_release_candidate_source.up.sql"
).read_text(encoding="utf-8")
DOWN_SQL = (
    ROOT / "migrations" / "007_synthetic_demo_release_candidate_source.down.sql"
).read_text(encoding="utf-8")

PREVIOUS_SOURCES = {
    "synthetic",
    "deidentified",
    "manual",
    "athena",
    "explicit_import",
}
DEMO_SOURCE = "synthetic_demo_release_candidate"


def _constraint_sources(sql: str) -> set[str]:
    normalized = " ".join(migration_body(sql).split())
    match = re.search(
        r"ADD CONSTRAINT encounters_source_valid CHECK \( source IN \((.*?)\) \)",
        normalized,
    )
    assert match is not None
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_up_migration_adds_only_the_fixed_demo_source():
    assert _constraint_sources(UP_SQL) == PREVIOUS_SOURCES | {DEMO_SOURCE}
    assert UP_SQL.count("DROP CONSTRAINT encounters_source_valid") == 1
    assert UP_SQL.count("ADD CONSTRAINT encounters_source_valid") == 1
    assert "ALTER TABLE encounters" in UP_SQL
    assert "UPDATE " not in UP_SQL.upper()
    assert "INSERT " not in UP_SQL.upper()


def test_down_migration_restores_the_exact_previous_allowlist():
    assert _constraint_sources(DOWN_SQL) == PREVIOUS_SOURCES
    assert DEMO_SOURCE not in migration_body(DOWN_SQL)
    assert "NOT VALID" not in DOWN_SQL.upper()


def test_both_directions_are_runner_owned_atomic_migrations():
    for sql in (UP_SQL, DOWN_SQL):
        body = migration_body(sql)
        assert "BEGIN;" not in body
        assert "COMMIT;" not in body
        assert "DROP CONSTRAINT encounters_source_valid" in body
        assert "ADD CONSTRAINT encounters_source_valid" in body
