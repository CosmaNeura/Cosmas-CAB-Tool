"""Tests for the de-identified ingestion entrypoint (N2)."""
from __future__ import annotations

import json

import pytest

from cosmaneura_billing.scribe_import import (
    ENCOUNTER_FEED,
    INGEST_CONTRACT_VERSION,
    SCRIBE_SOURCE_SYSTEM,
    source_import_external_id,
)
from cosmaneura_billing.scribe_import_cli import main
from cosmaneura_billing.source_import import MemorySourceImportStore

PRACTICE = "synthetic-pilot"


def _visit(**overrides):
    v = {
        "encounter_type": "office_established",
        "place_of_service": "11",
        "patient_age": 26,
        "patient_sex": "M",
        "signed_note": "A/P: hematochezia, refer for colonoscopy.",
    }
    v.update(overrides)
    return v


def _page(batch_id="batch-1", records=None):
    return {
        "contract_version": INGEST_CONTRACT_VERSION,
        "batch_id": batch_id,
        "records": records if records is not None else [
            {"source_record_id": "enc-1", "source_version": "v1", "visit": _visit()},
        ],
    }


def _write(tmp_path, obj):
    path = tmp_path / "page.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def _shared_store_factory(store):
    def factory(*, dsn, practice_id):
        return store
    return factory


def test_valid_page_imports(tmp_path, capsys):
    store = MemorySourceImportStore(practice_id=PRACTICE)
    code = main(
        ["--page", _write(tmp_path, _page()), "--practice-id", PRACTICE],
        store_factory=_shared_store_factory(store),
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["code"] == "IMPORTED" and out["imported"] == 1 and out["replayed"] == 0

    external_id = source_import_external_id(
        practice_id=PRACTICE, source_system=SCRIBE_SOURCE_SYSTEM,
        feed=ENCOUNTER_FEED, source_record_id="enc-1",
    )
    assert store.get_encounter(external_id) is not None


def test_replay_reports_replayed(tmp_path, capsys):
    store = MemorySourceImportStore(practice_id=PRACTICE)
    factory = _shared_store_factory(store)
    page_path = _write(tmp_path, _page())
    assert main(["--page", page_path, "--practice-id", PRACTICE], store_factory=factory) == 0
    capsys.readouterr()
    assert main(["--page", page_path, "--practice-id", PRACTICE], store_factory=factory) == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["imported"] == 0 and out["replayed"] == 1


def test_malformed_page_fails_closed(tmp_path, capsys):
    bad = _page()
    bad["records"][0]["visit"].pop("patient_age")
    code = main(
        ["--page", _write(tmp_path, bad), "--practice-id", PRACTICE],
        store_factory=_shared_store_factory(MemorySourceImportStore(practice_id=PRACTICE)),
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out.strip())["code"] == "SOURCE_IMPORT_SCHEMA_INVALID"


def test_wrong_contract_version_fails_closed(tmp_path, capsys):
    bad = _page()
    bad["contract_version"] = "scribe.visit.v2"
    code = main(
        ["--page", _write(tmp_path, bad), "--practice-id", PRACTICE],
        store_factory=_shared_store_factory(MemorySourceImportStore(practice_id=PRACTICE)),
    )
    assert code == 1


def test_missing_file_is_input_error(tmp_path, capsys):
    code = main(["--page", str(tmp_path / "nope.json"), "--practice-id", PRACTICE])
    assert code == 2
    assert json.loads(capsys.readouterr().out.strip())["code"] == "PAGE_UNREADABLE"


def test_invalid_json_is_input_error(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    code = main(["--page", str(path), "--practice-id", PRACTICE])
    assert code == 2


def test_missing_required_arg_exits(capsys):
    with pytest.raises(SystemExit):
        main(["--page", "x.json"])  # missing --practice-id


def test_dry_run_reports_not_durable(tmp_path, capsys):
    # No --dsn and no injected factory -> default in-memory store, durable=False.
    code = main(["--page", _write(tmp_path, _page()), "--practice-id", PRACTICE])
    assert code == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["durable"] is False
