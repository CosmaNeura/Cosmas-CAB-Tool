"""P3 migration runner invariants that do not require a live PostgreSQL."""
from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager

import pytest

from cosmaneura_billing.migrations import (
    _bootstrap_practice_admin,
    apply_migrations,
)
from cosmaneura_billing.settings import Settings


TENANT_ID = "11111111-1111-4111-8111-111111111111"
ADMIN_ID = "22222222-2222-4222-8222-222222222222"


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Transaction(AbstractContextManager):
    def __init__(self, connection):
        self.connection = connection
        self.snapshot = None

    def __enter__(self):
        assert self.connection.transaction_depth == 0
        self.snapshot = (
            dict(self.connection.applied), self.connection.schema_change_applied,
        )
        self.connection.transaction_depth = 1
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        self.connection.transaction_depth = 0
        if exc_type is not None:
            (
                self.connection.applied,
                self.connection.schema_change_applied,
            ) = self.snapshot
            self.connection.rolled_back = True
        return False


class _MigrationConnection(AbstractContextManager):
    def __init__(self, applied=None, *, fail_ledger_insert=False):
        self.applied = dict(applied or {})
        self.fail_ledger_insert = fail_ledger_insert
        self.transaction_depth = 0
        self.migration_executed_in_transaction = False
        self.ledger_written_in_transaction = False
        self.schema_change_applied = False
        self.rolled_back = False
        self.unlocked = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def transaction(self):
        return _Transaction(self)

    def execute(self, statement, params=None, **_kwargs):
        sql = " ".join(str(statement).split())
        if sql.startswith("SELECT name, checksum_sha256"):
            return _Result(self.applied.items())
        if sql.startswith("UPDATE cosmaneura_schema_migrations"):
            checksum, name = params
            self.applied[name] = checksum
            return _Result()
        if sql.startswith("INSERT INTO cosmaneura_schema_migrations"):
            self.ledger_written_in_transaction = self.transaction_depth == 1
            if self.fail_ledger_insert:
                raise RuntimeError("synthetic ledger failure")
            name, checksum = params
            self.applied[name] = checksum
            return _Result()
        if sql.startswith("SELECT pg_advisory_unlock"):
            self.unlocked = True
            return _Result()
        if "CREATE TABLE synthetic_schema_change" in sql:
            self.migration_executed_in_transaction = self.transaction_depth == 1
            self.schema_change_applied = True
        return _Result()


class _Connect:
    def __init__(self, connection):
        self.connection = connection

    def __call__(self, dsn, *, autocommit):
        assert dsn == "postgresql://migration.invalid/db"
        assert autocommit is True
        return self.connection


def _migration_settings(tmp_path):
    migration = tmp_path / "001_synthetic.up.sql"
    migration.write_text(
        "BEGIN;\nCREATE TABLE synthetic_schema_change (id int);\nCOMMIT;\n",
        encoding="utf-8",
    )
    return migration, Settings(
        database_url="postgresql://migration.invalid/db",
        migrations_dir=str(tmp_path),
    )


def test_schema_change_and_ledger_row_share_one_transaction(tmp_path):
    migration, settings = _migration_settings(tmp_path)
    connection = _MigrationConnection()

    assert apply_migrations(settings, connect=_Connect(connection)) == [
        migration.name
    ]
    assert connection.migration_executed_in_transaction is True
    assert connection.ledger_written_in_transaction is True
    assert connection.schema_change_applied is True
    assert connection.unlocked is True


def test_ledger_failure_rolls_back_and_releases_advisory_lock(tmp_path):
    _migration, settings = _migration_settings(tmp_path)
    connection = _MigrationConnection(fail_ledger_insert=True)

    with pytest.raises(RuntimeError, match="synthetic ledger failure"):
        apply_migrations(settings, connect=_Connect(connection))

    assert connection.applied == {}
    assert connection.schema_change_applied is False
    assert connection.rolled_back is True
    assert connection.unlocked is True


def test_applied_migration_checksum_mismatch_fails_closed(tmp_path):
    migration, settings = _migration_settings(tmp_path)
    connection = _MigrationConnection({migration.name: "0" * 64})

    with pytest.raises(RuntimeError, match=rf"checksum changed: {migration.name}"):
        apply_migrations(settings, connect=_Connect(connection))

    assert connection.unlocked is True


def test_legacy_null_checksum_is_backfilled_without_reapplying(tmp_path):
    migration, settings = _migration_settings(tmp_path)
    connection = _MigrationConnection({migration.name: None})

    assert apply_migrations(settings, connect=_Connect(connection)) == []
    assert connection.applied[migration.name] == hashlib.sha256(
        migration.read_bytes()
    ).hexdigest()
    assert connection.migration_executed_in_transaction is False
    assert connection.unlocked is True


class _BootstrapConnection:
    """Tiny behavior fake for an intentionally deactivated initial admin."""

    def __init__(self):
        self.user_active = False
        self.membership_active = False

    def execute(self, statement, _params=None):
        sql = " ".join(statement.split()).casefold()
        conflict_update = sql.partition("on conflict")[2]
        if sql.startswith("insert into users") and "active =" in conflict_update:
            self.user_active = True
        if (
            sql.startswith("insert into practice_memberships")
            and "active =" in conflict_update
        ):
            self.membership_active = True
        return _Result()


def test_repeat_bootstrap_does_not_reactivate_disabled_administrator():
    connection = _BootstrapConnection()

    _bootstrap_practice_admin(
        connection,
        Settings(
            database_url="postgresql://migration.invalid/db",
            entra_tenant_id=TENANT_ID,
            practice_id="practice-a",
            bootstrap_admin_oid=ADMIN_ID,
        ),
    )

    assert connection.user_active is False
    assert connection.membership_active is False
