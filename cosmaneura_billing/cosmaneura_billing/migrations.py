"""Small, locked SQL migration runner for deployment jobs.

Migrations stay as reviewable SQL files in the repository.  This module only
orders them, records which files committed successfully, and serializes runners
with a PostgreSQL advisory lock.  It never prints a DSN or SQL parameter.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .settings import Settings

_UP_MIGRATION = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.up\.sql$")
_MIGRATION_LOCK = 4_836_623_987_031_721_441


def migration_directory(settings: Settings) -> Path:
    if settings.migrations_dir:
        return Path(settings.migrations_dir)
    return Path(__file__).resolve().parents[1] / "migrations"


def discover_migrations(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise RuntimeError(f"migration directory is unavailable: {directory}")
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and _UP_MIGRATION.fullmatch(path.name)
    )


def migration_body(sql: str) -> str:
    """Remove one conventional outer transaction for runner-owned atomicity."""
    lines = sql.splitlines()
    significant = [
        index for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("--")
    ]
    controls = [
        index for index in significant
        if lines[index].strip().upper() in {"BEGIN;", "COMMIT;"}
    ]
    if not controls:
        return sql
    if (
        len(controls) != 2
        or controls[0] != significant[0]
        or controls[1] != significant[-1]
        or lines[controls[0]].strip().upper() != "BEGIN;"
        or lines[controls[1]].strip().upper() != "COMMIT;"
    ):
        raise RuntimeError(
            "migration transaction control must be one outer BEGIN/COMMIT pair"
        )
    return "\n".join(
        line for index, line in enumerate(lines) if index not in controls
    )


def apply_migrations(
    settings: Settings | None = None,
    *,
    connect: Callable[..., object] | None = None,
) -> list[str]:
    """Apply every pending up migration and return the names applied now."""
    config = settings or Settings.from_env()
    if not config.database_url:
        raise RuntimeError("DATABASE_URL is required to apply migrations")
    if connect is None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - image wiring check
            raise RuntimeError(
                "database migrations require the 'postgres' package extra"
            ) from exc
        connect = psycopg.connect

    paths = discover_migrations(migration_directory(config))
    if not paths:
        raise RuntimeError("no up migrations were found")

    applied_now: list[str] = []
    with connect(config.database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS cosmaneura_schema_migrations (
                       name text PRIMARY KEY,
                       applied_at timestamptz NOT NULL DEFAULT now()
                   )"""
            )
            applied = {
                row[0] for row in conn.execute(
                    "SELECT name FROM cosmaneura_schema_migrations"
                ).fetchall()
            }
            for path in paths:
                if path.name in applied:
                    continue
                # The schema change and its ledger row commit together. Checked-in
                # SQL keeps an outer transaction for safe manual psql use; the
                # runner removes only that wrapper and owns the transaction here.
                with conn.transaction():
                    conn.execute(
                        migration_body(path.read_text(encoding="utf-8")),
                        prepare=False,
                    )
                    conn.execute(
                        "INSERT INTO cosmaneura_schema_migrations (name) VALUES (%s)",
                        (path.name,),
                    )
                applied_now.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    return applied_now


def main() -> int:
    for name in apply_migrations():
        print(f"applied {name}")
    return 0


if __name__ == "__main__":  # pragma: no cover - deployment entrypoint
    raise SystemExit(main())
