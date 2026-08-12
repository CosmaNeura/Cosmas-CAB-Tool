"""Coder feedback loop: store round-trip, metrics, tuning backlog, gold export."""
from cosmaneura_billing.coder_feedback import (
    Action, CodeDecision, EncounterFeedback, FeedbackStore, Reason,
    export_gold, summarize, tuning_signals,
)


def _sample_feedbacks():
    return [
        EncounterFeedback("ENC-1", "coder_A", "cpt2025", "99213", "99214", "99214",
            [CodeDecision("EM", "99214", Action.ACCEPT, "99214"),
             CodeDecision("ICD10", "K62.5", Action.ACCEPT, "K62.5"),
             CodeDecision("ICD10", None, Action.ADD, "Z86.010", Reason.DX_MISSED)],
            time_spent_seconds=95),
        EncounterFeedback("ENC-2", "coder_A", "cpt2025", "99214", "99214", "99213",
            [CodeDecision("EM", "99214", Action.MODIFY, "99213", Reason.EM_SUPPORTS_LOWER),
             CodeDecision("ICD10", "R51.9", Action.MODIFY, "R51.0", Reason.DX_LOW_SPECIFICITY)],
            time_spent_seconds=140),
        EncounterFeedback("ENC-3", "coder_B", "cpt2025", "99213", "99214", "99214",
            [CodeDecision("EM", "99214", Action.ACCEPT, "99214"),
             CodeDecision("ICD10", None, Action.ADD, "E66.9", Reason.DX_MISSED)],
            time_spent_seconds=80),
    ]


def test_store_round_trip(tmp_path):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    for fb in _sample_feedbacks():
        store.append(fb)
    loaded = store.load()
    assert len(loaded) == 3
    assert loaded[1].final_em == "99213"
    assert loaded[0].decisions[2].action == Action.ADD


def test_summarize_metrics():
    s = summarize(_sample_feedbacks())
    assert s["encounters"] == 3
    assert s["em_exact_agreement"] == round(2 / 3, 3)
    assert s["em_within_one"] == 1.0
    assert s["em_engine_over_coded"] == 1     # ENC-2: engine 99214 above coder-final 99213
    assert s["missed_dx_added_by_coder"] == 2
    assert s["median_seconds_per_encounter"] == 95


def test_tuning_signals_ranked_with_safety_flag():
    signals = tuning_signals(_sample_feedbacks())
    assert signals[0]["reason"] == Reason.DX_MISSED and signals[0]["count"] == 2
    safety = [s for s in signals if s["reason"] == Reason.EM_SUPPORTS_LOWER]
    assert safety and safety[0]["change_type"] == "rule_safety"


def test_gold_export_excludes_rejected():
    fbs = _sample_feedbacks()
    fbs[0].decisions.append(CodeDecision("ICD10", "Z80.0", Action.REJECT, None,
                                         Reason.DX_CONTRADICTED))
    gold = export_gold(fbs)
    assert gold[0]["gold_em"] == "99214"
    assert "Z80.0" not in gold[0]["gold_dx"]
    assert gold[0]["gold_dx"] == ["K62.5", "Z86.010"]
