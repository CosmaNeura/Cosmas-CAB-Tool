"""Source-agnostic import receipts, checkpoints, and atomic page behavior."""
from __future__ import annotations

from dataclasses import replace

import pytest

from cosmaneura_billing.athena_client import MockAthenaClient
from cosmaneura_billing.persistence import (
    ATHENA_PREVIEW_SYNTHETIC_SOURCE,
    MemoryEncounterStore,
    PersistenceUnavailable,
)
from cosmaneura_billing.source_import import (
    MAX_IMPORT_PAGE_BYTES,
    MemorySourceImportStore,
    SourceImportConflict,
    SourceImportSchemaError,
    source_import_external_id,
    source_import_idempotency_key,
)


SOURCE = "athena_preview"
FEED = "encounters"
PRACTICE = "server-practice"
PROVENANCE = ATHENA_PREVIEW_SYNTHETIC_SOURCE


def _first_page():
    return MockAthenaClient(server_practice_id=PRACTICE).read_page()


def _external_id(record) -> str:
    return source_import_external_id(
        practice_id=PRACTICE,
        source_system=SOURCE,
        feed=FEED,
        source_record_id=record.source_record_id,
    )


def test_page_commit_persists_records_receipts_and_opaque_continuation():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()

    outcome = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=page.records[0].mapping_version,
        records=page.records,
        checkpoint=page.checkpoint,
        next_cursor=page.next_cursor,
    )

    assert outcome.imported_count == 2
    assert outcome.replayed_count == 0
    stored = store.get_encounter(_external_id(page.records[0]))
    assert stored is not None
    assert stored.source == PROVENANCE
    assert stored.bundle.encounter_id != page.records[0].visit.encounter_id
    state = store.load_checkpoint(source_system=SOURCE, feed=FEED)
    assert state is not None
    assert state.next_cursor == page.next_cursor
    assert page.next_cursor not in repr(state)


def test_exact_replay_records_receipts_without_mutating_encounter_versions():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    arguments = {
        "source_system": SOURCE,
        "feed": FEED,
        "provenance": PROVENANCE,
        "mapping_version": page.records[0].mapping_version,
        "records": page.records,
        "checkpoint": page.checkpoint,
        "next_cursor": page.next_cursor,
    }

    first = store.import_encounter_page(**arguments)
    encounter_id = _external_id(page.records[0])
    version = store.get_encounter(encounter_id).version
    second = store.import_encounter_page(
        **{
            **arguments,
            "expected_checkpoint_version": first.checkpoint.version,
        }
    )

    assert first.imported_count == 2
    assert second.imported_count == 0
    assert second.replayed_count == 2
    assert second.checkpoint == first.checkpoint
    assert store.get_encounter(encounter_id).version == version

    advanced_source_position = store.import_encounter_page(
        **{
            **arguments,
            "checkpoint": "different-source-checkpoint",
            "next_cursor": "different-source-cursor",
            "expected_checkpoint_version": first.checkpoint.version,
        }
    )
    assert advanced_source_position.imported_count == 0
    assert advanced_source_position.replayed_count == 2
    assert advanced_source_position.checkpoint == first.checkpoint


def test_new_receipt_at_same_position_bumps_cas_and_stale_writer_is_atomic():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    original = page.records[0]
    first = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=original.mapping_version,
        records=(original,),
        checkpoint=page.checkpoint,
        next_cursor=page.next_cursor,
    )
    changed = replace(
        original,
        source_version="same-position-version-2",
        idempotency_key=source_import_idempotency_key(
            practice_id=PRACTICE,
            source_system=SOURCE,
            feed=FEED,
            source_record_id=original.source_record_id,
            source_version="same-position-version-2",
            mapping_version=original.mapping_version,
        ),
        visit=replace(original.visit, signed_note="Same-position correction."),
    )

    winner = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=changed.mapping_version,
        records=(changed,),
        checkpoint=first.checkpoint.checkpoint,
        next_cursor=first.checkpoint.next_cursor,
        expected_checkpoint_version=first.checkpoint.version,
    )

    assert winner.imported_count == 1
    assert winner.checkpoint.version == first.checkpoint.version + 1
    assert winner.checkpoint.checkpoint == first.checkpoint.checkpoint
    assert winner.checkpoint.next_cursor == first.checkpoint.next_cursor

    stale_record = page.records[1]
    with pytest.raises(SourceImportConflict):
        store.import_encounter_page(
            source_system=SOURCE,
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=stale_record.mapping_version,
            records=(stale_record,),
            checkpoint=winner.checkpoint.checkpoint,
            next_cursor=winner.checkpoint.next_cursor,
            expected_checkpoint_version=first.checkpoint.version,
        )

    assert store.load_checkpoint(source_system=SOURCE, feed=FEED) == (
        winner.checkpoint
    )
    assert store.get_encounter(_external_id(stale_record)) is None


def test_new_source_version_updates_exactly_one_encounter():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    initial = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=page.records[0].mapping_version,
        records=page.records,
        checkpoint=page.checkpoint,
        next_cursor=page.next_cursor,
    )
    original = page.records[0]
    changed = replace(
        original,
        source_version="version-2",
        idempotency_key=source_import_idempotency_key(
            practice_id=PRACTICE,
            source_system=SOURCE,
            feed=FEED,
            source_record_id=original.source_record_id,
            source_version="version-2",
            mapping_version=original.mapping_version,
        ),
        visit=replace(original.visit, signed_note=original.visit.signed_note + " Updated."),
    )

    outcome = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=changed.mapping_version,
        records=(changed,),
        checkpoint="mock-checkpoint-v2",
        next_cursor=None,
        expected_checkpoint_version=initial.checkpoint.version,
    )

    assert outcome.imported_count == 1
    assert outcome.replayed_count == 0
    stored = store.get_encounter(_external_id(original))
    assert stored.version == 2
    assert stored.bundle.signed_note.endswith(" Updated.")


def test_mapping_version_change_requires_explicit_restart_or_backfill():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    original = page.records[0]
    initial = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=original.mapping_version,
        records=(original,),
        checkpoint=page.checkpoint,
        next_cursor=page.next_cursor,
    )
    mapping_version = "cosmaneura-athena-normalized-v2"
    remapped = replace(
        original,
        mapping_version=mapping_version,
        idempotency_key=source_import_idempotency_key(
            practice_id=PRACTICE,
            source_system=SOURCE,
            feed=FEED,
            source_record_id=original.source_record_id,
            source_version=original.source_version,
            mapping_version=mapping_version,
        ),
        visit=replace(
            original.visit,
            signed_note=original.visit.signed_note + " Remapped.",
        ),
    )

    with pytest.raises(SourceImportConflict):
        store.import_encounter_page(
            source_system=SOURCE,
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=remapped.mapping_version,
            records=(remapped,),
            checkpoint="mock-checkpoint-mapping-v2",
            next_cursor=None,
            expected_checkpoint_version=initial.checkpoint.version,
        )

    stored = store.get_encounter(_external_id(original))
    assert stored.version == 1
    assert stored.bundle.signed_note == original.visit.signed_note
    assert store.load_checkpoint(source_system=SOURCE, feed=FEED) == (
        initial.checkpoint
    )


def test_page_failure_rolls_back_records_receipts_and_checkpoint():
    calls = 0

    def fail_before_commit() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic transaction failure")

    store = MemorySourceImportStore(
        practice_id=PRACTICE,
        transaction_check=fail_before_commit,
    )
    page = _first_page()
    with pytest.raises(RuntimeError, match="synthetic transaction failure"):
        store.import_encounter_page(
            source_system=SOURCE,
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=page.records[0].mapping_version,
            records=page.records,
            checkpoint=page.checkpoint,
            next_cursor=page.next_cursor,
        )

    assert calls == 1
    assert store.get_encounter(_external_id(page.records[0])) is None
    assert store.load_checkpoint(source_system=SOURCE, feed=FEED) is None


def test_receipt_collision_and_cross_practice_record_fail_closed():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    record = page.records[0]
    arguments = {
        "source_system": SOURCE,
        "feed": FEED,
        "provenance": PROVENANCE,
        "mapping_version": record.mapping_version,
        "records": (record,),
        "checkpoint": page.checkpoint,
        "next_cursor": page.next_cursor,
    }
    first = store.import_encounter_page(**arguments)
    arguments["expected_checkpoint_version"] = first.checkpoint.version

    collision = replace(
        record,
        visit=replace(record.visit, signed_note=record.visit.signed_note + " changed"),
    )
    with pytest.raises(SourceImportConflict) as caught:
        store.import_encounter_page(**{**arguments, "records": (collision,)})
    assert str(caught.value) == "SOURCE_IMPORT_CONFLICT"

    cross_practice = replace(record, target_practice_id="other-practice")
    with pytest.raises(SourceImportSchemaError):
        store.import_encounter_page(**{**arguments, "records": (cross_practice,)})

    mismatched_version = replace(record, source_version="unexpected-version")
    with pytest.raises(SourceImportSchemaError):
        store.import_encounter_page(
            **{**arguments, "records": (mismatched_version,)}
        )


def test_stale_checkpoint_writer_cannot_regress_committed_state():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    first = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=page.records[0].mapping_version,
        records=page.records,
        checkpoint=page.checkpoint,
        next_cursor=page.next_cursor,
    )
    second_page = MockAthenaClient(server_practice_id=PRACTICE).read_page(
        page.next_cursor,
    )
    winner = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=second_page.records[0].mapping_version,
        records=second_page.records,
        checkpoint=second_page.checkpoint,
        next_cursor=second_page.next_cursor,
        expected_checkpoint_version=first.checkpoint.version,
    )

    with pytest.raises(SourceImportConflict):
        store.import_encounter_page(
            source_system=SOURCE,
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=page.records[0].mapping_version,
            records=page.records,
            checkpoint=page.checkpoint,
            next_cursor=page.next_cursor,
            expected_checkpoint_version=first.checkpoint.version,
        )
    assert store.load_checkpoint(source_system=SOURCE, feed=FEED) == (
        winner.checkpoint
    )


def test_generic_encounter_store_cannot_claim_reserved_athena_provenance():
    page = _first_page()
    with pytest.raises(PersistenceUnavailable, match="source import store"):
        MemoryEncounterStore(practice_id=PRACTICE).upsert(
            page.records[0].visit,
            source=PROVENANCE,
        )


def test_names_and_opaque_state_are_bounded_and_never_rendered():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    with pytest.raises(SourceImportSchemaError):
        store.import_encounter_page(
            source_system="Athena Preview",
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=page.records[0].mapping_version,
            records=page.records,
            checkpoint=page.checkpoint,
            next_cursor=page.next_cursor,
        )
    with pytest.raises(SourceImportSchemaError):
        store.import_encounter_page(
            source_system=SOURCE,
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=page.records[0].mapping_version,
            records=page.records,
            checkpoint="x" * 513,
            next_cursor=None,
        )
    with pytest.raises(SourceImportSchemaError):
        store.import_encounter_page(
            source_system=SOURCE,
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=page.records[0].mapping_version,
            records=page.records,
            checkpoint="é" * 300,
            next_cursor=None,
        )


def test_encounter_page_rejects_other_feeds_without_mutation():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()

    with pytest.raises(SourceImportSchemaError):
        store.import_encounter_page(
            source_system=SOURCE,
            feed="coverage",
            provenance=PROVENANCE,
            mapping_version=page.records[0].mapping_version,
            records=page.records,
            checkpoint=page.checkpoint,
            next_cursor=page.next_cursor,
        )

    assert store.load_checkpoint(source_system=SOURCE, feed="coverage") is None
    assert store.load_checkpoint(source_system=SOURCE, feed=FEED) is None
    assert store.get_encounter(_external_id(page.records[0])) is None


def test_empty_page_can_advance_a_terminal_checkpoint():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    page = _first_page()
    terminal = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=page.records[0].mapping_version,
        records=page.records,
        checkpoint="terminal-checkpoint",
        next_cursor=None,
    )

    advanced = store.import_encounter_page(
        source_system=SOURCE,
        feed=FEED,
        provenance=PROVENANCE,
        mapping_version=page.records[0].mapping_version,
        records=(),
        checkpoint="empty-page-checkpoint",
        next_cursor="cursor-after-empty-page",
        expected_checkpoint_version=terminal.checkpoint.version,
    )

    assert (advanced.imported_count, advanced.replayed_count) == (0, 0)
    assert advanced.checkpoint.checkpoint == "empty-page-checkpoint"
    assert advanced.checkpoint.next_cursor == "cursor-after-empty-page"
    assert advanced.checkpoint.version == terminal.checkpoint.version + 1


def test_memory_store_rejects_the_same_oversized_page_as_postgres():
    store = MemorySourceImportStore(practice_id=PRACTICE)
    original = _first_page().records[0]
    transcript = "x" * 500_000
    records = []
    for ordinal in range((MAX_IMPORT_PAGE_BYTES // len(transcript)) + 2):
        source_record_id = f"oversized-record-{ordinal}"
        records.append(replace(
            original,
            source_record_id=source_record_id,
            idempotency_key=source_import_idempotency_key(
                practice_id=PRACTICE,
                source_system=SOURCE,
                feed=FEED,
                source_record_id=source_record_id,
                source_version=original.source_version,
                mapping_version=original.mapping_version,
            ),
            visit=replace(
                original.visit,
                encounter_id=source_record_id,
                transcript=transcript,
            ),
        ))

    with pytest.raises(SourceImportSchemaError):
        store.import_encounter_page(
            source_system=SOURCE,
            feed=FEED,
            provenance=PROVENANCE,
            mapping_version=original.mapping_version,
            records=tuple(records),
            checkpoint="oversized-page",
            next_cursor=None,
        )

    assert store.load_checkpoint(source_system=SOURCE, feed=FEED) is None


def test_canonical_replay_key_binds_namespace_record_and_version_without_echo():
    values = {
        "practice_id": PRACTICE,
        "source_system": SOURCE,
        "feed": FEED,
        "source_record_id": "patient-sensitive-source-id",
        "source_version": "vendor-version-1",
        "mapping_version": "normalizer-v1",
    }
    key = source_import_idempotency_key(**values)
    external_id = source_import_external_id(
        practice_id=values["practice_id"],
        source_system=values["source_system"],
        feed=values["feed"],
        source_record_id=values["source_record_id"],
    )

    assert key.startswith("source-import-v1:")
    assert len(key.removeprefix("source-import-v1:")) == 64
    assert values["source_record_id"] not in key
    assert values["source_version"] not in key
    assert values["source_record_id"] not in external_id
    assert len(external_id) <= 128
    assert source_import_external_id(
        practice_id="other-practice",
        source_system=values["source_system"],
        feed=values["feed"],
        source_record_id=values["source_record_id"],
    ) != external_id
    for field, replacement in (
        ("source_system", "mcd"),
        ("practice_id", "other-practice"),
        ("feed", "coverage.eligibility"),
        ("source_record_id", "different-record"),
        ("source_version", "vendor-version-2"),
        ("mapping_version", "normalizer-v2"),
    ):
        assert source_import_idempotency_key(
            **{**values, field: replacement}
        ) != key

    with pytest.raises(SourceImportSchemaError):
        source_import_idempotency_key(
            **{**values, "source_record_id": "unsafe\nidentifier"}
        )
    with pytest.raises(SourceImportSchemaError):
        source_import_idempotency_key(
            **{**values, "source_record_id": "invalid-\ud800-identifier"}
        )
    with pytest.raises(SourceImportSchemaError):
        source_import_external_id(
            practice_id=PRACTICE,
            source_system=SOURCE,
            feed=FEED,
            source_record_id="invalid-\ud800-identifier",
        )
