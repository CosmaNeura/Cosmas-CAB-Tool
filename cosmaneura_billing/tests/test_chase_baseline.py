"""Tests for the Card Chase baseline capture surface (B5)."""
from __future__ import annotations

import pytest

from cosmaneura_billing.chase.baseline import (
    BaselineValidationError,
    ChaseBaseline,
    compare,
    compare_items,
    record_baseline,
)

FIXED_NOW = lambda: "2026-09-03T00:00:00+00:00"  # noqa: E731


def test_record_full_baseline():
    b = record_baseline(
        captured_by="admin-oid",
        cards_chased_per_month=400,
        touches_per_card=4.0,
        minutes_per_card=6.5,
        cards_outstanding=120,
        source="month_end_close",
        notes="from Grace, Aug close",
        now=FIXED_NOW,
    )
    assert isinstance(b, ChaseBaseline)
    d = b.to_dict()
    assert d["captured_at"] == "2026-09-03T00:00:00+00:00"
    assert d["captured_by"] == "admin-oid"
    assert d["cards_chased_per_month"] == 400
    assert d["minutes_per_card"] == 6.5
    assert d["source"] == "month_end_close"


def test_partial_baseline_is_allowed():
    b = record_baseline(captured_by="admin-oid", minutes_per_card=6.5, now=FIXED_NOW)
    assert b.minutes_per_card == 6.5
    assert b.cards_chased_per_month is None


def test_empty_baseline_rejected():
    with pytest.raises(BaselineValidationError):
        record_baseline(captured_by="admin-oid", now=FIXED_NOW)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cards_chased_per_month": -1},
        {"cards_chased_per_month": True},
        {"cards_chased_per_month": 1.5},
        {"minutes_per_card": -0.1},
        {"minutes_per_card": True},
        {"minutes_per_card": "6"},
        {"cards_outstanding": -5},
    ],
)
def test_malformed_metrics_rejected(kwargs):
    with pytest.raises(BaselineValidationError):
        record_baseline(captured_by="admin-oid", now=FIXED_NOW, **kwargs)


def test_missing_captured_by_rejected():
    with pytest.raises(BaselineValidationError):
        record_baseline(captured_by="  ", minutes_per_card=6.5, now=FIXED_NOW)


def test_explicit_captured_at_used():
    b = record_baseline(
        captured_by="admin-oid",
        minutes_per_card=6.5,
        captured_at="2026-08-31T23:59:59+00:00",
    )
    assert b.captured_at == "2026-08-31T23:59:59+00:00"


def test_compare_computes_deltas_when_both_present():
    b = record_baseline(
        captured_by="admin-oid",
        minutes_per_card=8.0,
        touches_per_card=5.0,
        cards_outstanding=120,
        cards_chased_per_month=400,
        now=FIXED_NOW,
    )
    measured = {
        "chases": 40,
        "outstanding": 90,
        "staff_minutes_per_card": 5.5,
        "median_touches_to_card": 4.0,
    }
    result = compare(b, measured)
    assert result["deltas"]["minutes_per_card_delta"] == -2.5  # improvement
    assert result["deltas"]["touches_per_card_delta"] == -1.0
    assert result["deltas"]["cards_outstanding_delta"] == -30
    assert result["cards_chased_per_month_baseline"] == 400
    assert result["measured_chases_in_window"] == 40


def test_compare_none_where_measured_missing():
    b = record_baseline(captured_by="admin-oid", minutes_per_card=8.0, now=FIXED_NOW)
    measured = {"chases": 0, "staff_minutes_per_card": None}
    result = compare(b, measured)
    assert result["deltas"]["minutes_per_card_delta"] is None
    assert result["deltas"]["touches_per_card_delta"] is None
    assert result["deltas"]["cards_outstanding_delta"] is None


def test_compare_items_on_empty_log():
    b = record_baseline(captured_by="admin-oid", minutes_per_card=8.0, now=FIXED_NOW)
    result = compare_items(b, [])
    assert result["measured"] == {"chases": 0}
    assert result["deltas"]["minutes_per_card_delta"] is None


def test_to_baseline_comparison_kwargs_bridges_metrics():
    from cosmaneura_billing.chase.metrics import baseline_comparison

    b = record_baseline(
        captured_by="admin-oid",
        minutes_per_card=8.0,
        cards_chased_per_month=400,
        now=FIXED_NOW,
    )
    out = baseline_comparison([], **b.to_baseline_comparison_kwargs())
    assert out["baseline_available"] is True
    assert out["claims_held_baseline"] == 400
