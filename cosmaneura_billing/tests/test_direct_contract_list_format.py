"""Validate the documented direct-contract roster file format (N4).

Proves the committed synthetic samples load through the existing gate loader and
that malformed/missing rosters fail closed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cosmaneura_billing.direct_contract import (
    DirectContractBlocked,
    DirectContractList,
    check_direct_contract,
    chase_routing,
    guard_patient_charge,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DIR = REPO_ROOT / "docs" / "contracts" / "direct-contract-list" / "v1"
NOW = lambda: "2026-09-03T00:00:00+00:00"  # noqa: E731


def test_samples_exist():
    assert (SAMPLE_DIR / "README.md").is_file()
    assert (SAMPLE_DIR / "sample_roster.csv").is_file()
    assert (SAMPLE_DIR / "sample_roster.json").is_file()


@pytest.mark.parametrize("filename", ["sample_roster.csv", "sample_roster.json"])
def test_sample_roster_loads_and_matches(filename):
    roster = DirectContractList(path=SAMPLE_DIR / filename)
    assert roster.available is True
    # Case-insensitive match against the documented normalization.
    hit = check_direct_contract("synth-dc-0001", roster=roster, now=NOW)
    assert hit.known is True and hit.is_direct_contract is True and hit.blocked is True
    miss = check_direct_contract("not-on-list", roster=roster, now=NOW)
    assert miss.known is True and miss.is_direct_contract is False and miss.blocked is False


@pytest.mark.parametrize("filename", ["sample_roster.csv", "sample_roster.json"])
def test_sample_roster_gate_policies(filename):
    roster = DirectContractList(path=SAMPLE_DIR / filename)
    # Charging a listed patient hard-stops; a non-listed one proceeds.
    with pytest.raises(DirectContractBlocked):
        guard_patient_charge("SYNTH-DC-0002", roster=roster, now=NOW)
    assert guard_patient_charge("not-on-list", roster=roster, now=NOW).blocked is False
    # Chase routes a match to human review, a non-match to auto.
    assert chase_routing("SYNTH-DC-0002", roster=roster, now=NOW)[1] == "review"
    assert chase_routing("not-on-list", roster=roster, now=NOW)[1] == "auto"


def test_csv_without_patient_ref_column_fails_closed(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("name,plan\nAlice,Gold\n", encoding="utf-8")
    roster = DirectContractList(path=bad)
    assert roster.available is False
    check = check_direct_contract("anyone", roster=roster, now=NOW)
    assert check.known is False and check.blocked is True
    with pytest.raises(DirectContractBlocked):
        guard_patient_charge("anyone", roster=roster, now=NOW)


def test_missing_file_fails_closed(tmp_path):
    roster = DirectContractList(path=tmp_path / "does-not-exist.csv")
    assert roster.available is False
    assert check_direct_contract("anyone", roster=roster, now=NOW).blocked is True


def test_malformed_json_fails_closed(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"patients": [123]}', encoding="utf-8")
    roster = DirectContractList(path=bad)
    assert roster.available is False
    assert check_direct_contract("anyone", roster=roster, now=NOW).blocked is True
