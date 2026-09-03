"""Bounded ingestion entrypoint for de-identified visit pages (N2).

Loads one ``scribe.visit.v1`` page from a JSON file and commits it through the
:mod:`cosmaneura_billing.scribe_import` adapter and the durable
:mod:`cosmaneura_billing.source_import` store. This lets a de-identified cohort
be loaded into a Beta-0 environment before the live Scribe producer exists.

    python -m cosmaneura_billing.scribe_import_cli \
        --page page.json --practice-id synthetic-pilot [--dsn DSN] [--source-system scribe]

Fail-closed and identifier-free: the command prints counts only (never patient
text or ids), and any schema/conflict/error returns a non-zero exit with a fixed
code. Without ``--dsn`` it uses an in-memory store for validation/dry-run only —
that result is not durable across processes. Staging/production ingestion must
pass a ``--dsn`` for the dedicated import role whose server-owned
``cosmaneura_source_import_allowed`` binding permits this source and provenance.
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Callable, Optional, Sequence

from .scribe_import import SCRIBE_SOURCE_SYSTEM, ingest_visit_page
from .source_import import (
    MemorySourceImportStore,
    SourceImportConflict,
    SourceImportError,
    SourceImportSchemaError,
    SourceImportStore,
)

StoreFactory = Callable[..., SourceImportStore]

# Exit codes: 0 ok, 1 import rejected (schema/conflict/error), 2 input/config.
EXIT_OK = 0
EXIT_IMPORT_REJECTED = 1
EXIT_INPUT_ERROR = 2


def _default_store(*, dsn: Optional[str], practice_id: str) -> SourceImportStore:
    if dsn:
        # Imported lazily so the in-memory dry-run path needs no driver.
        from .source_import import PostgresSourceImportStore

        return PostgresSourceImportStore(dsn, practice_id=practice_id)
    return MemorySourceImportStore(practice_id=practice_id)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    store_factory: Optional[StoreFactory] = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cosmaneura_billing.scribe_import_cli",
        description="Import one de-identified scribe.visit.v1 page.",
    )
    parser.add_argument("--page", required=True, help="Path to the JSON page file.")
    parser.add_argument("--practice-id", required=True, dest="practice_id")
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN (omit for in-memory dry-run).")
    parser.add_argument("--source-system", default=SCRIBE_SOURCE_SYSTEM, dest="source_system")
    args = parser.parse_args(argv)

    try:
        with open(args.page, "r", encoding="utf-8") as handle:
            page = json.load(handle)
    except (OSError, ValueError):
        _emit({"code": "PAGE_UNREADABLE"})
        return EXIT_INPUT_ERROR

    factory = store_factory or _default_store
    try:
        store = factory(dsn=args.dsn, practice_id=args.practice_id)
    except Exception:
        _emit({"code": "STORE_UNAVAILABLE"})
        return EXIT_INPUT_ERROR

    try:
        result = ingest_visit_page(
            store, page,
            practice_id=args.practice_id,
            source_system=args.source_system,
        )
    except SourceImportConflict:
        _emit({"code": "SOURCE_IMPORT_CONFLICT"})
        return EXIT_IMPORT_REJECTED
    except SourceImportSchemaError:
        _emit({"code": "SOURCE_IMPORT_SCHEMA_INVALID"})
        return EXIT_IMPORT_REJECTED
    except (SourceImportError, Exception):
        _emit({"code": "SOURCE_IMPORT_ERROR"})
        return EXIT_IMPORT_REJECTED

    _emit({
        "code": "IMPORTED",
        "imported": result.imported_count,
        "replayed": result.replayed_count,
        "checkpoint_version": result.checkpoint.version,
        "durable": bool(args.dsn),
    })
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())


__all__ = ["main"]
