"""Current-code enforcement survives the full pipeline and both review views."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cosmaneura_billing.agents import DenialRiskAgent
from cosmaneura_billing.billing_header import validate_generated
from cosmaneura_billing.billing_header_runtime import generated_header
from cosmaneura_billing.billing_tab import build_billing_tab_view
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle
from cosmaneura_billing.suggested_codes import build_suggested_codes_payload

QUOTE = "Cough and hypertension assessed; supportive care and existing treatment continued."


@pytest.fixture(scope="module")
def catalogue():
    return ICD10CCSR()


def candidate(code, label="Synthetic clinical finding", *, kind=CodeKind.ICD10):
    return CodeSuggestion(kind, code, label, disposition=Disposition.CAPTURED,
        evidence=[Evidence("signed_note", QUOTE)], necessity_supported=True,
        rationale="The documented finding was proposed for review.")


@pytest.mark.parametrize("code", ["R05", "E66.8", "A00", "X99.99"])
def test_invalid_or_category_code_is_held_despite_exact_evidence(catalogue, code):
    item = candidate(code)
    evidence = deepcopy(item.evidence)
    out, flags = DenialRiskAgent().run([item], icd10=catalogue)
    assert out == [item] and item.code == code and item.evidence == evidence
    assert item.disposition == Disposition.REVIEW and not item.necessity_supported
    assert item.denial_risk == "high"
    assert "icd10_current_fy_hold: invalid_or_nonbillable" in item.notes
    assert "Held for coding review" in item.rationale and flags


@pytest.mark.parametrize("catalogue", [None, ICD10CCSR(current_csv=""),
    SimpleNamespace(has_current_fy=True, is_active=lambda _code: None),
    SimpleNamespace(has_current_fy=True, is_active=lambda _code: 1)])
def test_missing_or_indeterminate_catalogue_never_affirms_capture(catalogue):
    item = candidate("I10")
    DenialRiskAgent().run([item], icd10=catalogue)
    assert item.disposition == Disposition.REVIEW and not item.necessity_supported
    assert "catalogue_unavailable" in item.notes[-1]


def test_valid_code_and_prior_rejection_are_not_promoted_or_rewritten(catalogue):
    valid, rejected = candidate("I10"), candidate("R05")
    rejected.disposition = Disposition.REJECTED
    rejected.rationale = "Explicit clinical contradiction."
    before = deepcopy(rejected)
    DenialRiskAgent().run([valid, rejected], icd10=catalogue)
    assert valid.disposition == Disposition.CAPTURED and valid.necessity_supported
    assert rejected == before


def test_held_diagnosis_cannot_support_coverage_or_em(catalogue):
    class Coverage:
        loaded = True

        def supports(self, *_args):
            pytest.fail("a held diagnosis reached the coverage check")

    dx, em, procedure = candidate("R05"), candidate("99213", kind=CodeKind.EM), candidate(
        "12345", kind=CodeKind.PROCEDURE)
    _, flags = DenialRiskAgent().run([dx, em, procedure], icd10=catalogue, coverage=Coverage())
    assert em.denial_risk == "high"
    assert any("no captured diagnosis" in flag for flag in flags)


def run_pipeline(items, catalogue):
    bundle = VisitBundle("SYN-INVALID-ICD", 40, "female", "office_established", "11",
                         signed_note=QUOTE)

    class Coder:
        def run(self, _bundle):
            return {"chief_complaint": "Synthetic follow-up", "problems": [
                {"name": "Cough", "acuity": "acute", "status": "stable", "quote": QUOTE}],
                "data_reviewed": [], "risk": {"level": "low", "quote": QUOTE}}, deepcopy(items)

    result = BillingOrchestrator(coding=Coder(), icd10=catalogue).process(bundle)
    stored = MemoryEncounterStore([bundle]).get(bundle.encounter_id)
    return bundle, stored, result


@pytest.mark.parametrize("code", ["R05", "E66.8"])
def test_full_pipeline_withholds_code_and_exposes_final_hold_everywhere(catalogue, code):
    bundle, stored, result = run_pipeline([candidate(code)], catalogue)
    assert result.provider_card.captured_codes == []
    assert result.biller_worksheet.diagnoses == []
    assert result.biller_worksheet.primary_diagnosis is None
    assert result.biller_worksheet.requires_provider_signoff
    held = result.provider_card.declined_codes[0]
    assert held.disposition == Disposition.REVIEW and held.has_evidence
    assert result.biller_worksheet.audit_trail[-1].startswith(f"HOLD    {code}")
    assert "final denial gate" in result.biller_worksheet.audit_trail[-1]
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-test")
    assert view["icd_candidates"][0]["status"] == "held"
    assert view["icd_candidates"][0]["copy_text"] is None and not view["copy_all"]
    payload = build_suggested_codes_payload(bundle, result, engine_version="synthetic-test")
    assert payload["codes"] == [] and payload["review_items"][0]["copy_text"] is None
    header = generated_header(stored, result, engine_version="synthetic-test")
    assert header["suggestions"] == []
    flag = next(flag for flag in header["flags"] if "Held for coding review" in flag["message"])
    assert flag["evidenceIds"] == [header["evidence"][0]["id"]]
    assert header["evidence"][0]["text"] == QUOTE
    validate_generated(header, stored)


def test_missing_catalogue_holds_through_full_pipeline():
    _, _, result = run_pipeline([candidate("I10")], ICD10CCSR(current_csv=""))
    assert result.biller_worksheet.diagnoses == []
    assert result.biller_worksheet.requires_provider_signoff
    assert "could not be verified" in result.provider_card.declined_codes[0].rationale


@pytest.mark.parametrize("reverse", [False, True])
def test_same_evidence_cannot_make_held_code_a_suggested_alternate(catalogue, reverse):
    items = [candidate("R05"), candidate("I10", "Essential hypertension")]
    if reverse:
        items.reverse()
    bundle, stored, result = run_pipeline(items, catalogue)
    # A stale primary reference must not override the final disposition.
    result.biller_worksheet.primary_diagnosis = result.provider_card.declined_codes[0]
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-test")
    suggested = next(row for row in view["icd_candidates"] if row["status"] == "suggested")
    held = next(row for row in view["icd_candidates"] if row["status"] == "held")
    assert suggested["code"] == "I10" and suggested["alternates"] == []
    assert held["code"] == "R05" and held["copy_text"] is None and not held["primary"]
    assert view["copy_all"] == "I10"
    header = generated_header(stored, result, engine_version="synthetic-test")
    assert [row["code"] for row in header["suggestions"]] == ["I10"]
    assert any("Held for coding review" in flag["message"] for flag in header["flags"])


@pytest.mark.parametrize("has_quote", [False, True])
def test_unresolved_concept_requires_review_without_creating_a_code(catalogue, has_quote):
    item = candidate("(unresolved)", "Fertility counseling with uncertain diagnosis")
    item.rationale = "No unique supported current diagnosis mapping; coding review required."
    if not has_quote:
        item.evidence = []
    bundle, stored, result = run_pipeline([item], catalogue)
    assert result.biller_worksheet.requires_provider_signoff
    assert result.provider_card.captured_codes == []
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-test")
    assert view["copy_all"] == ""
    header = generated_header(stored, result, engine_version="synthetic-test")
    assert header["suggestions"] == []
    if has_quote:
        flag = next(flag for flag in header["flags"] if "Fertility counseling" in flag["message"])
        assert "No unique supported" in flag["message"] and flag["evidenceIds"]
        assert header["evidence"][0]["text"] == QUOTE
    else:
        assert any(flag["id"] == "withheld_without_evidence" for flag in header["flags"])
        assert "(unresolved)" not in str(header)
    validate_generated(header, stored)
