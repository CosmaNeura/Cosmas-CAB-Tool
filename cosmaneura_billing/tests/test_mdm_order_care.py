"""Direct problem/order ownership; full-engine and corpus replay stay in C6."""
import pytest

from cosmaneura_billing import mdm_problems as problems
from cosmaneura_billing.schemas import VisitBundle


def example(action, *, second=None, suffix=None, later=""):
    suffix = suffix if suffix is not None else (", plus a request for " + second if second else "")
    identity = "Assessment is abdominal pain with cause unconfirmed" + suffix + "."
    names = ["abdominal pain", second] if second else ["abdominal pain"]
    raw = [dict(name=name, source="signed_note", identity_quote=identity,
                addressed_quote=action, status_quote=identity) for name in names]
    bundle = VisitBundle("synthetic-order-care", 30, "F", "office_established", "11",
                         signed_note=identity + " " + action + (" " + later if later else ""))
    return problems.qualify_problems(raw, bundle)


@pytest.mark.parametrize("action", [
    "I ordered an ultrasound for prompt outpatient assessment.",
    "I ordered a blood count for inpatient evaluation.",
    "Orders placed today are an ultrasound and blood count for assessment.",
])
def test_current_order_purpose_for_one_unambiguous_problem_is_care_not_complexity(action):
    q = example(action)
    assert q.has_addressed and not q.established
    assert q.events[0].addressed
    assert q.supports_addressed(q.addressed_evidence())


@pytest.mark.parametrize("whole_action", [False, True])
def test_request_does_not_own_order_when_extraction_omits_the_assessed_problem(whole_action):
    identity = "Assessment is abdominal pain with cause unconfirmed, plus a request for nutrition counseling."
    action = "I ordered an ultrasound for prompt outpatient assessment."
    raw = [dict(name="nutrition counseling", source="signed_note", identity_quote=identity,
                addressed_quote=identity + " " + action if whole_action else action, status_quote=identity)]
    bundle = VisitBundle("synthetic-request-only", 30, "F", "office_established", "11",
                         signed_note=identity + " " + action)
    qualified = problems.qualify_problems(raw, bundle)
    assert not qualified.has_addressed and not qualified.addressed_evidence()


def test_explicit_completed_service_does_not_remain_only_a_request():
    identity = "Assessment is abdominal pain with cause unconfirmed, plus a request for nutrition counseling."
    action = "Nutrition counseling: I counseled about nutrition."
    raw = [dict(name="nutrition counseling", source="signed_note", identity_quote=identity,
                addressed_quote=action, status_quote=identity)]
    bundle = VisitBundle("synthetic-completed-counseling", 30, "F", "office_established", "11",
                         signed_note=identity + " " + action)
    qualified = problems.qualify_problems(raw, bundle)
    assert qualified.has_addressed and qualified.supports_addressed(qualified.addressed_evidence())
    assert not qualified.established


@pytest.mark.parametrize("action", [
    "I ordered an ultrasound for prompt outpatient assessment of hypothyroidism.",
    "I ordered an ultrasound for prompt outpatient assessment for another patient.",
    "I ordered an ultrasound for hypothyroidism.",
    "The nurse ordered an ultrasound for prompt outpatient assessment.",
    "Patient reports: I ordered an ultrasound for prompt outpatient assessment.",
    "I did not order an ultrasound for prompt outpatient assessment.",
    "I may order an ultrasound for prompt outpatient assessment next month.",
    "I will order an ultrasound for prompt outpatient assessment next month.",
    "I forwarded an outside ultrasound order for prompt outpatient assessment.",
])
def test_diagnostic_purpose_does_not_borrow_foreign_tentative_or_routing_work(action):
    q = example(action)
    assert not q.has_addressed and not q.addressed_evidence()


@pytest.mark.parametrize("second,suffix", [
    ("hypothyroidism", ", plus hypothyroidism"),
    ("hypothyroidism", ", plus a request for hypothyroidism"),
    ("nutrition counseling", ", plus nutrition counseling performed today"),
    ("nutrition counseling", ", plus a request for nutrition counseling because of hypothyroidism"),
])
def test_assessed_condition_or_unresolved_request_ownership_still_blocks_order(second, suffix):
    q = example("I ordered an ultrasound for prompt outpatient assessment.", second=second, suffix=suffix)
    assert not q.has_addressed


@pytest.mark.parametrize("requested_service", ["hypothyroidism assessment", "neurologic evaluation",
    "thyroid consultation", "hypothyroidism counseling", "preconception counseling"])
@pytest.mark.parametrize("extraction", ["both", "primary-only", "whole-action"])
def test_requested_services_remain_competing_owners_even_when_omitted_from_extraction(requested_service, extraction):
    identity = "Assessment is abdominal pain, plus a request for " + requested_service + "."
    action = "I ordered laboratory tests for prompt outpatient assessment."
    names = ["abdominal pain", requested_service] if extraction == "both" else ["abdominal pain"]
    raw = [dict(name=name, source="signed_note", identity_quote=identity,
                addressed_quote=identity + " " + action if extraction == "whole-action" else action,
                status_quote=identity) for name in names]
    bundle = VisitBundle("synthetic-ambiguous-order", 30, "F", "office_established", "11",
                         signed_note=identity + " " + action)
    q = problems.qualify_problems(raw, bundle)
    assert not q.has_addressed and not q.addressed_evidence()


def test_explicit_named_order_keeps_its_owner_despite_another_requested_service():
    q = example("I ordered an ultrasound for abdominal pain.", second="hypothyroidism assessment")
    assert q.events[0].addressed and not q.events[1].addressed
    assert q.supports_addressed(q.addressed_evidence()) and not q.established


def test_later_current_order_retraction_cannot_be_clipped():
    q = example("I ordered an ultrasound for prompt outpatient assessment.",
                later="Correction: that order was cancelled today.")
    assert not q.has_addressed and not q.addressed_evidence()
