"""The direct-contract gate (F3).

Acceptance criteria from the requirements synthesis §3 F3: unit tests for match,
non-match, and missing-list fail-closed; plus an integration test proving a
direct-contract patient can never enter a chase sequence (that one lives in
test_chase.py, where the sequence does).
"""
import pytest

from cosmaneura_billing.direct_contract import (
    DirectContractBlocked, DirectContractList, chase_routing,
    check_direct_contract, guard_patient_charge,
)

CLOCK = lambda: "2026-08-17T12:00:00+00:00"  # noqa: E731
ROSTER = DirectContractList(refs=["PT-1001", "PT-1002"], version="test")


def _check(ref, roster=ROSTER):
    return check_direct_contract(ref, roster=roster, now=CLOCK)


# --------------------------------------------------------------------------- #
# The three required cases
# --------------------------------------------------------------------------- #
def test_match():
    c = _check("PT-1001")
    assert c.is_direct_contract and c.known and c.blocked


def test_non_match():
    c = _check("PT-9999")
    assert not c.is_direct_contract and c.known and not c.blocked


def test_missing_list_fails_closed(tmp_path):
    roster = DirectContractList(path=tmp_path / "nope.csv")
    c = _check("PT-9999", roster)
    assert not c.is_direct_contract
    assert not c.known, "an unreadable list must not report a confident answer"
    assert c.blocked, "unknown must block"
    assert "unavailable" in c.source


def test_unparseable_list_fails_closed(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("some,other,columns\n1,2,3\n", encoding="utf-8")
    c = _check("PT-1001", DirectContractList(path=path))
    assert not c.known and c.blocked


# --------------------------------------------------------------------------- #
# List loading
# --------------------------------------------------------------------------- #
def test_csv_list(tmp_path):
    path = tmp_path / "dc.csv"
    path.write_text("patient_ref,name\nPT-1001,Alice\nPT-1002,Bob\n", encoding="utf-8")
    roster = DirectContractList(path=path)
    assert roster.available
    assert _check("PT-1001", roster).is_direct_contract
    assert not _check("PT-1003", roster).is_direct_contract


def test_json_list(tmp_path):
    path = tmp_path / "dc.json"
    path.write_text('{"patients": [{"patient_ref": "PT-1001"}, "PT-1002"]}',
                    encoding="utf-8")
    roster = DirectContractList(path=path)
    assert _check("PT-1002", roster).is_direct_contract


@pytest.mark.parametrize("ref", ["pt-1001", "  PT-1001  ", "PT-1001"])
def test_matching_ignores_case_and_padding(ref):
    """A trailing space in a spreadsheet export must not charge a patient."""
    assert _check(ref).is_direct_contract


def test_empty_but_readable_list_is_a_confident_no():
    """An empty roster is a real answer — the practice has no direct-contract
    patients — and must not be confused with a missing file."""
    roster = DirectContractList(refs=[])
    c = _check("PT-1001", roster)
    assert c.known and not c.is_direct_contract and not c.blocked


# --------------------------------------------------------------------------- #
# Charging policy: hard stop
# --------------------------------------------------------------------------- #
def test_charge_guard_raises_on_a_match():
    with pytest.raises(DirectContractBlocked) as e:
        guard_patient_charge("PT-1001", roster=ROSTER, now=CLOCK)
    assert "direct-contract list" in str(e.value)
    assert e.value.check.is_direct_contract


def test_charge_guard_raises_on_unknown(tmp_path):
    roster = DirectContractList(path=tmp_path / "missing.csv")
    with pytest.raises(DirectContractBlocked) as e:
        guard_patient_charge("PT-9999", roster=roster, now=CLOCK)
    assert "unknown" in str(e.value)


def test_charge_guard_allows_a_non_match():
    check = guard_patient_charge("PT-9999", roster=ROSTER, now=CLOCK)
    assert check.known and not check.blocked


def test_charge_guard_raises_rather_than_returning_a_flag():
    """A caller who forgets to inspect a boolean charges the patient; a caller
    who forgets to catch an exception crashes. Only one of those is safe."""
    assert issubclass(DirectContractBlocked, PermissionError)


# --------------------------------------------------------------------------- #
# Chase policy: review, never silent suppression
# --------------------------------------------------------------------------- #
def test_chase_routes_direct_contract_to_review():
    check, route = chase_routing("PT-1001", roster=ROSTER, now=CLOCK)
    assert route == "review"
    assert check.is_direct_contract


def test_chase_routes_unknown_to_review(tmp_path):
    _, route = chase_routing(
        "PT-9999", roster=DirectContractList(path=tmp_path / "x.csv"), now=CLOCK)
    assert route == "review"


def test_chase_allows_auto_for_a_clear_non_match():
    _, route = chase_routing("PT-9999", roster=ROSTER, now=CLOCK)
    assert route == "auto"


def test_chase_never_suppresses():
    """Dropping the patient off the worklist would hide the open policy question
    (§8 Q11) instead of surfacing it for a human."""
    routes = {chase_routing(r, roster=ROSTER, now=CLOCK)[1]
              for r in ("PT-1001", "PT-9999")}
    assert routes <= {"auto", "review"}


def test_check_serializes_with_its_uncertainty_visible():
    body = _check("PT-1001").to_dict()
    assert body["blocked"] is True and body["known"] is True
    assert body["checked_at"] == "2026-08-17T12:00:00+00:00"
    assert set(body) == {"patient_ref", "is_direct_contract", "known", "blocked",
                         "source", "checked_at"}
