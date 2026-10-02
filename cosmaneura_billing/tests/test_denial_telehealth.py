"""Denial-risk additions: telehealth POS consistency and stale-code detection."""
from cosmaneura_billing.agents import DenialRiskAgent
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.knowledge.pos import PlaceOfService
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence


def _captured(kind, code, label):
    return CodeSuggestion(kind=kind, code=code, label=label,
                          disposition=Disposition.CAPTURED,
                          evidence=[Evidence("signed_note", "x", "y")])


def test_telehealth_pos_flags_modifier_95():
    suggestions = [_captured(CodeKind.EM, "99214", "Office visit"),
                   _captured(CodeKind.ICD10, "K62.5", "Hemorrhage of anus and rectum")]
    _, flags = DenialRiskAgent().run(suggestions, pos=PlaceOfService(),
                                     place_of_service="10",
                                     encounter_type="telehealth_established")
    tele = [f for f in flags if "modifier 95" in f]
    assert tele and "10" in tele[0]


def test_telehealth_type_with_office_pos_flags_mismatch():
    suggestions = [_captured(CodeKind.EM, "99214", "Office visit"),
                   _captured(CodeKind.ICD10, "K62.5", "Hemorrhage")]
    _, flags = DenialRiskAgent().run(suggestions, pos=PlaceOfService(),
                                     place_of_service="11",
                                     encounter_type="telehealth_established")
    assert any("typed telehealth but place of service is 11" in f for f in flags)


def test_office_visit_pos_11_no_telehealth_flag():
    suggestions = [_captured(CodeKind.EM, "99214", "Office visit"),
                   _captured(CodeKind.ICD10, "K62.5", "Hemorrhage")]
    _, flags = DenialRiskAgent().run(suggestions, pos=PlaceOfService(),
                                     place_of_service="11",
                                     encounter_type="office_established")
    assert not any("elehealth" in f for f in flags)


def test_stale_icd10_code_flagged_high_risk():
    kb = ICD10CCSR()
    suggestions = [_captured(CodeKind.ICD10, "X99.99", "Made-up retired code")]
    out, flags = DenialRiskAgent().run(suggestions, icd10=kb)
    assert out[0].disposition == Disposition.REVIEW
    assert out[0].necessity_supported is False
    assert out[0].denial_risk == "high"
    assert any("Not a billable code in the current" in f for f in flags)


def test_active_icd10_code_not_flagged():
    kb = ICD10CCSR()
    suggestions = [_captured(CodeKind.ICD10, "K62.5", "Hemorrhage of anus and rectum")]
    out, flags = DenialRiskAgent().run(suggestions, icd10=kb)
    assert out[0].denial_risk == "low"
    assert not any("not a billable" in f for f in flags)
