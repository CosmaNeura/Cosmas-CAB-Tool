"""Data levels require distinct completed work, not extracted category labels."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from cosmaneura_billing.agents.coding import CodingAgent, _facts_to_em
from cosmaneura_billing.agents.compliance_guardrail import ComplianceGuardrailAgent
from cosmaneura_billing.agents.consensus import merge_facts
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.mdm_data import qualify_mdm_data
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, VisitBundle
from cosmaneura_billing.settings import Settings


def bundle(note, **changes):
    return VisitBundle("SYN-MDM-DATA", 35, "F", "office_established", "11",
                       signed_note=note, **changes)


def event(subject, action, quote, qualification="", **changes):
    return {"subject": subject, "action": action, "source": "signed_note",
            "identity_quote": quote, "action_quote": quote,
            "qualification_quote": qualification, **changes}


def run(*events, note=None):
    if note is None:
        note = "\n".join(dict.fromkeys(q for e in events for k, q in e.items()
                                      if k.endswith("_quote") and q))
    return qualify_mdm_data(list(events), bundle(note)).public


@pytest.mark.parametrize("tests,units,level", [
    (["CBC"], 1, "straightforward"),
    (["CBC", "TSH"], 2, "low"),
    (["CBC", "TSH", "A1c"], 3, "moderate"),
    (["FSH", "LH", "estradiol", "prolactin"], 4, "moderate"),
    (["quantitative serum hCG", "transvaginal ultrasound"], 2, "low"),
])
def test_actual_unique_tests_in_one_model_category_count_separately(tests, units, level):
    result = run(*(event(t, "test_order", f"I ordered {t} today.") for t in tests))
    assert (result["category_one_units"], result["level"]) == (units, level)
    assert all(e["status"] == "counted" for e in result["events"])


@pytest.mark.parametrize("tests,units", [
    (["CMP", "BMP", "creatinine", "ALT"], 1),
    (["CBC", "hemoglobin", "platelet count"], 1),
    (["CBC", "CBC with differential"], 1),
    (["lipid panel", "total cholesterol", "triglycerides"], 1),
    (["urinalysis", "urine culture"], 2),
    (["TSH", "TSH receptor antibody"], 2),
    (["right knee X-ray", "left knee X-ray"], 2),
])
def test_overlap_variant_and_laterality_decisions(tests, units):
    raw = [event(t, "test_order", f"I ordered {t} today.") for t in tests]
    result = run(*raw)
    assert result["category_one_units"] == units
    assert run(*reversed(raw))["category_one_units"] == units


def test_order_review_and_repeats_do_not_create_more_units():
    events = [event("A1c", "test_order", "I ordered A1c today."),
              event("A1c", "test_result_review", "I reviewed A1c today."),
              event("A1c", "test_order", "I ordered a repeat A1c today.")]
    result = run(*events)
    assert result["category_one_units"] == 1
    assert sum(e["status"] == "counted" for e in result["events"]) == 1


@pytest.mark.parametrize("quote", [
    "The patient reports I ordered CBC today.",
    "I did not order CBC today.",
    "I never reviewed CBC today.",
    "I will order CBC tomorrow.",
    "I may order CBC if symptoms persist.",
    "Last week I ordered CBC.",
    "Example: I ordered CBC today.",
    "The nurse ordered CBC today.",
    "A CBC is attached and available.",
    "I ordered CBC today?",
])
def test_unperformed_or_nonclinician_work_is_held(quote):
    assert run(event("CBC", "test_order", quote))["category_one_units"] == 0


def test_clipped_quote_cannot_remove_negation_or_following_correction():
    e = event("CBC", "test_order", "order CBC today")
    result = run(e, note="I did not order CBC today.")
    assert result["category_one_units"] == 0
    assert result["events"][0]["evidence"][0]["quote"] == "I did not order CBC today."
    e = event("CBC", "test_order", "I ordered CBC today.")
    assert run(e, note=e["action_quote"] + " Correction: this order was canceled.")["category_one_units"] == 0


def test_quote_forgery_ambiguous_source_and_wrong_identity_are_held():
    good = event("CBC", "test_order", "I ordered CBC today.")
    for bad, note in [({**good, "action_quote": "I ordered CBC now."}, good["action_quote"]),
                      (good, good["action_quote"] * 2),
                      ({**good, "subject": "TSH"}, good["action_quote"]),
                      ({**good, "source": "intake_text"}, good["action_quote"])]:
        assert run(bad, note=note)["category_one_units"] == 0


def test_transcript_requires_clinician_speaker():
    for prefix, expected in [("Patient: ", 0), ("Physician: ", 1), ("", 0)]:
        quote = prefix + "I ordered CBC today."
        raw = [event("CBC", "test_order", quote, source="transcript")]
        q = qualify_mdm_data(raw, bundle("", transcript=quote))
        assert q.public["category_one_units"] == expected


def test_considered_declined_test_requires_shared_decision():
    yes = "I considered chest X-ray; after shared decision-making the patient declined due to cost."
    assert run(event("chest X-ray", "test_considered", yes))["category_one_units"] == 1
    no = "I considered chest X-ray if the cough continues."
    assert run(event("chest X-ray", "test_considered", no))["category_one_units"] == 0


def test_external_records_dedup_by_source_not_name_or_document():
    first = "I reviewed the outside endocrinology note from Dr. Jones at North Clinic today."
    second = "I reviewed the outside endocrinology note from Dr. Smith at North Clinic today."
    third = "I reviewed the outside cardiology note from Dr. Green at North Clinic today."
    result = run(*(event("external note", "external_note_review", q) for q in (first, second, third)))
    assert result["category_one_units"] == 2
    assert [e["status"] for e in result["events"]] == ["counted", "duplicate", "counted"]
    name_only = "I reviewed Dr. Jones's note today."
    assert run(event("Dr Jones", "external_note_review", name_only))["category_one_units"] == 0


def test_embedded_tests_cannot_multiply_external_note_credit():
    text = "I reviewed CBC in the outside endocrinology note from North Clinic today."
    result = run(event("North Clinic", "external_note_review", text),
                 event("CBC", "test_result_review", text))
    assert result["category_one_units"] == 1
    assert result["events"][1]["status"] == "held"


def test_historian_counts_once_and_combines_with_test_units():
    historian = "History obtained from mother because the patient is unable to provide reliable history."
    father = "History obtained from father because the patient is unable to provide reliable history."
    history_events = [event("mother", "independent_historian", historian, historian),
                      event("father", "independent_historian", father, father)]
    low = run(*history_events)
    assert (low["category_one_units"], low["level"]) == (1, "low")
    moderate = run(*history_events, event("CBC", "test_order", "I ordered CBC today."),
                   event("TSH", "test_order", "I ordered TSH today."))
    assert (moderate["category_one_units"], moderate["level"]) == (3, "moderate")
    unsupported = "Mother was present as a translator."
    assert not run(event("mother", "independent_historian", unsupported, unsupported))["independent_historian"]


INTERPRETATION = "I independently interpreted chest X-ray today: no focal infiltrate."
NOT_BILLED = "I will not separately report this interpretation."
DISCUSSION = "I discussed the treatment plan with outside cardiology at North Clinic today; we agreed to adjust therapy."
DISCUSSION_NOT_BILLED = "I will not separately report this discussion."


def interpretation():
    return event("chest X-ray", "independent_interpretation", INTERPRETATION, NOT_BILLED)


def test_interpretation_order_and_discussion_retain_separate_work():
    result = run(interpretation(), event("chest X-ray", "test_order", "I ordered chest X-ray today."),
                 event("chest X-ray", "test_result_review", "I reviewed chest X-ray today."),
                 event("cardiology", "external_discussion", DISCUSSION, DISCUSSION_NOT_BILLED))
    assert (result["level"], result["category_one_units"]) == ("high", 1)
    assert result["qualifying_categories"] == ["interpretation", "discussion"]
    assert [e["status"] for e in result["events"]] == ["counted", "counted", "duplicate", "counted"]


@pytest.mark.parametrize("quote,subject,qual", [
    ("I independently interpreted CBC today: hemoglobin is low.", "CBC", NOT_BILLED),
    ("I read the chest X-ray report today.", "chest X-ray", NOT_BILLED),
    (INTERPRETATION, "chest X-ray", ""),
    ("I independently interpreted chest X-ray today.", "chest X-ray", NOT_BILLED),
])
def test_interpretation_requires_eligible_test_findings_and_reporting_status(quote, subject, qual):
    assert not run(event(subject, "independent_interpretation", quote, qual))["independent_interpretation"]


def test_separately_reported_professional_component_excludes_order_too():
    order = event("chest X-ray", "test_order", "I ordered chest X-ray today.")
    note = order["action_quote"] + " I separately reported chest X-ray interpretation."
    assert run(order, note=note)["category_one_units"] == 0


@pytest.mark.parametrize("quote", [
    "I will discuss the treatment plan with outside cardiology at North Clinic.",
    "I sent the treatment plan to outside cardiology at North Clinic; reply pending.",
    "I discussed the treatment plan with the patient today.",
])
def test_uncompleted_or_patient_discussions_do_not_qualify(quote):
    assert not run(event("cardiology", "external_discussion", quote))["external_discussion"]


@pytest.mark.parametrize("text,action,subject", [
    ("The visiting physician ordered TSH today.", "test_order", "TSH"),
    ("I reviewed TSH on 2026-08-01.", "test_result_review", "TSH"),
    ("The patient asked whether I ordered CBC today.", "test_order", "CBC"),
    ("I discussed management today with outside cardiology at Harbor Clinic and separately billed the interprofessional consultation.", "external_discussion", "cardiology"),
    ("I independently interpreted chest X-ray today: the interpretation is still pending.", "independent_interpretation", "chest X-ray"),
])
def test_independent_review_accounting_regressions(text, action, subject):
    result = run(event(subject, action, text, NOT_BILLED))
    assert result["level"] == "straightforward"
    assert result["category_one_units"] == 0


def test_external_identity_cannot_be_borrowed_from_an_unrelated_anchor():
    identity = "An outside cardiology note from Harbor Clinic is available."
    for action, quote in [("external_note_review", "I reviewed my own note today."),
                          ("external_discussion", "I discussed management with the spouse today.")]:
        raw = event("Harbor Clinic", action, quote, DISCUSSION_NOT_BILLED, identity_quote=identity)
        assert run(raw)["level"] == "straightforward"
    for action, quote in [
        ("external_note_review", "I reviewed my own note about outside cardiology at Harbor Clinic today."),
        ("external_discussion", "I discussed management with my spouse about outside cardiology at Harbor Clinic today.")]:
        assert run(event("Harbor Clinic", action, quote, DISCUSSION_NOT_BILLED))["level"] == "straightforward"


def test_actual_order_clause_can_follow_other_clinical_management():
    quote = "Continue metformin ER without dose change; order an external basic metabolic panel and A1c because the prior results are old."
    result = run(*(event(t, "test_order", quote) for t in ("basic metabolic panel", "A1c")))
    assert result["category_one_units"] == 2


def test_pronominal_separately_reported_interpretation_excludes_its_review():
    quote = "I reviewed chest X-ray today."
    assert run(event("chest X-ray", "test_result_review", quote),
               note=quote + " I separately reported its interpretation.")["category_one_units"] == 0
    assert run(event("chest X-ray", "test_result_review", quote),
               note=quote + " I am separately reporting its interpretation.")["category_one_units"] == 0
    for passive in ("Chest X-ray interpretation was separately reported by me.",
                    "The chest X-ray interpretation is separately billed by me."):
        assert run(event("chest X-ray", "test_result_review", quote),
                   note=quote + "\n" + passive)["category_one_units"] == 0


@pytest.mark.parametrize("quote,qualification", [
    ("I independently interpreted chest X-ray today: interpretation completed.", NOT_BILLED),
    (INTERPRETATION, "If I do not separately report this interpretation, it can be counted in MDM."),
    (INTERPRETATION, "I will not separately report the ECG interpretation."),
])
def test_interpretation_requires_actual_findings_and_an_affirmative_same_service_attestation(quote, qualification):
    result = run(event("chest X-ray", "independent_interpretation", quote, qualification))
    assert not result["independent_interpretation"]


@pytest.mark.parametrize("reporting", [
    "I am separately reporting chest X-ray interpretation.",
    "I separately reported chest X-ray interpretation; I will not separately report the ECG interpretation.",
    "I separately billed chest X-ray interpretation but I will not separately report the ECG interpretation.",
])
def test_each_reporting_verb_is_scoped_to_its_own_service_and_negation(reporting):
    note = INTERPRETATION + "\n" + NOT_BILLED + "\n" + reporting
    assert not run(interpretation(), note=note)["independent_interpretation"]


def test_indication_for_a_test_is_not_an_independent_finding():
    text = "I independently interpreted chest X-ray today: ordered to evaluate for infiltrate."
    assert not run(event("chest X-ray", "independent_interpretation", text, NOT_BILLED))["independent_interpretation"]


@pytest.mark.parametrize("separator", ["; ", " and "])
def test_available_record_cannot_borrow_an_unrelated_review_verb(separator):
    quote = "I reviewed the medication list today" + separator + "an outside cardiology note from Harbor Clinic is available."
    assert run(event("Harbor Clinic", "external_note_review", quote))["category_one_units"] == 0


def test_omitted_following_allocation_and_historical_passive_review_are_held():
    quote = "I reviewed TSH today."
    result = run(event("TSH", "test_result_review", quote),
                 note=quote + " This result was ordered and counted during the previous encounter.")
    assert result["category_one_units"] == 0
    historical = "TSH was reviewed last week for today's visit."
    assert run(event("TSH", "test_result_review", historical))["category_one_units"] == 0


@pytest.mark.parametrize("subject", ["", " ", "\r", "\ud800", None])
def test_malformed_subject_becomes_a_reviewable_static_label(subject):
    quote = "I ordered CBC today."
    result = run(event("CBC", "test_order", quote), event(subject, "test_order", quote))
    assert result["category_one_units"] == 1
    assert result["events"][1]["subject"] == "Unclassified data"
    assert result["events"][1]["status"] == "held"


@pytest.mark.parametrize("quote,action,subject", [
    ("I ordered TSH today; free T4 is normal.", "test_order", "free T4"),
    ("I reviewed TSH today; free T4 results are unavailable.", "test_result_review", "free T4"),
    ("I ordered TSH and free T4 is normal.", "test_order", "free T4"),
    ("Today I documented this history: TSH was reviewed last week.", "test_result_review", "TSH"),
    ("I reviewed TSH today; this test was ordered and counted during the previous encounter.", "test_result_review", "TSH"),
])
def test_test_identity_action_and_date_must_be_a_single_supported_relation(quote, action, subject):
    assert run(event(subject, action, quote))["category_one_units"] == 0


def test_patient_report_heading_is_not_lost_at_a_line_break():
    quote = "I ordered TSH today."
    assert run(event("TSH", "test_order", quote), note="Patient report:\n" + quote)["category_one_units"] == 0


@pytest.mark.parametrize("qualification,expected", [
    ("No confirmatory history was needed; the patient provided a complete and reliable history.", False),
    ("Confirmatory history was not required; the patient provided a complete and reliable history.", False),
    ("Confirmatory history was unnecessary.", False),
    ("There was no need for confirmatory history.", False),
    ("It is not true that the patient is unable to provide reliable history.", False),
    ("The patient is unable to provide reliable history?", False),
    ("Confirmatory history was needed?", False),
    ("Confirmatory history will be needed if the patient is unable to provide history.", False),
    ("The patient was unable to provide reliable history yesterday.", False),
    ("Confirmatory history was needed. Correction: this was not required.", False),
    ("Confirmatory history was needed because the patient could not recall the medication changes.", True),
    ("The patient could not provide reliable history.", True),
    ("Confirmatory history would be needed if the patient could not recall the medication changes.", False),
    ("Confirmatory history was needed yesterday because the patient could not recall the medication changes.", False),
    ("Confirmatory history was needed but could not be obtained.", False),
    ("Confirmatory history was needed.", True),
    ("Conflicting history required.", True),
    ("I needed confirmatory history.", True),
    ("The patient is unable to provide complete and reliable history.", True),
    ("The patient cannot give reliable history.", True),
])
def test_historian_credit_requires_affirmed_current_need(qualification, expected):
    historian = event("mother", "independent_historian", "I obtained history from the mother today.", qualification)
    result = run(historian, event("CBC", "test_order", "I ordered CBC today."),
                 event("TSH", "test_order", "I ordered TSH today."))
    assert result["independent_historian"] is expected
    assert (result["category_one_units"], result["level"]) == ((3, "moderate") if expected else (2, "low"))
    assert result["events"][0]["status"] == ("counted" if expected else "held")
    assert qualification in [a["quote"] for a in result["events"][0]["evidence"]]


def test_clipped_historian_qualification_cannot_remove_denied_need():
    action = "I obtained history from the mother today."
    historian = event("mother", "independent_historian", action, "confirmatory history was needed")
    note = action + " No confirmatory history was needed; the patient provided reliable history."
    result = run(historian, note=note)
    assert result["independent_historian"] is False
    assert result["category_one_units"] == 0
    assert result["events"][0]["status"] == "held"
    assert any(a["quote"].startswith("No confirmatory") for a in result["events"][0]["evidence"])


def test_held_event_evidence_cannot_authorize_capture_of_an_unrelated_code():
    quote = "I reviewed my medication list today."
    source = bundle(quote)
    ledger = qualify_mdm_data([event("TSH", "test_result_review", quote)], source)
    assert ledger.events[0].evidence
    assert not ledger.evidence()
    facts = {"problems": [{"name": "invented problem", "status": "new"}], "risk": {"level": "moderate"}}
    from cosmaneura_billing.schemas import CodeSuggestion, Disposition
    code = CodeSuggestion(CodeKind.EM, "99214", "Office visit", evidence=ledger.evidence(), data_review=ledger.public)
    guarded, _ = ComplianceGuardrailAgent().run(facts, [code], new_patient=False)
    assert guarded[0].disposition == Disposition.REVIEW


def test_even_counted_data_alone_cannot_authorize_unsubstantiated_problem_and_risk():
    quote = "I ordered CBC, TSH and A1c today."
    raw = [event(t, "test_order", quote) for t in ("CBC", "TSH", "A1c")]
    ledger = qualify_mdm_data(raw, bundle(quote))
    facts = {"problems": [{"name": "invented problem", "status": "new"}], "risk": {"level": "moderate"},
             "mdm_data_events": raw, "_qualified_mdm_data": ledger}
    from cosmaneura_billing.schemas import CodeSuggestion, Disposition
    code = CodeSuggestion(CodeKind.EM, "99214", "Office visit", evidence=ledger.evidence(), data_review=ledger.public)
    guarded, _ = ComplianceGuardrailAgent().run(facts, [code], new_patient=False)
    assert guarded[0].disposition == Disposition.REVIEW


def test_legacy_categories_and_raw_json_counts_cannot_set_data_level():
    raw = {"problems": [{"name": "rash", "status": "new"}], "risk": {"level": "straightforward"},
           "data_reviewed": [{"category": str(i), "item": "CBC"} for i in range(10)],
           "_qualified_mdm_data": {"level": "high"}, "data_review": {"level": "high"}}
    assert _facts_to_em(raw, new_patient=False).elements["data"] == "straightforward"


class FactsLLM(MockLLM):
    def __init__(self, events):
        self.events = events

    def complete_json(self, **kwargs):
        text = "Asthma is in moderate exacerbation; I evaluated it and adjusted treatment."
        return {"chief_complaint": "asthma", "mdm_problem_events": [{"name": "asthma", "source": "signed_note",
                    "identity_quote": text, "addressed_quote": text, "status_quote": text}], "mdm_risk_events": [],
                "mdm_data_events": deepcopy(self.events)} if "extract_clinical_facts" in kwargs["user"] else {"diagnoses": []}


def test_full_pipeline_uses_same_immutable_ledger_and_retains_exact_event_evidence():
    quote = "I ordered CBC, TSH and A1c today."
    raw = [event(t, "test_order", quote) for t in ("CBC", "TSH", "A1c")]
    source = bundle("Asthma is in moderate exacerbation; I evaluated it and adjusted treatment. " + quote)
    kb = ICD10CCSR()
    facts, suggestions = CodingAgent(FactsLLM(raw), kb).run(source)
    assert _facts_to_em(facts, new_patient=False).code == "99214"
    merged, _ = merge_facts(facts, {"mdm_data_events": [], "_qualified_mdm_data": {"level": "high"}}, new_patient=False)
    assert _facts_to_em(merged, new_patient=False).code == "99214"
    tampered = {**facts, "mdm_data_events": raw + [raw[0]]}
    assert _facts_to_em(tampered, new_patient=False).elements["data"] == "straightforward"
    suggestions[0].code = "99215"
    guarded, _ = ComplianceGuardrailAgent().run(facts, suggestions, new_patient=False)
    assert guarded[0].code == "99214"
    settings = Settings(preferences_enabled=False, azure_openai_enabled=False,
                        azure_language_enabled=False, azure_language_shadow_enabled=False)
    result = BillingOrchestrator(llm=FactsLLM(raw), icd10=kb, settings=settings).process(source)
    proposed = next(s for s in result.provider_card.captured_codes + result.provider_card.declined_codes if s.kind == CodeKind.EM)
    assert proposed.code == "99214" and proposed.data_review["level"] == "moderate"
    assert {e.rationale for e in proposed.evidence if e.rationale.startswith("MDM data:")} == {"MDM data: mdm-data-1", "MDM data: mdm-data-2", "MDM data: mdm-data-3"}
    assert all(e.quote in source.signed_note for e in proposed.evidence)
    assert "_qualified_mdm_data" not in json.dumps(asdict(result))
