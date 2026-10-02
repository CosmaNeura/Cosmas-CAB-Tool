"""Bounded common wording, preserving every clinically defining attribute."""
import pytest

from cosmaneura_billing.knowledge.diagnosis_terms import meaning_key
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import VisitBundle
from cosmaneura_billing.settings import Settings


@pytest.fixture(scope="module")
def kb():
    return ICD10CCSR()


@pytest.mark.parametrize("concept,quote,expected", [
    ("Essential hypertension, stable", "Essential hypertension is stable by reviewed home readings.", "I10"),
    ("Hypertension, well controlled", "Hypertension is well controlled; continue treatment.", "I10"),
    ("Clinically stable hypertension", "Hypertension is clinically stable.", "I10"),
    ("Type 2 diabetes mellitus, clinically stable", "Type 2 diabetes is clinically stable by symptoms.", "E11.9"),
    ("Type 1 diabetes mellitus, stable", "Type 1 diabetes is stable.", "E10.9"),
    ("Well controlled type 2 diabetes mellitus", "Type 2 diabetes mellitus is well controlled.", "E11.9"),
    ("Newly recognized type 2 diabetes mellitus", "Assessment is newly recognized type 2 diabetes.", "E11.9"),
    ("Newly diagnosed type 1 diabetes mellitus", "Newly diagnosed type 1 diabetes mellitus.", "E10.9"),
    ("Intrauterine device in place", "She elects to keep her IUD in place.", "Z97.5"),
])
def test_recognized_state_wording_traverses_pipeline(kb, concept, quote, expected):
    class Recorded(MockLLM):
        def complete_json(self, *, system, user, schema=None):
            return {"diagnoses": [{"concept": concept, "quote": quote}]} if "propose_diagnoses" in user else {}

    bundle = VisitBundle("synthetic-state", 40, "F", "office_established", "11", signed_note=quote)
    result = BillingOrchestrator(llm=Recorded(), icd10=kb,
                                 settings=Settings(preferences_enabled=False)).process(bundle)
    assert [s.code for s in result.biller_worksheet.diagnoses] == [expected]
    assert result.biller_worksheet.diagnoses[0].evidence[0].quote == quote


@pytest.mark.parametrize("concept", [
    "Type 2 diabetes mellitus, unstable", "Type 2 diabetes mellitus, uncontrolled",
    "Type 2 diabetes mellitus, stable with neuropathy",
    "Type 2 diabetes mellitus, clinically stable, with foot ulcer",
    "Newly recognized type 2 diabetes mellitus with unknown complication",
    "Hypertension, stable due to renal disease", "Possible stable hypertension",
    "History of clinically stable hypertension", "Newly diagnosed portal hypertension",
    "Stable angina", "Unstable angina", "Stable burst fracture",
])
def test_state_is_not_a_global_stop_word(kb, concept):
    # Some may have their own valid code; they must never become generic I10 or
    # typed diabetes without complications through the routine-state rules.
    hit, _ = kb.resolve(concept)
    assert hit is None or hit.code not in {"I10", "E10.9", "E11.9"}
    assert meaning_key(concept) != meaning_key("hypertension")


@pytest.mark.parametrize("concept,quote", [
    ("Essential hypertension, stable", "No hypertension. Stable examination."),
    ("Hypertension, stable", "Her father has stable hypertension."),
    ("Type 2 diabetes mellitus, clinically stable", "Type 1 diabetes is stable."),
    ("Newly recognized type 2 diabetes mellitus", "Type 2 diabetes mellitus with nephropathy."),
    ("Type 2 diabetes mellitus, stable", "Type 2 diabetes mellitus complicated by neuropathy."),
    ("Type 2 diabetes mellitus, stable", "Type 2 diabetes status unknown."),
    ("Intrauterine device in place", "Its continued presence is reported."),
])
def test_state_alias_does_not_supply_missing_evidence(kb, concept, quote):
    assert kb.resolve(concept, quote=quote)[0] is None
