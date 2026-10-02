"""Qualified clinical elements remain bound through the full billing pipeline."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from cosmaneura_billing.agents.coding import _facts_to_em
from cosmaneura_billing.agents.compliance_guardrail import ComplianceGuardrailAgent
from cosmaneura_billing.agents.consensus import merge_facts
from cosmaneura_billing.corpus.mdm_cases import qualified_case
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition
from cosmaneura_billing.settings import Settings


class FactsLLM(MockLLM):
    def __init__(self, facts):
        self.facts = facts

    def complete_json(self, **kwargs):
        return deepcopy(self.facts) if "extract_clinical_facts" in kwargs["user"] else {"diagnoses": []}


@pytest.fixture(scope="module")
def kb():
    return ICD10CCSR()


def pipeline(raw, source, kb):
    config = Settings(preferences_enabled=False, azure_openai_enabled=False,
        azure_language_enabled=False, azure_language_shadow_enabled=False)
    return BillingOrchestrator(llm=FactsLLM(raw), icd10=kb, settings=config).process(source)


def em(result):
    return next(s for s in result.provider_card.captured_codes + result.provider_card.declined_codes
                if s.kind == CodeKind.EM)


@pytest.mark.parametrize("level", [2, 3, 4, 5])
def test_real_source_positive_paths_survive_full_pipeline(level, kb):
    facts, _, source = qualified_case(level)
    raw = {k: v for k, v in facts.items() if not k.startswith("_")}
    result = pipeline(raw, source, kb)
    suggestion = em(result)
    assert suggestion.code == f"9921{level}" and suggestion.disposition == Disposition.CAPTURED
    assert all(e.quote in source.signed_note for e in suggestion.evidence)
    payload = json.dumps(asdict(result))
    assert "_qualified_problems" not in payload and "_qualified_risk" not in payload
    assert suggestion.clinical_review["problems"]["events"]


def test_forged_json_levels_objects_and_real_unrelated_quote_cannot_authorize_em(kb):
    _, evidence, source = qualified_case(4)
    raw = {"problems": [{"name": "new severe problem", "status": "new", "uncertain_prognosis": True}],
        "risk": {"level": "high", "quote": evidence[0].quote},
        "_qualified_problems": {"level": "high", "established": True},
        "_qualified_risk": {"level": "high", "established": True},
        "mdm_problem_events": [], "mdm_risk_events": [], "mdm_data_events": []}
    result = em(pipeline(raw, source, kb))
    assert result.code == "99212" and result.disposition == Disposition.REVIEW
    assert not result.evidence
    assert result.clinical_review["problems"]["events"] == []


@pytest.mark.parametrize("lost", ["problems", "data"])
def test_each_authorizing_element_needs_its_own_retained_evidence(lost):
    facts, evidence, _ = qualified_case(4)
    retained = [e for e in evidence if not e.rationale.startswith(f"MDM {lost}:")]
    proposed = CodeSuggestion(kind=CodeKind.EM, code="99215", label="Office visit", evidence=retained)
    out, audit = ComplianceGuardrailAgent().run(facts, [proposed], new_patient=False)
    assert out[0].code == "99212" and out[0].disposition == Disposition.REVIEW
    assert any("fewer than two qualified" in line for line in audit)


def test_quote_with_reassigned_rationale_cannot_supply_a_missing_element():
    facts, evidence, _ = qualified_case(4)
    evidence = [replace(e, rationale="MDM problems: invented") for e in evidence]
    proposed = CodeSuggestion(kind=CodeKind.EM, code="99214", label="Office visit", evidence=evidence)
    out, _ = ComplianceGuardrailAgent().run(facts, [proposed], new_patient=False)
    assert out[0].disposition == Disposition.REVIEW and out[0].code == "99212"


def test_removed_time_evidence_updates_displayed_pathway_and_retains_mdm():
    facts, evidence, _ = qualified_case(5)
    proposed = CodeSuggestion(kind=CodeKind.EM, code="99215", label="Office visit",
        evidence=[e for e in evidence if e.rationale.startswith("MDM")],
        notes=["em_pathway:total_time", "em_total_time_minutes:65", "em_mdm_code:99214"])
    out, _ = ComplianceGuardrailAgent().run(facts, [proposed], new_patient=False)
    assert out[0].code == "99214" and out[0].disposition == Disposition.CAPTURED
    assert "em_pathway:mdm_2of3" in out[0].notes
    assert not any(n.startswith("em_total_time_minutes:") for n in out[0].notes)
    assert "retained qualified evidence" in out[0].rationale


def test_raw_extraction_mutation_invalidates_qualification_and_secondary_cannot_replace_it():
    facts, evidence, _ = qualified_case(4)
    facts["mdm_problem_events"][0]["name"] = "unrelated condition"
    assert _facts_to_em(facts, new_patient=False).code == "99212"
    secondary, _, _ = qualified_case(5)
    merged, audit = merge_facts(facts, secondary, new_patient=False)
    assert _facts_to_em(merged, new_patient=False).code == "99212"
    assert any("non-namespaced" in line for line in audit)


def test_qualified_time_does_not_create_medical_necessity_without_addressed_problem(kb):
    facts, _, source = qualified_case(5)
    facts = {k: v for k, v in facts.items() if not k.startswith("_")}
    facts["mdm_problem_events"] = []
    result = em(pipeline(facts, source, kb))
    assert result.code == "99215" and result.disposition == Disposition.REVIEW
    assert not result.necessity_supported


def test_qualified_time_equal_to_unestablished_mdm_is_retained_and_captured(kb):
    _, _, source = qualified_case(2)
    problem = "Hypertension is new; I evaluated it and started therapy."
    time = "Total physician time on the date of service: 16 minutes."
    source = replace(source, signed_note=problem + " " + time)
    raw = {"mdm_problem_events": [dict(name="hypertension", source="signed_note",
        identity_quote=problem, addressed_quote=problem, status_quote=problem)],
        "mdm_risk_events": [], "mdm_data_events": [], "total_time_minutes": 16, "total_time_quote": time}
    result = em(pipeline(raw, source, kb))
    assert result.code == "99212" and result.disposition == Disposition.CAPTURED
    assert "em_pathway:total_time" in result.notes and result.evidence[0].quote == time
    assert result.clinical_review["problems"]["events"][0]["status"] == "held"


def test_equal_code_pathway_refreshes_when_mdm_evidence_is_removed(kb):
    from cosmaneura_billing.agents.coding import CodingAgent
    facts, _, source = qualified_case(2)
    time = "Total physician time on the date of service: 16 minutes."
    source = replace(source, signed_note=source.signed_note + "\n\n" + time)
    raw = {k: v for k, v in facts.items() if not k.startswith("_")}
    raw.update(total_time_minutes=16, total_time_quote=time)
    facts, suggestions = CodingAgent(FactsLLM(raw), kb).run(source)
    proposal = next(s for s in suggestions if s.kind == CodeKind.EM)
    assert "em_pathway:mdm_2of3" in proposal.notes
    proposal.evidence = [e for e in proposal.evidence if not e.rationale.startswith("MDM data:")]
    out, _ = ComplianceGuardrailAgent().run(facts, [proposal], new_patient=False)
    assert out[0].code == "99212" and out[0].disposition == Disposition.CAPTURED
    assert "em_pathway:total_time" in out[0].notes
    assert "MDM classification remains unestablished" in out[0].rationale


def test_held_complexity_can_retain_current_care_but_cannot_supply_mdm(kb):
    _, _, source = qualified_case(4)
    text = "Hypertension is new; I evaluated it and started therapy."
    source = replace(source, signed_note=text)
    raw = {"mdm_problem_events": [dict(name="hypertension", source="signed_note",
        identity_quote=text, addressed_quote=text, status_quote=text)], "mdm_risk_events": [], "mdm_data_events": []}
    result = em(pipeline(raw, source, kb))
    assert result.clinical_review["problems"]["events"][0]["status"] == "held"
    assert result.necessity_supported and result.disposition == Disposition.REVIEW
    assert result.evidence
    assert all(e.rationale.startswith("Current clinical work:") for e in result.evidence)
    assert not any(e.rationale.startswith("MDM problems:") for e in result.evidence)


def current_care_case(minutes=26):
    _, _, source = qualified_case(2)
    identity = "Hypertension is new."
    work = "I evaluated hypertension and discussed treatment."
    time = f"Total physician time on the date of service: {minutes} minutes."
    source = replace(source, signed_note=f"{identity} {work} {time}")
    raw = {"chief_complaint": "Hypertension review", "mdm_problem_events": [dict(
        name="hypertension", source="signed_note", identity_quote=identity,
        addressed_quote=work, status_quote=identity)], "mdm_risk_events": [],
        "mdm_data_events": [], "total_time_minutes": minutes, "total_time_quote": time}
    return raw, source


@pytest.mark.parametrize("tamper", ["remove-identity", "remove-action", "wrong-role", "wrong-source", "altered-raw"])
def test_time_necessity_requires_a_complete_retained_current_care_group(tamper, kb):
    from cosmaneura_billing.agents.coding import CodingAgent
    raw, source = current_care_case()
    facts, suggestions = CodingAgent(FactsLLM(raw), kb).run(source)
    proposal = next(s for s in suggestions if s.kind == CodeKind.EM)
    care = [e for e in proposal.evidence if e.rationale.startswith("Current clinical work:")]
    assert len(care) == 2
    before = deepcopy(proposal)
    out, _ = ComplianceGuardrailAgent().run(facts, [before], new_patient=False)
    assert out[0].disposition == Disposition.CAPTURED
    if tamper.startswith("remove-"):
        proposal.evidence.remove(care[0 if tamper == "remove-identity" else 1])
    elif tamper == "wrong-role":
        care[0].rationale = "MDM problems: fabricated"
    elif tamper == "wrong-source":
        care[0].source = "intake_text"
    else:
        facts["mdm_problem_events"][0]["name"] = "other problem"
    out, audit = ComplianceGuardrailAgent().run(facts, [proposal], new_patient=False)
    assert out[0].code == "99213" and out[0].disposition == Disposition.REVIEW
    assert not out[0].necessity_supported
    assert any("missing evidence or necessity" in line for line in audit)


def test_time_explanation_preserves_care_without_claiming_mdm(kb):
    raw, source = current_care_case()
    result = pipeline(raw, source, kb)
    suggestion = em(result)
    assert suggestion.disposition == Disposition.CAPTURED
    assert suggestion.label == "Office visit supported by documented total time (99213)"
    assert "MDM classification remains unestablished" in suggestion.rationale
    assert any("supported by documented total time" in line for line in result.biller_worksheet.audit_trail)
    assert "MDM ceiling" not in json.dumps(asdict(result))
    assert any(e.rationale.startswith("Current clinical work:") for e in suggestion.evidence)
    assert suggestion.clinical_review["problems"]["events"][0]["status"] == "held"
    assert "Current clinical work" in suggestion.clinical_review["problems"]["events"][0]["reason"]
