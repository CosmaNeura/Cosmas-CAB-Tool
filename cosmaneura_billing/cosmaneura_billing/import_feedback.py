"""Explicit, all-or-nothing importer for legacy coder-feedback JSONL.

The pre-P2 ``coder_id`` field was caller-controlled. Import therefore requires
an operator-reviewed mapping from each legacy label to an immutable Entra OID;
unmapped rows never create identities or preference signals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from .coder_feedback import EncounterFeedback, validate_feedback
from .persistence import PostgresContext, PostgresFeedbackStore
from .settings import Settings


def prepare_import(
    source: Path, *, tenant_id: str, coder_mapping: dict[str, str],
) -> tuple[str, list[tuple[EncounterFeedback, bytes, int]]]:
    """Validate every line before returning any database work."""
    tenant = str(UUID(tenant_id))
    raw = source.read_bytes()
    digest_bytes = hashlib.sha256(raw).digest()
    digest_hex = digest_bytes.hex()
    prepared: list[tuple[EncounterFeedback, bytes, int]] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            feedback = EncounterFeedback.from_dict(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid feedback JSONL at line {line_number}") from exc
        mapped = coder_mapping.get(feedback.coder_id)
        if not mapped:
            raise ValueError(f"unmapped legacy coder at line {line_number}")
        try:
            coder_oid = str(UUID(mapped))
        except ValueError as exc:
            raise ValueError(
                f"invalid mapped coder object ID at line {line_number}"
            ) from exc
        feedback = replace(
            feedback,
            coder_id=coder_oid,
            coder_tenant_id=tenant,
            review_id=str(uuid5(
                NAMESPACE_URL, f"cosmaneura-feedback:{digest_hex}:{line_number}",
            )),
        )
        try:
            validate_feedback(feedback)
        except ValueError as exc:
            raise ValueError(
                f"invalid feedback contract at line {line_number}"
            ) from exc
        prepared.append((feedback, digest_bytes, line_number))
    return digest_hex, prepared


def import_feedback(
    source: Path, mapping_path: Path, *, settings: Settings, dry_run: bool = False,
) -> dict[str, object]:
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in mapping.items()
    ):
        raise ValueError("coder mapping must be a JSON object of string IDs")
    digest, rows = prepare_import(
        source, tenant_id=settings.entra_tenant_id, coder_mapping=mapping,
    )
    if dry_run:
        return {"digest": digest, "validated": len(rows), "imported": 0, "replayed": 0}
    store = PostgresFeedbackStore(PostgresContext(
        settings.database_url, practice_id=settings.practice_id,
    ))
    imported, replayed = store.import_batch(rows)
    return {
        "digest": digest, "validated": len(rows),
        "imported": imported, "replayed": replayed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import a prevalidated legacy coder-feedback JSONL file",
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--coder-map", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = import_feedback(
        args.source, args.coder_map, settings=Settings.from_env(),
        dry_run=args.dry_run,
    )
    # Counts and the non-secret file digest are safe; no record content or DSN.
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
