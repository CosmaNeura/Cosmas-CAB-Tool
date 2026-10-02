"""Evidence-span verification: a quote is only evidence if it exists in the chart."""
from cosmaneura_billing.evidence import verify_quote, verify_suggestion_evidence
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import (
    CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle,
)
from demo_visit import SAMPLE_VISIT

SOURCES = {
    "intake_text": SAMPLE_VISIT.intake_text,
    "transcript": SAMPLE_VISIT.transcript,
    "signed_note": SAMPLE_VISIT.signed_note,
}


def _bundle(**kw):
    defaults = dict(encounter_id="T-1", patient_age=40, patient_sex="female",
                    encounter_type="office_established", place_of_service="11")
    defaults.update(kw)
    return VisitBundle(**defaults)


# ---- verify_quote ---------------------------------------------------------- #
def test_verbatim_quote_verifies_with_source():
    ok, src = verify_quote("When I eat spicy food, I poop blood", SOURCES)
    assert ok and src == "intake_text"


def test_case_and_whitespace_insensitive():
    ok, _ = verify_quote("  when i eat SPICY food,   I poop blood ", SOURCES)
    assert ok


def test_ellipsis_quote_checks_each_part():
    ok, src = verify_quote(
        "When I eat spicy food, I poop blood ... I poop a little bit of blood", SOURCES)
    assert ok and src == "intake_text"


def test_hallucinated_quote_fails():
    ok, _ = verify_quote("Patient reports severe chest pain radiating to the left arm", SOURCES)
    assert not ok


def test_paraphrase_is_not_evidence():
    # Real text: "refer you for a colonoscopy to be safe" — a paraphrase with a
    # shared prefix must not pass.
    ok, _ = verify_quote("refer you for a colonoscopy to evaluate the bleeding", SOURCES)
    assert not ok


def test_empty_quote_fails():
    assert verify_quote("", SOURCES) == (False, None)
    assert verify_quote(" ... ", SOURCES)[0] is False


# ---- verify_suggestion_evidence -------------------------------------------- #
def test_unverifiable_evidence_is_removed_and_audited():
    bundle = _bundle(signed_note="Assessment: seasonal allergies. Plan: loratadine.")
    s = CodeSuggestion(kind=CodeKind.ICD10, code="J30.2", label="Seasonal allergic rhinitis",
                       evidence=[Evidence("signed_note", "patient reports wheezing and stridor", "x")])
    audit = verify_suggestion_evidence(bundle, [s])
    assert s.evidence == []
    assert any("EVIDENCE-REMOVED J30.2" in line for line in audit)
    assert any("quote not found" in n for n in s.notes)


def test_verified_evidence_is_kept():
    bundle = _bundle(signed_note="Assessment: seasonal allergies. Plan: loratadine.")
    s = CodeSuggestion(kind=CodeKind.ICD10, code="J30.2", label="Seasonal allergic rhinitis",
                       evidence=[Evidence("signed_note", "seasonal allergies", "dx")])
    audit = verify_suggestion_evidence(bundle, [s])
    assert len(s.evidence) == 1
    assert audit == []


def test_mislabeled_source_is_corrected():
    bundle = _bundle(intake_text="I have had a cough for two weeks.",
                     signed_note="Assessment: acute cough.")
    s = CodeSuggestion(kind=CodeKind.ICD10, code="R05.9", label="Cough",
                       evidence=[Evidence("signed_note", "a cough for two weeks", "symptom")])
    audit = verify_suggestion_evidence(bundle, [s])
    assert s.evidence[0].source == "intake_text"
    assert any("EVIDENCE-RELABELED" in line for line in audit)


# ---- end to end: hallucinated evidence cannot be billed -------------------- #
def test_stripped_code_cannot_be_captured():
    """A code whose only evidence fails verification must not reach the claim."""
    from cosmaneura_billing.llm import MockLLM

    class HallucinatingLLM(MockLLM):
        def _propose_diagnoses(self):
            dx = super()._propose_diagnoses()
            dx["diagnoses"].append({
                "concept": "Constipation, unspecified", "primary": False,
                "quote": "patient has been severely constipated for six months"})
            return dx

    result = BillingOrchestrator(llm=HallucinatingLLM()).process(SAMPLE_VISIT)
    audit = "\n".join(result.biller_worksheet.audit_trail)
    assert "HOLD    unresolved dx 'Constipation, unspecified'" in audit
    # The fabricated duplicate is stopped before a code is assigned; the real
    # evidence-backed constipation suggestion remains available.
    held = [s for s in result.provider_card.declined_codes if s.label == "Constipation, unspecified"]
    assert held and held[0].disposition == Disposition.REVIEW
    assert held[0].code == "(unresolved)" and not held[0].evidence
    assert len([s for s in result.provider_card.captured_codes if s.code == "K59.00"]) == 1


def test_demo_pipeline_evidence_all_verifies():
    """The stock demo's captured codes all carry verified, correctly-labeled evidence."""
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    for s in result.provider_card.captured_codes:
        assert s.has_evidence
        for ev in s.evidence:
            assert ev.source in ("intake_text", "transcript", "signed_note")
