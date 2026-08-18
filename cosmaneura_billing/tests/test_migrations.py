"""Migration discovery and deployment-runner safety checks."""
from pathlib import Path

import pytest

from cosmaneura_billing.migrations import (
    discover_migrations, migration_body, migration_directory,
)
from cosmaneura_billing.settings import Settings


def test_checked_in_up_migrations_are_ordered_and_down_files_are_excluded():
    paths = discover_migrations(migration_directory(Settings()))
    assert [path.name for path in paths] == [
        "001_init_pgvector.up.sql",
        "002_chase_persistence.up.sql",
        "003_pilot_persistence.up.sql",
        "004_chase_delivery_runtime.up.sql",
    ]


def test_explicit_missing_migration_directory_fails_closed(tmp_path):
    settings = Settings(migrations_dir=str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="migration directory is unavailable"):
        discover_migrations(migration_directory(settings))


def test_runner_owns_transaction_without_weakening_manual_sql_files():
    sql = "-- comment\n\nBEGIN;\nCREATE TABLE example (id int);\nCOMMIT;\n"

    body = migration_body(sql)

    assert "CREATE TABLE example" in body
    assert "BEGIN;" not in body
    assert "COMMIT;" not in body


def test_nested_or_partial_transaction_control_is_rejected():
    with pytest.raises(RuntimeError, match="outer BEGIN/COMMIT"):
        migration_body("CREATE TABLE example (id int);\nCOMMIT;")
