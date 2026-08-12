"""Documentation-integrity agent: contradictions are rejected, unaddressed
findings are held with a gap, evidence-less codes need review."""
from cosmaneura_billing.agents import DocumentationIntegrityAgent
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence


def test_contradicted_family_history_is_rejected():
    facts = {"family_history_colon_cancer": False,
             "family_history_quote": "I do not have a family history of colon cancer"}
    s = CodeSuggestion(kind=CodeKind.ICD10, code="Z80.0",
                       label="Family history of malignant neoplasm of digestive organs")
    result, _ = DocumentationIntegrityAgent().run(facts, [s])
    assert result[0].disposition == Disposition.REJECTED
    assert "denied" in result[0].rationale


def test_documented_but_unaddressed_finding_is_held_with_gap():
    facts = {"vitals": {"bmi": 37.6, "documented": True, "addressed_in_plan": False}}
    s = CodeSuggestion(kind=CodeKind.ICD10, code="E66.8", label="Other obesity",
                       evidence=[Evidence("signed_note", "BMI 37.6", "vitals")])
    result, gaps = DocumentationIntegrityAgent().run(facts, [s])
    assert result[0].disposition == Disposition.REVIEW
    assert "not_addressed_in_plan" in result[0].notes
    assert gaps and "BMI" in gaps[0]


def test_obesity_addressed_in_plan_is_not_held():
    facts = {"vitals": {"bmi": 37.6, "documented": True, "addressed_in_plan": True}}
    s = CodeSuggestion(kind=CodeKind.ICD10, code="E66.8", label="Other obesity",
                       evidence=[Evidence("signed_note", "obesity counseling provided", "plan")])
    result, gaps = DocumentationIntegrityAgent().run(facts, [s])
    assert result[0].disposition != Disposition.REJECTED
    assert "not_addressed_in_plan" not in result[0].notes
    assert not gaps


def test_no_evidence_gets_review_note():
    s = CodeSuggestion(kind=CodeKind.ICD10, code="K59.00", label="Constipation, unspecified")
    result, _ = DocumentationIntegrityAgent().run({}, [s])
    assert result[0].disposition == Disposition.REVIEW
    assert any("no documentation span" in n for n in result[0].notes)
