"""Clinical-meaning regressions; synthetic text only, no model/network calls."""
import csv
import json
from pathlib import Path

import pytest

from cosmaneura_billing.agents.coding import CodingAgent
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition, VisitBundle
from cosmaneura_billing.settings import Settings


@pytest.fixture(scope="module")
def kb():
    return ICD10CCSR()


@pytest.mark.parametrize("concept,code", [
    ("Hypertension", "I10"), ("Essential hypertension", "I10"),
    ("Portal hypertension", "K76.6"),
    ("Type 2 diabetes mellitus", "E11.9"),
    ("T1DM without complications", "E10.9"),
    ("Type 2 diabetes mellitus with hyperglycemia", "E11.65"),
    ("Type 2 diabetes mellitus with foot ulcer", "E11.621"),
    ("Chronic migraine without aura, without status migrainosus, not intractable", "G43.709"),
    ("Chronic migraine without aura, intractable, with status migrainosus", "G43.711"),
    ("Oligomenorrhea", "N91.5"), ("Secondary oligomenorrhea", "N91.4"),
    ("Preconception counseling", "Z31.69"),
    ("Encounter for procreative genetic counseling", "Z31.5"),
    ("Intrauterine contraceptive device in place", "Z97.5"),
    ("Encounter for reversal of previous sterilization / fertility restoration after tubal ligation", "Z31.0"),
    ("Acute cystitis without hematuria", "N30.00"),
    ("COPD exacerbation", "J44.1"), ("Postinfectious cough", "R05.8"),
    ("Hypothyroidism", "E03.9"), ("Obesity", "E66.9"),
    ("Sprain of unspecified ligament of left ankle, subsequent encounter", "S93.402D"),
])
def test_whole_clinical_meaning_maps_to_current_code(kb, concept, code):
    hit, reason = kb.resolve(concept, quote=concept)
    assert hit is not None, reason
    assert hit.code == code
    assert kb.is_active(hit.code) is True


@pytest.mark.parametrize("concept", [
    "New mysterious hypertension disease", "Possible diabetes",
    "Recent pregnancy loss with completion unconfirmed",
    "Diabetes, type unknown", "Right lateral ankle injury with unclear healing phase",
    "Type 2 diabetes with no documented complication, hyperglycemia above individualized goal",
    "Hepatitis without hepatic coma", "Vitamin deficiency with corneal xerosis",
    "Underlying condition due to diabetes mellitus without complications",
])
def test_no_partial_keyword_fallback(kb, concept):
    assert kb.resolve(concept)[0] is None
    assert kb.find_by_description(concept, limit=1) == []


@pytest.mark.parametrize("code,quote", [
    ("E10.9", "Type 2 diabetes mellitus is stable."),
    ("E11.621", "Type 2 diabetes without complications."),
    ("E11.9", "Type 2 diabetes mellitus with nephropathy."),
    ("K76.6", "Hypertension is controlled."),
    ("G43.711", "Chronic migraine without aura, not intractable, without status migrainosus."),
    ("G43.709", "Chronic migraine without aura; intractability and status are not established."),
    ("N91.3", "Oligomenorrhea; primary versus secondary is unknown."),
    ("Z31.5", "Preconception counseling."),
    ("T83.32XS", "IUD in place."),
    ("M84.371D", "Right lateral ankle sprain in routine healing."),
    ("M17.11", "Right knee osteoarthritis."),
    ("S93.402D", "Sprain of unspecified ligament of right ankle, subsequent encounter."),
    ("I10", "No hypertension."), ("I10", "Hypertension denied."),
    ("I10", "Patient doesn't have hypertension."),
    ("I10", "Family history of hypertension."),
    ("E10.9", "No type 1 diabetes; type 2 diabetes mellitus stable."),
    ("E10.9", "Mother has type 1 diabetes; patient has type 2 diabetes."),
    ("I10", "Portal hypertension is present."),
    ("I10", "Secondary hypertension due to renal disease."),
    ("E03.9", "Postprocedural hypothyroidism is treated."),
    ("I10", "No diabetes, hypertension or heart disease."),
    ("I10", "Hypertension: no."), ("I10", "Negative for hypertension."),
    ("I10", "No evidence of: hypertension."),
    ("I10", "Her husband has hypertension."),
    ("E11.9", "Her daughter has type 2 diabetes mellitus."),
    ("I10", "We will screen for hypertension."),
    ("I10", "The patient may have hypertension."),
    ("I10", "The patient is at risk for hypertension."),
    ("M17.11", "Primary osteoarthritis of knee and right shoulder pain."),
    ("E11.9", "Type 2 endoleak and diabetes mellitus are documented."),
    ("E11.9", "Type 2 diabetes mellitus complicated by neuropathy."),
    ("E11.9", "Type 2 diabetes mellitus and diabetic neuropathy."),
    ("I10", "Hypertension caused by chronic kidney disease."),
    ("E03.9", "Hypothyroidism due to medication."),
    ("I10", "Hypertension is unlikely."), ("I10", "Hypertension has resolved."),
    ("I10", "Hypertension is not present."),
    ("R50.9", "Without cough, fever or chills."),
    ("I10", "Hypertension: negative."), ("I10", "Hypertension not present."),
    ("I10", "Hypertension - resolved."), ("I10", "Hypertension resolved."),
    ("I10", "FHx: hypertension."), ("I10", "Hypertension versus white coat syndrome."),
    ("I10", "Hypertension not active."), ("I10", "H/o hypertension."),
    ("I10", "Hypertension status unknown."),
])
def test_quoted_words_do_not_prove_clinical_meaning(kb, code, quote):
    assert not kb.supports(code, quote)


def test_entire_catalogue_not_a_case_whitelist(kb):
    # These are outside the 12-note evaluation and the explicit aliases.
    for code in ("A00.0", "B35.1", "H10.31", "L40.0", "M54.2", "R10.31", "Z23"):
        desc = kb.get(code).description
        hit, reason = kb.resolve(desc, quote=desc)
        assert hit and hit.code == code, (desc, reason)


def test_ambiguity_order_independence_and_current_descriptor(tmp_path):
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("code,description,ccsr_category,ccsr_description\nA001,Old meaning,X,Category\n")
    current = tmp_path / "current.csv"
    entries = [("A001", "1", "short", "Current meaning"),
               ("A002", "1", "short", "Current meaning"),
               ("A003", "1", "short", "Unique meaning"),
               ("A00", "0", "short", "Category only")]
    for rows in (entries, list(reversed(entries))):
        with current.open("w") as f:
            csv.writer(f).writerows(rows)
        kb = ICD10CCSR(csv_path=legacy, current_csv=current)
        assert kb.get("A00.1").description == "Current meaning"
        assert kb.find_by_description("Current meaning", limit=1) == []
        assert kb.resolve("Unique meaning")[0].code == "A00.3"
        assert kb.resolve("Category only")[0] is None
    assert ICD10CCSR(csv_path=legacy, current_csv="").resolve("Old meaning")[0] is None


class RecordedLLM(MockLLM):
    def __init__(self, extractions):
        self.extractions = iter(extractions)

    def complete_json(self, **kwargs):
        return next(self.extractions)


@pytest.mark.parametrize("source", [
    "No hypertension. Stable today.",
    "Family history of hypertension. Stable today.",
    "Possible hypertension. Reassessment planned.",
    "No hx. of hypertension.",
    "Family history:\nhypertension.",
    "FHx:\nhypertension.",
    "Differential diagnosis:\nhypertension.",
])
def test_model_cannot_trim_assertion_context(kb, source):
    bundle = VisitBundle("synthetic", 40, "F", "office_established", "11", signed_note=source)
    agent = CodingAgent(RecordedLLM([{}, {"diagnoses": [
        {"concept": "Hypertension", "quote": "hypertension"}
    ]}]), kb)
    _, suggestions = agent.run(bundle)
    diagnosis = next(s for s in suggestions if s.kind == CodeKind.ICD10)
    assert diagnosis.code == "(unresolved)"
    assert diagnosis.evidence[0].quote == "hypertension"


def test_ambiguous_signed_note_does_not_fall_through_to_transcript(kb):
    bundle = VisitBundle("synthetic", 40, "F", "office_established", "11",
                         signed_note="No hypertension. Confirmed absence of hypertension.",
                         transcript="Patient has hypertension.")
    _, suggestions = CodingAgent(RecordedLLM([{}, {"diagnoses": [
        {"concept": "Hypertension", "quote": "hypertension"}
    ]}]), kb).run(bundle)
    dx = next(s for s in suggestions if s.kind == CodeKind.ICD10)
    assert dx.code == "(unresolved)" and dx.evidence == []
