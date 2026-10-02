"""Problem/order ownership with cached source evidence and full-engine controls."""
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import hashlib
import json

import pytest

from cosmaneura_billing import mdm_problems as problems
from cosmaneura_billing.agents.coding import CodingAgent
from cosmaneura_billing.agents.compliance_guardrail import ComplianceGuardrailAgent
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition, VisitBundle
from cosmaneura_billing.settings import Settings


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


FIXTURE = json.loads((Path(__file__).parent / "fixtures/diagnostic_order_care.json").read_text())


CASES = {case["index"]: case for case in FIXTURE["cases"]}


class CachedObjects(MockLLM):
    def __init__(self, case):
        self.objects = case["extractions"]
        self.calls = []

    def complete_json(self, **kwargs):
        name = "extract_clinical_facts" if "extract_clinical_facts" in kwargs["user"] else "propose_diagnoses"
        self.calls.append(name)
        return deepcopy(self.objects[0 if name == "extract_clinical_facts" else 1])


@pytest.fixture(scope="module")
def catalogue():
    return ICD10CCSR()


@pytest.mark.parametrize("number", [10, 11])
def test_unchanged_sources_keep_problem_holds_and_levels_with_separate_encounter_care(number, catalogue):
    case = deepcopy(CASES[number]); before = deepcopy(case)
    bundle = VisitBundle(**case["source"])
    raw = case["extractions"][0]["mdm_problem_events"]
    assert hashlib.sha256(bundle.signed_note.encode()).hexdigest() == case["signedNoteSha256"]
    qualified = problems.qualify_problems(raw, bundle)
    assert qualified.public["rule_version"] == "office-mdm-problems.2026-09-30.v4"
    assert not qualified.has_addressed and not qualified.established
    assert not qualified.addressed_evidence() and not qualified.supports_addressed([])
    llm = CachedObjects(case)
    result = BillingOrchestrator(llm=llm, icd10=catalogue, settings=Settings(
        preferences_enabled=False, azure_openai_enabled=False,
        azure_language_enabled=False, azure_language_shadow_enabled=False)).process(bundle)
    suggestions = result.provider_card.captured_codes + result.provider_card.declined_codes
    em = next(s for s in suggestions if s.kind == CodeKind.EM)
    # The committed prior output is a comparison baseline, not a code selector.
    assert em.code == case["baselineEmCode"]
    assert em.disposition == Disposition.CAPTURED
    assert em.necessity_supported and "em_pathway:total_time" in em.notes
    assert "individual problem linkage remains unresolved" in em.rationale
    assert all(e["status"] == "held" for e in em.clinical_review["problems"]["events"])
    assert em.clinical_review["risk"]["rule_version"] == "office-mdm-risk.2026-09-30.v4"
    assert sorted(s.code for s in result.provider_card.captured_codes if s.kind == CodeKind.ICD10) == case["baselineCapturedDiagnoses"]
    assert llm.calls == ["extract_clinical_facts", "propose_diagnoses"]
    assert asdict(bundle) == case["source"] and case == before


def single_problem_case():
    identity = "Assessment is abdominal pain with cause unconfirmed."
    action = "I ordered an ultrasound for prompt outpatient assessment."
    time = "Total physician time on the date of service: 26 minutes."
    bundle = VisitBundle("synthetic-single-problem-order", 30, "F", "office_established", "11",
                         signed_note=identity + " " + action + "\n\n" + time)
    raw = {"chief_complaint": "Abdominal pain evaluation", "mdm_problem_events": [dict(
        name="abdominal pain", source="signed_note", identity_quote=identity,
        addressed_quote=action, status_quote=identity)], "mdm_risk_events": [],
        "mdm_data_events": [], "total_time_minutes": 26, "total_time_quote": time}
    return {"source": asdict(bundle), "extractions": [raw, {"diagnoses": []}]}


@pytest.mark.parametrize("tamper", [None, "lost-identity", "wrong-role", "raw-owner"])
def test_order_care_needs_complete_retained_source_and_raw_identity(tamper, catalogue):
    case = single_problem_case(); bundle = VisitBundle(**case["source"])
    if tamper is None:
        result = BillingOrchestrator(llm=CachedObjects(case), icd10=catalogue, settings=Settings(
            preferences_enabled=False, azure_openai_enabled=False,
            azure_language_enabled=False, azure_language_shadow_enabled=False)).process(bundle)
        em = next(s for s in result.provider_card.captured_codes if s.kind == CodeKind.EM)
        assert em.necessity_supported and "em_pathway:total_time" in em.notes
        assert all(e["status"] == "held" for e in em.clinical_review["problems"]["events"])
        qualified = problems.qualify_problems(case["extractions"][0]["mdm_problem_events"], bundle)
        proof = qualified.addressed_evidence_groups()[0]
        assert len(proof) == 2 and all(e.rationale == "Current clinical work: mdm-problem-1" for e in proof)
        assert qualified.supports_addressed(em.evidence) and not qualified.supports_addressed(proof[:1])
        return
    facts, suggestions = CodingAgent(CachedObjects(case), catalogue).run(bundle)
    em = next(s for s in suggestions if s.kind == CodeKind.EM)
    proof = [e for e in em.evidence if e.rationale.startswith("Current clinical work:")]
    assert len(proof) == 2
    if tamper == "lost-identity":
        em.evidence.remove(proof[0])
    elif tamper == "wrong-role":
        em.evidence = [replace(e, rationale="MDM problems: mdm-problem-1") if e in proof else e for e in em.evidence]
    else:
        facts["mdm_problem_events"][0]["name"] = "hypothyroidism"
    result, _ = ComplianceGuardrailAgent().run(facts, [em], new_patient=False)
    assert result[0].disposition == Disposition.REVIEW
    assert not result[0].necessity_supported
