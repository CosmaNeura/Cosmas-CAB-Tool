"""Practice-bound, source-agnostic persistence for bounded import pages.

Adapters own protocol details and normalize records before they reach this
module.  This layer owns the durable replay/checkpoint transaction only: every
new receipt, encounter change, and the page's opaque continuation state commits
together or not at all.  Raw source identifiers and cursors are never rendered
or logged by these types.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Callable, Protocol, Sequence
from uuid import NAMESPACE_URL, uuid5

from .persistence import PersistenceUnavailable, PostgresContext, StoredEncounter
from .schemas import VisitBundle


MAX_IMPORT_RECORDS = 100
MAX_IMPORT_CURSOR_BYTES = 512
MAX_IMPORT_CURSOR_HISTORY = 100
MAX_IMPORT_PAGE_BYTES = 16 * 1024 * 1024
_SOURCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FEED_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_PROVENANCE_NAME = re.compile(r"[a-z][a-z0-9_]{0,127}")
_MAPPING_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ENCOUNTER_FEED = "encounters"


class SourceImportError(RuntimeError):
    """Fail-closed import error with a static, identifier-free message."""

    code = "SOURCE_IMPORT_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)


class SourceImportConflict(SourceImportError):
    code = "SOURCE_IMPORT_CONFLICT"


class SourceImportSchemaError(SourceImportError):
    code = "SOURCE_IMPORT_SCHEMA_INVALID"


class SourceEncounterRecord(Protocol):
    """Structural seam implemented by each source adapter's normalized record."""

    target_practice_id: str
    source_record_id: str
    source_version: str
    mapping_version: str
    idempotency_key: str
    visit: VisitBundle


@dataclass(frozen=True, repr=False)
class SourceImportCheckpoint:
    checkpoint: str | None
    next_cursor: str | None
    mapping_version: str
    cursor_history: tuple[str, ...]
    version: int

    def __repr__(self) -> str:
        return f"SourceImportCheckpoint(version={self.version})"


@dataclass(frozen=True)
class SourceImportPageResult:
    imported_count: int
    replayed_count: int
    checkpoint: SourceImportCheckpoint


class SourceImportStore(Protocol):
    def load_checkpoint(
        self,
        *,
        source_system: str,
        feed: str,
    ) -> SourceImportCheckpoint | None:
        ...

    def import_encounter_page(
        self,
        *,
        source_system: str,
        feed: str,
        provenance: str,
        mapping_version: str,
        records: Sequence[SourceEncounterRecord],
        checkpoint: str | None,
        next_cursor: str | None,
        expected_checkpoint_version: int | None,
    ) -> SourceImportPageResult:
        ...


def _validate_name(
    value: str,
    *,
    feed: bool = False,
    provenance: bool = False,
) -> str:
    expression = (
        _PROVENANCE_NAME
        if provenance
        else _FEED_NAME if feed else _SOURCE_NAME
    )
    if not isinstance(value, str) or expression.fullmatch(value) is None:
        raise SourceImportSchemaError() from None
    return value


def _validate_opaque(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise SourceImportSchemaError() from None
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SourceImportSchemaError() from None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise SourceImportSchemaError() from None
    if len(encoded) > MAX_IMPORT_CURSOR_BYTES:
        raise SourceImportSchemaError() from None
    return value


def _validate_source_identity(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise SourceImportSchemaError() from None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SourceImportSchemaError() from None
    return value


def source_import_idempotency_key(
    *,
    practice_id: str,
    source_system: str,
    feed: str,
    source_record_id: str,
    source_version: str,
    mapping_version: str,
) -> str:
    """Derive the one accepted, identifier-hiding replay key."""

    canonical = json.dumps(
        [
            _validate_source_identity(practice_id),
            _validate_name(source_system),
            _validate_name(feed, feed=True),
            _validate_source_identity(source_record_id),
            _validate_source_identity(source_version),
            _validate_mapping_version(mapping_version),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "source-import-v1:" + hashlib.sha256(canonical).hexdigest()


def _validate_mapping_version(value: str) -> str:
    if not isinstance(value, str) or _MAPPING_VERSION.fullmatch(value) is None:
        raise SourceImportSchemaError() from None
    return value


def _validate_encounter_feed(value: str) -> str:
    """Keep the encounter writer unavailable to every other feed type."""

    feed = _validate_name(value, feed=True)
    if feed != _ENCOUNTER_FEED:
        raise SourceImportSchemaError() from None
    return feed


def source_import_external_id(
    *,
    practice_id: str,
    source_system: str,
    feed: str,
    source_record_id: str,
) -> str:
    """Return a deterministic internal ID without exposing the source ID."""

    canonical = json.dumps(
        [
            _validate_source_identity(practice_id),
            _validate_name(source_system),
            _validate_name(feed, feed=True),
            _validate_source_identity(source_record_id),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "source-encounter-v1:" + hashlib.sha256(canonical).hexdigest()


def _payload_sha256(bundle: VisitBundle) -> str:
    if not isinstance(bundle, VisitBundle):
        raise SourceImportSchemaError() from None
    try:
        canonical = json.dumps(
            asdict(bundle),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise SourceImportSchemaError() from None
    return hashlib.sha256(canonical).hexdigest()


def _validated_records(
    records: Sequence[SourceEncounterRecord],
    *,
    practice_id: str,
    source_system: str,
    feed: str,
    mapping_version: str,
) -> tuple[tuple[SourceEncounterRecord, str, str], ...]:
    if (
        isinstance(records, (str, bytes))
        or not isinstance(records, Sequence)
        or len(records) > MAX_IMPORT_RECORDS
    ):
        raise SourceImportSchemaError() from None
    validated: list[tuple[SourceEncounterRecord, str, str]] = []
    for record in records:
        try:
            target_practice_id = record.target_practice_id
            source_record_id = record.source_record_id
            source_version = record.source_version
            record_mapping_version = record.mapping_version
            idempotency_key = record.idempotency_key
            visit = record.visit
        except (AttributeError, TypeError):
            raise SourceImportSchemaError() from None
        if (
            target_practice_id != practice_id
            or record_mapping_version != mapping_version
            or not isinstance(idempotency_key, str)
        ):
            raise SourceImportSchemaError() from None
        expected_key = source_import_idempotency_key(
            practice_id=practice_id,
            source_system=source_system,
            feed=feed,
            source_record_id=source_record_id,
            source_version=source_version,
            mapping_version=record_mapping_version,
        )
        if idempotency_key != expected_key:
            raise SourceImportSchemaError() from None
        target_external_id = source_import_external_id(
            practice_id=practice_id,
            source_system=source_system,
            feed=feed,
            source_record_id=source_record_id,
        )
        validated.append((record, _payload_sha256(visit), target_external_id))
    return tuple(validated)


def _page_records(
    prepared: Sequence[tuple[SourceEncounterRecord, str, str]],
) -> list[dict[str, object]]:
    """Build and byte-bound the exact JSON page accepted by PostgreSQL."""

    try:
        page = [
            {
                "target_external_id": target_external_id,
                "idempotency_key": record.idempotency_key,
                "mapping_version": record.mapping_version,
                "payload_sha256": payload_hash,
                "patient_age": record.visit.patient_age,
                "patient_sex": record.visit.patient_sex,
                "encounter_type": record.visit.encounter_type,
                "place_of_service": record.visit.place_of_service,
                "intake_text": record.visit.intake_text,
                "transcript": record.visit.transcript,
                "signed_note": record.visit.signed_note,
                "provider_specialty": record.visit.provider_specialty,
                "submitted_em_code": record.visit.submitted_em_code,
                "submitted_icd10": list(record.visit.submitted_icd10),
            }
            for record, payload_hash, target_external_id in prepared
        ]
        serialized = json.dumps(
            page,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise SourceImportSchemaError() from None
    if len(serialized) > MAX_IMPORT_PAGE_BYTES:
        raise SourceImportSchemaError() from None
    return page


def _validate_expected_checkpoint_version(value: int | None) -> int | None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 1
    ):
        raise SourceImportSchemaError() from None
    return value


def _checkpoint_matches_expected(
    checkpoint: SourceImportCheckpoint | None,
    expected_version: int | None,
) -> bool:
    return (
        checkpoint is None and expected_version is None
    ) or (
        checkpoint is not None and checkpoint.version == expected_version
    )


class MemorySourceImportStore:
    """Transactional in-memory implementation for deterministic adapter tests."""

    def __init__(
        self,
        *,
        practice_id: str,
        transaction_check: Callable[[], None] | None = None,
    ) -> None:
        self.practice_id = _validate_source_identity(practice_id)
        self._transaction_check = transaction_check
        self._checkpoints: dict[tuple[str, str], SourceImportCheckpoint] = {}
        self._receipts: dict[
            tuple[str, str, str], tuple[str, str, str, str]
        ] = {}
        self._encounters: dict[str, StoredEncounter] = {}

    def load_checkpoint(
        self,
        *,
        source_system: str,
        feed: str,
    ) -> SourceImportCheckpoint | None:
        key = (_validate_name(source_system), _validate_name(feed, feed=True))
        return self._checkpoints.get(key)

    def get_encounter(self, encounter_id: str) -> StoredEncounter | None:
        return self._encounters.get(encounter_id)

    def import_encounter_page(
        self,
        *,
        source_system: str,
        feed: str,
        provenance: str,
        mapping_version: str,
        records: Sequence[SourceEncounterRecord],
        checkpoint: str | None,
        next_cursor: str | None,
        expected_checkpoint_version: int | None = None,
    ) -> SourceImportPageResult:
        source_system = _validate_name(source_system)
        feed = _validate_encounter_feed(feed)
        provenance = _validate_name(provenance, provenance=True)
        mapping_version = _validate_mapping_version(mapping_version)
        checkpoint = _validate_opaque(checkpoint, required=False)
        next_cursor = _validate_opaque(next_cursor, required=False)
        if checkpoint is None and next_cursor is None:
            raise SourceImportSchemaError() from None
        expected_checkpoint_version = _validate_expected_checkpoint_version(
            expected_checkpoint_version,
        )
        prepared = _validated_records(
            records,
            practice_id=self.practice_id,
            source_system=source_system,
            feed=feed,
            mapping_version=mapping_version,
        )
        _page_records(prepared)

        receipts = dict(self._receipts)
        encounters = dict(self._encounters)
        checkpoints = dict(self._checkpoints)
        imported = 0
        replayed = 0
        for record, payload_hash, target_external_id in prepared:
            receipt_key = (source_system, feed, record.idempotency_key)
            existing_receipt = receipts.get(receipt_key)
            if existing_receipt is not None:
                if existing_receipt != (
                    payload_hash,
                    target_external_id,
                    provenance,
                    record.mapping_version,
                ):
                    raise SourceImportConflict() from None
                replayed += 1
                continue

            prior = encounters.get(target_external_id)
            if prior is not None and prior.source != provenance:
                raise SourceImportConflict() from None
            stored_visit = replace(
                record.visit,
                encounter_id=target_external_id,
            )
            changed = prior is None or prior.bundle != stored_visit
            resource_id = (
                prior.resource_id
                if prior is not None
                else str(uuid5(
                    NAMESPACE_URL,
                    f"{self.practice_id}:{provenance}:{target_external_id}",
                ))
            )
            encounters[target_external_id] = StoredEncounter(
                resource_id=resource_id,
                bundle=stored_visit,
                version=(
                    1
                    if prior is None
                    else prior.version + 1 if changed
                    else prior.version
                ),
                source=provenance,
            )
            receipts[receipt_key] = (
                payload_hash,
                target_external_id,
                provenance,
                record.mapping_version,
            )
            imported += 1

        old_checkpoint = checkpoints.get((source_system, feed))
        if not _checkpoint_matches_expected(
            old_checkpoint,
            expected_checkpoint_version,
        ):
            raise SourceImportConflict() from None
        if (
            old_checkpoint is not None
            and old_checkpoint.mapping_version != mapping_version
        ):
            raise SourceImportConflict() from None
        # A non-empty page whose receipts are all already durable proves no
        # new import progress, so it cannot move or bump the checkpoint. The
        # caller may still walk source cursors to verify a whole-feed replay.
        # Empty pages are deliberately excluded so they can advance state.
        exact_replay = (
            old_checkpoint is not None
            and imported == 0
            and replayed == len(prepared)
            and replayed > 0
        )
        committed_checkpoint_value = (
            old_checkpoint.checkpoint if exact_replay else checkpoint
        )
        committed_next_cursor = (
            old_checkpoint.next_cursor if exact_replay else next_cursor
        )
        if exact_replay:
            committed_checkpoint = old_checkpoint
        else:
            position_changed = old_checkpoint is None or (
                old_checkpoint.checkpoint != committed_checkpoint_value
                or old_checkpoint.next_cursor != committed_next_cursor
            )
            active_history = (
                ()
                if old_checkpoint is None
                or (position_changed and old_checkpoint.next_cursor is None)
                else old_checkpoint.cursor_history
            )
            if (
                position_changed
                and committed_next_cursor is not None
                and committed_next_cursor in active_history
            ):
                raise SourceImportConflict() from None
            cursor_history = (
                active_history + (committed_next_cursor,)
                if position_changed and committed_next_cursor is not None
                else active_history
            )
            if len(cursor_history) > MAX_IMPORT_CURSOR_HISTORY:
                raise SourceImportConflict() from None
            version = (
                1
                if old_checkpoint is None
                else old_checkpoint.version
                + int(position_changed or imported > 0)
            )
            committed_checkpoint = SourceImportCheckpoint(
                checkpoint=committed_checkpoint_value,
                next_cursor=committed_next_cursor,
                mapping_version=mapping_version,
                cursor_history=cursor_history,
                version=version,
            )
            checkpoints[(source_system, feed)] = committed_checkpoint
        if self._transaction_check is not None:
            self._transaction_check()
        self._receipts = receipts
        self._encounters = encounters
        self._checkpoints = checkpoints
        return SourceImportPageResult(imported, replayed, committed_checkpoint)


class PostgresSourceImportStore:
    """Forced-RLS PostgreSQL implementation used by isolated import jobs."""

    def __init__(
        self,
        dsn: str,
        *,
        practice_id: str,
        transaction_check: Callable[[object], None] | None = None,
    ) -> None:
        self.practice_id = _validate_source_identity(practice_id)
        self.context = PostgresContext(dsn, practice_id=self.practice_id)
        self._transaction_check = transaction_check

    def load_checkpoint(
        self,
        *,
        source_system: str,
        feed: str,
    ) -> SourceImportCheckpoint | None:
        source_system = _validate_name(source_system)
        feed = _validate_name(feed, feed=True)
        with self.context.connection() as conn:
            row = conn.execute(
                """SELECT checkpoint, next_cursor, mapping_version,
                          cursor_history, version
                     FROM public.source_import_checkpoints
                    WHERE practice_id = %s AND source_system = %s AND feed = %s""",
                (self.practice_id, source_system, feed),
            ).fetchone()
        if row is None:
            return None
        return SourceImportCheckpoint(
            checkpoint=row["checkpoint"],
            next_cursor=row["next_cursor"],
            mapping_version=row["mapping_version"],
            cursor_history=tuple(row["cursor_history"]),
            version=int(row["version"]),
        )

    def import_encounter_page(
        self,
        *,
        source_system: str,
        feed: str,
        provenance: str,
        mapping_version: str,
        records: Sequence[SourceEncounterRecord],
        checkpoint: str | None,
        next_cursor: str | None,
        expected_checkpoint_version: int | None = None,
    ) -> SourceImportPageResult:
        source_system = _validate_name(source_system)
        feed = _validate_encounter_feed(feed)
        provenance = _validate_name(provenance, provenance=True)
        mapping_version = _validate_mapping_version(mapping_version)
        checkpoint = _validate_opaque(checkpoint, required=False)
        next_cursor = _validate_opaque(next_cursor, required=False)
        if checkpoint is None and next_cursor is None:
            raise SourceImportSchemaError() from None
        expected_checkpoint_version = _validate_expected_checkpoint_version(
            expected_checkpoint_version,
        )
        prepared = _validated_records(
            records,
            practice_id=self.practice_id,
            source_system=source_system,
            feed=feed,
            mapping_version=mapping_version,
        )
        page_records = _page_records(prepared)

        committed_checkpoint: SourceImportCheckpoint | None = None
        result_imported = 0
        result_replayed = 0
        try:
            with self.context.connection() as conn:
                binding = conn.execute(
                    """SELECT public.cosmaneura_source_import_allowed(
                               %s, %s, %s
                           ) AS allowed""",
                    (self.practice_id, source_system, provenance),
                ).fetchone()
                if binding is None or binding["allowed"] is not True:
                    raise SourceImportConflict() from None
                checkpoint_row = conn.execute(
                    """SELECT checkpoint, next_cursor, mapping_version,
                              cursor_history, version
                         FROM public.source_import_checkpoints
                        WHERE practice_id = %s
                          AND source_system = %s
                          AND feed = %s""",
                    (self.practice_id, source_system, feed),
                ).fetchone()
                current_checkpoint = (
                    None
                    if checkpoint_row is None
                    else SourceImportCheckpoint(
                        checkpoint=checkpoint_row["checkpoint"],
                        next_cursor=checkpoint_row["next_cursor"],
                        mapping_version=checkpoint_row["mapping_version"],
                        cursor_history=tuple(checkpoint_row["cursor_history"]),
                        version=int(checkpoint_row["version"]),
                    )
                )
                if not _checkpoint_matches_expected(
                    current_checkpoint,
                    expected_checkpoint_version,
                ):
                    raise SourceImportConflict() from None
                if (
                    current_checkpoint is not None
                    and current_checkpoint.mapping_version != mapping_version
                ):
                    raise SourceImportConflict() from None
                result = conn.execute(
                    """SELECT *
                         FROM public.cosmaneura_commit_source_encounter_page(
                             %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                         )""",
                    (
                        self.practice_id,
                        source_system,
                        feed,
                        provenance,
                        mapping_version,
                        expected_checkpoint_version,
                        self.context.Jsonb(page_records),
                        checkpoint,
                        next_cursor,
                    ),
                ).fetchone()
                if result is None:
                    raise SourceImportConflict() from None
                result_imported = int(result["result_imported_count"])
                result_replayed = int(result["result_replayed_count"])
                committed_checkpoint = SourceImportCheckpoint(
                    checkpoint=result["result_checkpoint"],
                    next_cursor=result["result_next_cursor"],
                    mapping_version=result["result_mapping_version"],
                    cursor_history=tuple(result["result_cursor_history"]),
                    version=int(result["result_version"]),
                )
                if self._transaction_check is not None:
                    self._transaction_check(conn)
        except SourceImportError:
            raise
        except PersistenceUnavailable as exc:
            if getattr(exc.__cause__, "sqlstate", None) in {
                "23505",  # unique_violation: receipt/provenance conflict
                "23514",  # check_violation: binding/mapping/cursor conflict
                "40001",  # serialization_failure: checkpoint CAS conflict
            }:
                raise SourceImportConflict() from None
            raise SourceImportError() from None
        if committed_checkpoint is None:  # pragma: no cover - defensive
            raise SourceImportError() from None
        return SourceImportPageResult(
            result_imported,
            result_replayed,
            committed_checkpoint,
        )


__all__ = [
    "MAX_IMPORT_CURSOR_BYTES",
    "MAX_IMPORT_CURSOR_HISTORY",
    "MAX_IMPORT_PAGE_BYTES",
    "MAX_IMPORT_RECORDS",
    "MemorySourceImportStore",
    "PostgresSourceImportStore",
    "SourceEncounterRecord",
    "SourceImportCheckpoint",
    "SourceImportConflict",
    "SourceImportError",
    "SourceImportPageResult",
    "SourceImportSchemaError",
    "SourceImportStore",
    "source_import_external_id",
    "source_import_idempotency_key",
]
