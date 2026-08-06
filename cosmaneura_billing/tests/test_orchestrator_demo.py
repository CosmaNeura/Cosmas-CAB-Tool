"""End-to-end pipeline on the demo encounter (offline MockLLM).

This is the ported run_demo.py, with the printed claims turned into assertions:
under-coding corrected to 99214, the contradicted family-history code rejected,
the unaddressed obesity finding held, and every capture backed by evidence.
"""
import pytest

from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition, FeedbackKind, FeedbackTarget
from demo_visit import SAMPLE_VISIT


@pytest.fixture(scope="module")
def result():
    return BillingOrchestrator().process(SAMPLE_VISIT)


def test_em_undercoding_corrected(result):
    em = result.biller_worksheet.em_code
    assert em is not None
    assert em.code == "99214"
    assert em.disposition == Disposition.CAPTURED


def test_primary_diagnosis_is_rectal_hemorrhage(result):
    assert result.biller_worksheet.primary_diagnosis.code == "K62.5"
    assert result.biller_worksheet.diagnoses[0].code == "K62.5"


def test_contradicted_family_history_never_billed(result):
    captured = {s.code for s in result.provider_card.captured_codes}
    assert not any(c.startswith("Z80") for c in captured)
    declined = {s.code: s.disposition for s in result.provider_card.declined_codes}
    z80 = next((d for c, d in declined.items() if c.startswith("Z80")), None)
    assert z80 == Disposition.REJECTED


def test_unaddressed_obesity_held_not_billed(result):
    captured = {s.code for s in result.provider_card.captured_codes}
    assert not any(c.startswith("E66") for c in captured)
    held = [s for s in result.provider_card.declined_codes if s.code.startswith("E66")]
    assert held and held[0].disposition == Disposition.REVIEW


def test_every_captured_code_has_evidence(result):
    for s in result.provider_card.captured_codes:
        assert s.has_evidence, f"{s.code} captured without a documentation span"


def test_audit_trail_covers_every_decision(result):
    audit = "\n".join(result.biller_worksheet.audit_trail)
    for token in ("CAPTURE 99214", "CAPTURE K62.5", "REJECT", "HOLD"):
        assert token in audit


def test_worksheet_requires_signoff_when_codes_held(result):
    assert result.biller_worksheet.requires_provider_signoff is True


def test_coding_education_event_names_both_levels(result):
    edu = [e for e in result.feedback_events if e.kind == FeedbackKind.CODING_EDUCATION]
    assert edu, "under-coded encounter should produce a coding-education nudge"
    assert "99213" in edu[0].message and "99214" in edu[0].message


def test_every_person_facing_event_states_patient_benefit(result):
    for e in result.feedback_events:
        if e.target != FeedbackTarget.BILLER:
            assert e.patient_benefit.strip(), f"{e.kind} event lacks a patient benefit"


def test_denial_flags_low_specificity(result):
    flags = "\n".join(result.biller_worksheet.denial_flags)
    assert "low-specificity" in flags
