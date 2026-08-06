"""E/M benchmark: real CMS allowed amounts, specialty mix, opportunity sizing."""
import pytest

from cosmaneura_billing.benchmarks.em_benchmark import EMBenchmark


@pytest.fixture(scope="module")
def bm():
    return EMBenchmark()


def test_specialties_load(bm):
    assert len(bm.specialties()) > 10
    assert "Internal Medicine" in bm.specialties()


def test_mix_is_a_distribution(bm):
    mix = bm.mix("Internal Medicine")
    assert abs(sum(mix.values()) - 1.0) < 0.01
    assert set(mix) >= {"99213", "99214"}


def test_allowed_amounts_are_ordered(bm):
    a13 = bm.allowed("99213", "Internal Medicine")
    a14 = bm.allowed("99214", "Internal Medicine")
    a15 = bm.allowed("99215", "Internal Medicine")
    assert a13 < a14 < a15


def test_low_coding_practice_shows_positive_gap(bm):
    res = bm.compare({"99212": 100, "99213": 700, "99214": 180, "99215": 20},
                     "Internal Medicine")
    assert res["per_visit_gap_vs_benchmark"] > 0
    assert res["direction"] == "below_peers_review_for_undercoding"
    assert res["over_coding_outlier"] is False


def test_high_coding_practice_screens_as_outlier(bm):
    res = bm.compare({"99214": 100, "99215": 900}, "Internal Medicine")
    assert res["over_coding_outlier"] is True
