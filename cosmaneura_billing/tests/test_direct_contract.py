"""F3 acceptance: match, non-match, and every unverifiable path fails closed."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from cosmaneura_billing.guards import PracticeListSource, check_direct_contract

NOW = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)


def _source(tmp_path, refs=("PT-DC-0001",), version="practice_list.v1"):
    p = tmp_path / "list.json"
    p.write_text(json.dumps({"version": version, "patient_refs": list(refs)}))
    return PracticeListSource(p)


def test_match_blocks(tmp_path):
    chk = check_direct_contract("PT-DC-0001", _source(tmp_path), now=NOW)
    assert chk.is_direct_contract is True
    assert chk.blocked is True
    assert chk.status == "match"
    assert chk.source == "practice_list.v1"


def test_match_is_case_and_whitespace_insensitive(tmp_path):
    chk = check_direct_contract("  pt-dc-0001 ", _source(tmp_path), now=NOW)
    assert chk.blocked is True and chk.status == "match"


def test_non_match_passes(tmp_path):
    chk = check_direct_contract("PT-0999", _source(tmp_path), now=NOW)
    assert chk.is_direct_contract is False
    assert chk.blocked is False
    assert chk.status == "no_match"
    assert chk.needs_human_review is False


def test_missing_list_fails_closed(tmp_path):
    src = PracticeListSource(tmp_path / "nope.json")
    chk = check_direct_contract("PT-0999", src, now=NOW)
    assert chk.blocked is True
    assert chk.status == "unknown_fail_closed"
    assert chk.needs_human_review is True
    assert chk.source == "unavailable"


def test_malformed_list_fails_closed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    chk = check_direct_contract("PT-0999", PracticeListSource(p), now=NOW)
    assert chk.blocked is True and chk.status == "unknown_fail_closed"


def test_wrong_shape_fails_closed(tmp_path):
    p = tmp_path / "shape.json"
    p.write_text(json.dumps({"version": "v1", "patient_refs": "not-a-list"}))
    chk = check_direct_contract("PT-0999", PracticeListSource(p), now=NOW)
    assert chk.blocked is True and chk.status == "unknown_fail_closed"


def test_empty_patient_ref_fails_closed(tmp_path):
    chk = check_direct_contract("", _source(tmp_path), now=NOW)
    assert chk.blocked is True and chk.status == "unknown_fail_closed"


def test_bundled_synthetic_list_loads():
    src = PracticeListSource()  # bundled default
    assert src.available()
    assert check_direct_contract("PT-DC-0002", src, now=NOW).blocked is True
    assert check_direct_contract("PT-3141", src, now=NOW).blocked is False


def test_wire_shape_matches_contract(tmp_path):
    j = check_direct_contract("PT-DC-0001", _source(tmp_path), now=NOW).to_json()
    assert set(j) == {"is_direct_contract", "source", "checked_at", "status"}
    assert j["checked_at"].startswith("2026-08-12T21:00")
