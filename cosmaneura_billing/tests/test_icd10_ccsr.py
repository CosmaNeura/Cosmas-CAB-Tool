"""ICD-10/CCSR knowledge base: bundled data loads and lookups resolve."""
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR, _format_icd10


def test_bundled_code_set_loads():
    kb = ICD10CCSR()
    assert kb.size > 100  # subset at minimum; full gz ships ~74k


def test_get_by_code_with_or_without_decimal():
    kb = ICD10CCSR()
    assert kb.get("K62.5") is not None
    assert kb.get("K625").code == "K62.5"


def test_find_by_description():
    kb = ICD10CCSR()
    hits = kb.find_by_description("Hemorrhage of anus and rectum", limit=1)
    assert hits and hits[0].code == "K62.5"
    assert hits[0].ccsr_category


def test_format_inserts_decimal():
    assert _format_icd10("K5900") == "K59.00"
    assert _format_icd10("I10") == "I10"
