"""Feedback agent: nudges must state a patient benefit or be dropped, and the
under-coding education fires only when captured E/M exceeds submitted."""
from cosmaneura_billing.agents import FeedbackAgent
from cosmaneura_billing.schemas import (
    CodeKind, CodeSuggestion, Disposition, Evidence, FeedbackKind,
)


def _captured_em(code):
    return CodeSuggestion(kind=CodeKind.EM, code=code, label=code,
                          disposition=Disposition.CAPTURED,
                          evidence=[Evidence("signed_note", "x", "y")])


def test_undercoding_education_emitted():
    events = FeedbackAgent().run({}, [], [_captured_em("99214")], submitted_em_code="99213")
    edu = [e for e in events if e.kind == FeedbackKind.CODING_EDUCATION]
    assert len(edu) == 1


def test_no_education_when_submitted_matches():
    events = FeedbackAgent().run({}, [], [_captured_em("99214")], submitted_em_code="99214")
    assert not [e for e in events if e.kind == FeedbackKind.CODING_EDUCATION]


def test_no_education_when_submitted_higher():
    """Submitted above supported is a guardrail/downgrade concern, not a nudge to keep it."""
    events = FeedbackAgent().run({}, [], [_captured_em("99213")], submitted_em_code="99214")
    assert not [e for e in events if e.kind == FeedbackKind.CODING_EDUCATION]


def test_doc_gaps_become_provider_events_with_benefit():
    events = FeedbackAgent().run({}, ["BMI documented but not addressed"], [],
                                 submitted_em_code=None)
    assert events and events[0].kind == FeedbackKind.DOC_GAP
    assert events[0].patient_benefit.strip()


def test_event_without_patient_benefit_is_dropped():
    agent = FeedbackAgent()
    from cosmaneura_billing.schemas import FeedbackEvent, FeedbackTarget
    e = FeedbackEvent(kind=FeedbackKind.CARE_QUALITY, target=FeedbackTarget.PROVIDER,
                      message="do more things", patient_benefit="   ")
    assert agent._assert_patient_benefit(e) is False


def test_raw_referral_driver_cannot_generate_unverified_care_directive():
    facts = {"risk": {"drivers": ["refer for colonoscopy"]}}
    events = FeedbackAgent().run(facts, [], [], submitted_em_code=None)
    assert not any(e.kind == FeedbackKind.CARE_QUALITY for e in events)
