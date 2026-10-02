"""The adaptive preference layer: reviewer corrections re-enter the engine —
holds and refinements only, never additions or upgrades, always audited."""
import pytest

from cosmaneura_billing.coder_feedback import (
    Action, CodeDecision, EncounterFeedback, FeedbackStore, Reason,
)
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.preferences import (
    PreferenceStore, Preferences, Refinement, Suppression, apply_preferences,
)
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence


def _fb(coder, decisions, enc="E1"):
    return EncounterFeedback(enc, coder, "test", "99213", "99214", "99214",
                             [CodeDecision(*d) for d in decisions])


def _reject(code, reason=Reason.DX_NOT_SUPPORTED):
    return ("ICD10", code, Action.REJECT, None, reason)


def _accept(code):
    return ("ICD10", code, Action.ACCEPT, code)


def _modify(code, to, reason=Reason.DX_LOW_SPECIFICITY):
    return ("ICD10", code, Action.MODIFY, to, reason)


@pytest.fixture()
def store(tmp_path):
    fs = FeedbackStore(tmp_path / "fb.jsonl")
    ps = PreferenceStore(tmp_path / "fb.jsonl", min_count=3, global_min_users=3)
    return fs, ps


# ---- derivation ------------------------------------------------------------- #
def test_repeated_rejects_become_user_suppression(store):
    fs, ps = store
    for i in range(3):
        fs.append(_fb("coder_A", [_reject("K59.00")], enc=f"E{i}"))
    prefs = ps.for_user("coder_A")
    assert "K5900" in prefs.suppress
    assert prefs.suppress["K5900"].scope == "you"
    # a different user is unaffected, and there aren't 3 distinct users yet
    assert ps.for_user("coder_B").empty


def test_below_threshold_or_outvoted_by_accepts_no_suppression(store):
    fs, ps = store
    fs.append(_fb("coder_A", [_reject("K59.00")] ))
    fs.append(_fb("coder_A", [_reject("K59.00")], enc="E2"))
    assert ps.for_user("coder_A").empty          # only 2 rejects
    for i in range(4):
        fs.append(_fb("coder_A", [_accept("K59.00")], enc=f"A{i}"))
    fs.append(_fb("coder_A", [_reject("K59.00")], enc="E3"))
    assert "K5900" not in ps.for_user("coder_A").suppress  # 3 rejects < 4 accepts


def test_practice_wide_needs_distinct_users(store):
    fs, ps = store
    for i, coder in enumerate(["a", "b", "c"]):
        fs.append(_fb(coder, [_reject("Z80.0", Reason.DX_CONTRADICTED)], enc=f"E{i}"))
    prefs = ps.for_user("someone_new")
    assert prefs.suppress["Z800"].scope == "practice"


def test_same_family_modifies_become_refinement(store):
    fs, ps = store
    for i in range(3):
        fs.append(_fb("coder_A", [_modify("R51.9", "R51.0")], enc=f"E{i}"))
    prefs = ps.for_user("coder_A")
    assert prefs.refine["R519"].to_code == "R51.0"


def test_cross_family_modify_never_refines(store):
    fs, ps = store
    for i in range(5):
        fs.append(_fb("coder_A", [_modify("K59.00", "R19.7")], enc=f"E{i}"))
    assert ps.for_user("coder_A").empty


def test_em_disagreement_becomes_note_only(store):
    fs, ps = store
    for i in range(3):
        fs.append(_fb("coder_A", [("EM", "99214", Action.MODIFY, "99213",
                                   Reason.EM_SUPPORTS_LOWER)], enc=f"E{i}"))
    prefs = ps.for_user("coder_A")
    assert prefs.em_notes and not prefs.suppress and not prefs.refine


def test_disabled_by_env(store, monkeypatch):
    fs, ps = store
    for i in range(3):
        fs.append(_fb("coder_A", [_reject("K59.00")], enc=f"E{i}"))
    monkeypatch.setenv("COSMANEURA_PREFS", "0")
    assert ps.for_user("coder_A").empty


# ---- application ------------------------------------------------------------ #
def _sugg(code, label, kind=CodeKind.ICD10):
    return CodeSuggestion(kind=kind, code=code, label=label,
                          evidence=[Evidence("intake_text", "q", "r")])


def test_suppression_holds_never_drops():
    prefs = Preferences(suppress={"K5900": Suppression("K59.00", 3, Reason.DX_NOT_SUPPORTED, "you")})
    s = _sugg("K59.00", "Constipation, unspecified")
    audit = apply_preferences([s], prefs)
    assert any(n.startswith("preference_hold") for n in s.notes)
    assert any("HOLD" in a and "declined K59.00 3x" in a for a in audit)


def test_refinement_is_a_hold_and_keeps_original_code_and_evidence():
    kb = ICD10CCSR()
    prefs = Preferences(refine={"R197": Refinement("R19.7", "R19.4", 3, "you")})
    s = _sugg("R19.7", "Diarrhea, unspecified")
    audit = apply_preferences([s], prefs, icd10=kb)
    assert s.code == "R19.7"
    assert s.evidence            # original evidence preserved
    assert any(n.startswith("preference_hold") for n in s.notes)
    assert any("HOLD" in a for a in audit)


def test_learned_same_family_cannot_add_a_diabetes_complication():
    prefs = Preferences(refine={"E119": Refinement("E11.9", "E11.621", 3, "you")})
    s = _sugg("E11.9", "Type 2 diabetes mellitus without complications")
    apply_preferences([s], prefs, icd10=ICD10CCSR())
    assert s.code == "E11.9"
    assert any(n.startswith("preference_hold") for n in s.notes)


def test_refinement_to_inactive_code_is_skipped():
    kb = ICD10CCSR()
    prefs = Preferences(refine={"R197": Refinement("R19.7", "R19.99", 3, "you")})
    s = _sugg("R19.7", "Diarrhea, unspecified")
    apply_preferences([s], prefs, icd10=kb)
    assert s.code == "R19.7"     # bogus target rejected by the FY check


def test_em_codes_can_only_be_annotated():
    prefs = Preferences(
        suppress={"99214": Suppression("99214", 5, Reason.DX_NOT_SUPPORTED, "you")},
        refine={"99214": Refinement("99214", "99215", 5, "you")},
        em_notes=["you have re-leveled E/M lower 3x"])
    em = _sugg("99214", "Office visit", kind=CodeKind.EM)
    apply_preferences([em], prefs)
    assert em.code == "99214"                          # never remapped
    assert not any(n.startswith("preference_hold") for n in em.notes)
    assert any(n.startswith("reviewer history") for n in em.notes)


# ---- end to end -------------------------------------------------------------- #
def test_pipeline_holds_learned_suppression_for_that_user_only(tmp_path):
    fs = FeedbackStore(tmp_path / "fb.jsonl")
    for i in range(3):
        fs.append(_fb("dr_smith", [_reject("K59.00")], enc=f"E{i}"))
    orch = BillingOrchestrator(preferences=PreferenceStore(tmp_path / "fb.jsonl",
                                                           min_count=3, global_min_users=3))
    # dr_smith: K59.00 held, not captured
    r = orch.process(SAMPLE_VISIT, user_id="dr_smith")
    captured = {s.code for s in r.provider_card.captured_codes}
    assert "K59.00" not in captured
    held = [s for s in r.provider_card.declined_codes if s.code == "K59.00"]
    assert held and held[0].disposition == Disposition.REVIEW
    assert any("declined K59.00 3x" in line for line in r.biller_worksheet.audit_trail)
    # everyone else: unchanged
    r2 = orch.process(SAMPLE_VISIT, user_id="dr_jones")
    assert "K59.00" in {s.code for s in r2.provider_card.captured_codes}


def test_new_feedback_takes_effect_without_restart(tmp_path):
    path = tmp_path / "fb.jsonl"
    fs = FeedbackStore(path)
    orch = BillingOrchestrator(preferences=PreferenceStore(path, min_count=3,
                                                           global_min_users=3))
    r = orch.process(SAMPLE_VISIT, user_id="dr_smith")
    assert "K59.00" in {s.code for s in r.provider_card.captured_codes}
    for i in range(3):
        fs.append(_fb("dr_smith", [_reject("K59.00")], enc=f"E{i}"))
    r2 = orch.process(SAMPLE_VISIT, user_id="dr_smith")   # mtime cache refreshes
    assert "K59.00" not in {s.code for s in r2.provider_card.captured_codes}
