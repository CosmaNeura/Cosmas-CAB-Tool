"""End-to-end pipeline on the demo encounter (offline MockLLM).

This is the ported run_demo.py, with the printed claims turned into assertions:
unsupported E/M complexity held, the contradicted family-history code rejected,
the unaddressed obesity finding held, and every capture backed by evidence.
"""
import pytest

from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition, FeedbackKind, FeedbackTarget
from demo_visit import SAMPLE_VISIT


@pytest.fixture(scope="module")
def result():
    return BillingOrchestrator().process(SAMPLE_VISIT)


def test_em_requires_documented_complexity_not_legacy_new_problem_flag(result):
    assert result.biller_worksheet.em_code is None
    em = next(s for s in result.provider_card.declined_codes if s.kind == CodeKind.EM)
    assert em.code == "99212" and em.disposition == Disposition.REVIEW
    assert em.clinical_review["risk"]["events"] == []


def test_primary_diagnosis_is_rectal_hemorrhage(result):
    assert result.biller_worksheet.primary_diagnosis.code == "K62.5"
    assert result.biller_worksheet.diagnoses[0].code == "K62.5"


def test_contradicted_family_history_never_billed(result):
    captured = {s.code for s in result.provider_card.captured_codes}
    assert not any(c.startswith("Z80") for c in captured)
    denied = next(s for s in result.provider_card.declined_codes if "Family history" in s.label)
    assert denied.disposition == Disposition.REJECTED
    assert denied.code == "(unresolved)"  # no code assigned to an unsupported concept


def test_unaddressed_obesity_held_not_billed(result):
    captured = {s.code for s in result.provider_card.captured_codes}
    assert not any(c.startswith("E66") for c in captured)
    held = [s for s in result.provider_card.declined_codes if s.label == "Obesity"]
    assert held and held[0].disposition == Disposition.REVIEW
    assert held[0].code == "(unresolved)"  # a weight alone cannot establish obesity


def test_every_captured_code_has_evidence(result):
    for s in result.provider_card.captured_codes:
        assert s.has_evidence, f"{s.code} captured without a documentation span"


def test_audit_trail_covers_every_decision(result):
    audit = "\n".join(result.biller_worksheet.audit_trail)
    for token in ("HOLD    99212", "CAPTURE K62.5", "REJECT", "HOLD"):
        assert token in audit


def test_worksheet_requires_signoff_when_codes_held(result):
    assert result.biller_worksheet.requires_provider_signoff is True


def test_no_coding_education_from_unsupported_risk_assumption(result):
    edu = [e for e in result.feedback_events if e.kind == FeedbackKind.CODING_EDUCATION]
    assert not edu


def test_every_person_facing_event_states_patient_benefit(result):
    for e in result.feedback_events:
        if e.target != FeedbackTarget.BILLER:
            assert e.patient_benefit.strip(), f"{e.kind} event lacks a patient benefit"


def test_denial_flags_low_specificity(result):
    flags = "\n".join(result.biller_worksheet.denial_flags)
    assert "low-specificity" in flags
