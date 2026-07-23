"""2021+ E/M leveling: the MDM 2-of-3 grid and documented-time leveling."""
import pytest

from cosmaneura_billing.knowledge.em_ruleset import (
    EMResult, level_office_visit, max_defensible_level,
)


@pytest.mark.parametrize("problems,data,risk,expected_est,expected_new", [
    ("straightforward", "straightforward", "straightforward", "99212", "99202"),
    ("low", "low", "straightforward", "99213", "99203"),
    ("moderate", "moderate", "low", "99214", "99204"),
    ("moderate", "low", "moderate", "99214", "99204"),
    ("high", "high", "moderate", "99215", "99205"),
    # 2-of-3: a single high element does NOT reach high
    ("high", "low", "low", "99213", "99203"),
    # 2-of-3: two moderates reach moderate even if third is straightforward
    ("moderate", "straightforward", "moderate", "99214", "99204"),
])
def test_mdm_grid_two_of_three(problems, data, risk, expected_est, expected_new):
    est = level_office_visit(new_patient=False, problems=problems, data=data, risk=risk)
    new = level_office_visit(new_patient=True, problems=problems, data=data, risk=risk)
    assert est.code == expected_est
    assert new.code == expected_new


def test_result_shows_its_work():
    r = level_office_visit(new_patient=False, problems="moderate", data="low", risk="moderate")
    assert isinstance(r, EMResult)
    assert r.mdm_level == "moderate"
    assert set(r.met_by) == {"problems", "risk"}
    assert r.elements == {"problems": "moderate", "data": "low", "risk": "moderate"}
    assert "MDM" in r.explanation


def test_documented_time_can_level_above_mdm():
    r = level_office_visit(new_patient=False, problems="low", data="low",
                           risk="straightforward", total_time_minutes=42)
    assert r.code == "99215"
    assert "time" in r.explanation.lower()


def test_undocumented_time_never_inflates():
    r = level_office_visit(new_patient=False, problems="low", data="low",
                           risk="straightforward", total_time_minutes=None)
    assert r.code == "99213"


def test_time_below_mdm_does_not_lower():
    r = level_office_visit(new_patient=False, problems="moderate", data="moderate",
                           risk="moderate", total_time_minutes=10)
    assert r.code == "99214"


def test_max_defensible_level_matches_grid():
    assert max_defensible_level(new_patient=False, problems="moderate", data="low",
                                risk="moderate") == "99214"
    assert max_defensible_level(new_patient=True, problems="straightforward",
                                data="straightforward", risk="straightforward") == "99202"
