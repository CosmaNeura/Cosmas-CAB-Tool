"""Tests for the de-identified Scribe visit ingestion adapter (A5).

They exercise the adapter against the in-memory durable import store: happy-path
import, idempotent replay, changed-payload conflict, version-bump update, and a
matrix of fail-closed schema rejections. No PostgreSQL is required.
"""
from __future__ import annotations

import copy

import pytest

from cosmaneura_billing.schemas import VisitBundle
from cosmaneura_billing.source_import import (
    MemorySourceImportStore,
    SourceImportConflict,
    SourceImportSchemaError,
    source_import_external_id,
    source_import_idempotency_key,
)
from cosmaneura_billing import scribe_import
from cosmaneura_billing.scribe_import import (
    ENCOUNTER_FEED,
    INGEST_CONTRACT_VERSION,
    SCRIBE_DEIDENTIFIED_PROVENANCE,
    SCRIBE_SOURCE_SYSTEM,
    ScribeEncounterRecord,
    ingest_visit_page,
    normalize_records,
)

PRACTICE = "synthetic-pilot"


def _visit(**overrides):
    visit = {
        "encounter_type": "office_established",
        "place_of_service": "11",
        "patient_age": 26,
        "patient_sex": "M",
        "signed_note": "A/P: hematochezia, refer for colonoscopy.",
        "intake_text": "When I eat spicy food, I poop a little blood.",
        "transcript": "We'll refer you for a colonoscopy to be safe.",
        "provider_specialty": "Family Medicine",
        "submitted_em_code": "99213",
        "submitted_icd10": ["K62.5"],
    }
    visit.update(overrides)
    return visit


def _record(rec_id="enc-0001", version="v1", **visit_overrides):
    return {
        "source_record_id": rec_id,
        "source_version": version,
        "visit": _visit(**visit_overrides),
    }


def _page(batch_id="batch-0001", records=None):
    return {
        "contract_version": INGEST_CONTRACT_VERSION,
        "batch_id": batch_id,
        "records": records if records is not None else [_record()],
    }


def _store():
    return MemorySourceImportStore(practice_id=PRACTICE)


def test_ingest_imports_and_reads_back():
    store = _store()
    result = ingest_visit_page(
        store, _page(records=[_record("enc-1"), _record("enc-2")]),
        practice_id=PRACTICE,
    )
    assert result.imported_count == 2
    assert result.replayed_count == 0

    external_id = source_import_external_id(
        practice_id=PRACTICE,
        source_system=SCRIBE_SOURCE_SYSTEM,
        feed=ENCOUNTER_FEED,
        source_record_id="enc-1",
    )
    stored = store.get_encounter(external_id)
    assert stored is not None
    assert stored.source == SCRIBE_DEIDENTIFIED_PROVENANCE
    assert isinstance(stored.bundle, VisitBundle)
    assert stored.bundle.encounter_id == external_id
    assert stored.bundle.patient_age == 26
    assert stored.bundle.submitted_em_code == "99213"
    assert stored.bundle.submitted_icd10 == ["K62.5"]


def test_idempotent_replay_does_not_advance():
    store = _store()
    page = _page(records=[_record("enc-1"), _record("enc-2")])
    first = ingest_visit_page(store, page, practice_id=PRACTICE)
    second = ingest_visit_page(store, copy.deepcopy(page), practice_id=PRACTICE)
    assert second.imported_count == 0
    assert second.replayed_count == 2
    assert second.checkpoint.version == first.checkpoint.version


def test_changed_payload_same_version_conflicts():
    store = _store()
    ingest_visit_page(store, _page(records=[_record("enc-1")]), practice_id=PRACTICE)
    mutated = _page(
        batch_id="batch-0002",
        records=[_record("enc-1", version="v1", signed_note="A/P: different note.")],
    )
    with pytest.raises(SourceImportConflict):
        ingest_visit_page(store, mutated, practice_id=PRACTICE)


def test_new_version_reimports_update():
    store = _store()
    ingest_visit_page(store, _page(records=[_record("enc-1", version="v1")]),
                      practice_id=PRACTICE)
    update = _page(
        batch_id="batch-0002",
        records=[_record("enc-1", version="v2", signed_note="A/P: updated note.")],
    )
    result = ingest_visit_page(store, update, practice_id=PRACTICE)
    assert result.imported_count == 1
    external_id = source_import_external_id(
        practice_id=PRACTICE,
        source_system=SCRIBE_SOURCE_SYSTEM,
        feed=ENCOUNTER_FEED,
        source_record_id="enc-1",
    )
    stored = store.get_encounter(external_id)
    assert stored.bundle.signed_note == "A/P: updated note."
    assert stored.version == 2


def test_normalized_idempotency_key_matches_helper():
    (record,) = normalize_records([_record("enc-9", version="v3")], practice_id=PRACTICE)
    assert isinstance(record, ScribeEncounterRecord)
    expected = source_import_idempotency_key(
        practice_id=PRACTICE,
        source_system=SCRIBE_SOURCE_SYSTEM,
        feed=ENCOUNTER_FEED,
        source_record_id="enc-9",
        source_version="v3",
        mapping_version=INGEST_CONTRACT_VERSION,
    )
    assert record.idempotency_key == expected
    assert record.mapping_version == INGEST_CONTRACT_VERSION


def test_repr_does_not_leak_visit_text():
    (record,) = normalize_records([_record("enc-1")], practice_id=PRACTICE)
    text = repr(record)
    assert "colonoscopy" not in text
    assert "poop" not in text
    assert "ScribeEncounterRecord" in text


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("contract_version"), id="missing_contract_version"),
        pytest.param(lambda p: p.update(contract_version="scribe.visit.v2"),
                     id="wrong_contract_version"),
        pytest.param(lambda p: p.pop("batch_id"), id="missing_batch_id"),
        pytest.param(lambda p: p.update(batch_id=""), id="empty_batch_id"),
        pytest.param(lambda p: p.update(batch_id="bad\ncursor"), id="control_char_batch_id"),
        pytest.param(lambda p: p.update(records=[]), id="empty_records"),
        pytest.param(lambda p: p.update(records="not-a-list"), id="records_not_list"),
        pytest.param(lambda p: p.update(unexpected=1), id="unknown_page_key"),
    ],
)
def test_fail_closed_page_schema(mutate):
    page = _page()
    mutate(page)
    with pytest.raises(SourceImportSchemaError):
        ingest_visit_page(_store(), page, practice_id=PRACTICE)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.pop("visit"), id="missing_visit"),
        pytest.param(lambda r: r.pop("source_record_id"), id="missing_source_record_id"),
        pytest.param(lambda r: r.update(extra="x"), id="unknown_record_key"),
        pytest.param(lambda r: r["visit"].pop("patient_age"), id="missing_required_visit_key"),
        pytest.param(lambda r: r["visit"].update(unexpected="x"), id="unknown_visit_key"),
        pytest.param(lambda r: r["visit"].update(signed_note=""), id="empty_signed_note"),
        pytest.param(lambda r: r["visit"].update(patient_age=200), id="age_out_of_range"),
        pytest.param(lambda r: r["visit"].update(patient_age=True), id="age_is_bool"),
        pytest.param(lambda r: r["visit"].update(patient_age="26"), id="age_is_str"),
        pytest.param(lambda r: r["visit"].update(submitted_icd10="K62.5"), id="icd10_not_list"),
        pytest.param(lambda r: r["visit"].update(submitted_icd10=["x"] * 65), id="icd10_too_long"),
        pytest.param(lambda r: r.update(source_record_id=""), id="empty_source_record_id"),
    ],
)
def test_fail_closed_record_schema(mutate):
    record = _record()
    mutate(record)
    with pytest.raises(SourceImportSchemaError):
        ingest_visit_page(_store(), _page(records=[record]), practice_id=PRACTICE)


def test_oversized_text_field_rejected():
    huge = "x" * (scribe_import.MAX_TEXT_FIELD_CHARS + 1)
    with pytest.raises(SourceImportSchemaError):
        normalize_records([_record(signed_note=huge)], practice_id=PRACTICE)
