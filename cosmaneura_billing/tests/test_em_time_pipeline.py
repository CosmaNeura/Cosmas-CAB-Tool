"""Time must be exact, clinician-attested and eligible before it can level E/M."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json

import pytest

from cosmaneura_billing.agents.coding import CodingAgent, _facts_to_em, sanitize_facts
from cosmaneura_billing.agents.compliance_guardrail import ComplianceGuardrailAgent
from cosmaneura_billing.agents.consensus import merge_facts
from cosmaneura_billing.em_time import qualify_em_time
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition, VisitBundle
from cosmaneura_billing.settings import Settings

TOTAL = "Total time spent on the date of the encounter: 42 minutes."
PROBLEM = "Rash: self-limited problem; I evaluated it and discussed care."
BASE = {
    "chief_complaint": "Minor resolving rash",
    "mdm_problem_events": [{"name": "rash", "source": "signed_note", "identity_quote": PROBLEM,
                            "addressed_quote": PROBLEM, "status_quote": PROBLEM}],
    "mdm_data_events": [], "mdm_risk_events": [],
}


class FactsLLM(MockLLM):
    def __init__(self, minutes, quote, **extra):
        self.facts = {**deepcopy(BASE), "total_time_minutes": minutes,
                      "total_time_quote": quote, **extra}

    def complete_json(self, **kwargs):
        return deepcopy(self.facts) if "extract_clinical_facts" in kwargs["user"] else {"diagnoses": []}


def bundle(note=TOTAL, **changes):
    return VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11",
        signed_note=PROBLEM + " Reassurance only. " + note, **changes)


@pytest.fixture(scope="module")
def kb():
    return ICD10CCSR()


def process(source, minutes, quote, kb):
    settings = Settings(preferences_enabled=False, azure_openai_enabled=False,
                        azure_language_enabled=False, azure_language_shadow_enabled=False)
    return BillingOrchestrator(llm=FactsLLM(minutes, quote), icd10=kb,
                               settings=settings).process(source)


def em(result):
    return next(s for s in result.provider_card.captured_codes + result.provider_card.declined_codes
                if s.kind == CodeKind.EM)


@pytest.mark.parametrize("text,minutes", [
    (TOTAL, 45),
    ("Reassurance only.", 45),
    ("The patient spent 45 minutes completing intake.", 45),
    ("Total physician time on the date of service: 42 minutes yesterday.", 42),
    ("Total nursing time today: 42 minutes.", 42),
    ("Total staff time today: 42 minutes.", 42),
    ("Total time today: 42 minutes waiting.", 42),
    ("Total time today for the procedure: 42 minutes.", 42),
    ("Total time today for the psychotherapy session: 42 minutes.", 42),
    ("Scheduled total physician time today: 42 minutes.", 42),
    ("No total physician time on the date of service: 42 minutes.", 42),
    ("Example: Total physician time on the date of service: 42 minutes.", 42),
    ("Total physician time today: approximately 42 minutes.", 42),
    ("Total physician time today: 30 to 42 minutes.", 42),
    ("Total physician time today: 40-42 minutes.", 42),
    ("Total physician time today: -42 minutes.", 42),
    ("Total physician time today: - 42 minutes.", 42),
    ("Total physician time today: 1.42 minutes.", 42),
    ("Total physician time today: 1,042 minutes.", 42),
    ("Total physician time today: 2000 minutes.", 2000),
    ("Total physician time today: 42 minutes across two days.", 42),
    ("A total of 45 minutes today was spent exercising.", 45),
    ("Total physician time today: 42 minutes including travel.", 42),
    ("Total physician time today: 42 minutes including commuting.", 42),
    ("Total time today for intake: 42 minutes.", 42),
    ("Total physician time today was less than 45 minutes.", 45),
    ("Total physician time today was under 45 minutes.", 45),
    ("Total physician time today was 45 minutes at most.", 45),
    ("Total physician time today: <45 minutes.", 45),
    ("Total physician time today was 45 minutes or less.", 45),
    ("Total physician time today was fewer than 45 minutes.", 45),
    ("Total physician time today was roughly 45 minutes.", 45),
    ("Total physician time today was 45 minutes maximum.", 45),
    ("Total physician time today was 45 minutes, give or take.", 45),
    ("Total physician time today was 45 minutes?", 45),
    ("Total physician time today was 45 minutes; corrected to 15 minutes.", 45),
    ("Total physician time today was 45 minutes reviewing records; corrected to 15 minutes.", 45),
])
def test_ineligible_or_mismatched_time_never_inflates_full_pipeline(text, minutes, kb):
    result = process(bundle(text), minutes, text, kb)
    assert em(result).code == "99212"
    assert "em_pathway:mdm_2of3" in em(result).notes
    assert any(n.startswith("em_time_held:") for n in em(result).notes)
    assert not any(n.startswith("em_total_time_minutes:") for n in em(result).notes)


@pytest.mark.parametrize("text", [
    TOTAL,
    "Total physician time on the date of service: 42 minutes.",
    "Total QHP E/M time today: 42 minutes.",
    "I spent a total of 42 minutes today evaluating and managing the patient.",
    "Total physician time on the service date: 42 minutes, excluding separately reported procedures.",
    "Total physician time today: 42 minutes including 10 minutes of chart review and 32 minutes of counseling.",
])
def test_qualified_totals_keep_effective_time_pathway_and_json_output(text, kb):
    source = bundle(text)
    result = process(source, 42, text, kb)
    proposed = em(result)
    assert proposed.code == "99215" and proposed.disposition == Disposition.CAPTURED
    assert "em_mdm_code:99212" in proposed.notes
    assert proposed.evidence[0].source == "signed_note"
    assert proposed.evidence[0].quote == text
    payload = json.loads(json.dumps(asdict(result)))
    assert payload["biller_worksheet"]["em_code"]["code"] == "99215"
    assert "_qualified_em_time" not in json.dumps(payload)


# Exact time statements and their following paragraph context from the frozen,
# invented 12-note September 30 corpus (encounters.json SHA256 a2d334c64b8b0bf6607c3ac70539d105d482329c590d018bfebf5975614e6afc).
# These check original amounts/context without depending on operator files.
FOLLOWING_CORPUS_TIME_CONTEXT = {
    32: "Staff intake, waiting, travel, and separately billed procedures are excluded.",
    23: "This is physician work time, not a scheduled slot; staff time, technical setup, waiting and separately reported procedures are excluded.",
    28: "Staff time, waiting, and any separately reported service are excluded.",
    22: "No independent imaging interpretation, injection, or other separately reported procedure time is included. Staff work and waiting are excluded.",
    31: "Staff intake, waiting and any separately billed procedure time are excluded.",
    19: "Staff specimen handling, waiting and any separately reported laboratory work are excluded.",
    16: "This total excludes staff intake, waiting and separately reported services. No other condition was independently evaluated or treated during this visit.",
    34: "Staff time, waiting and separately reported procedures are excluded.",
    24: "Staff time, waiting, specimen collection and separately billed procedures are excluded.",
    44: "Staff, waiting, travel, separately reported procedures, and work on other dates are excluded.",
    26: "Staff time, waiting, connection setup, separately reported procedures, and work on other dates are excluded.",
    33: "Staff, waiting, separately reported procedure time, and work on other dates are excluded.",
}
@pytest.mark.parametrize("minutes,text", [
    (32, "Time: Total physician time personally spent on this date was 32 minutes: 6 reviewing relevant history and the external laboratory report, 20 evaluating/counseling the patient and making treatment decisions, and 6 documenting and arranging follow-up."),
    (23, "Time: Total same-day physician time was 23 minutes: 5 reviewing the selected history and home log, 15 in the video encounter, and 3 entering orders and documenting."),
    (28, "Time: Total physician time on this date was 28 minutes: 5 reviewing history and diary, 18 evaluating/counseling and selecting therapy, and 5 documenting and arranging follow-up."),
    (22, "Time: Total same-day physician work was 22 minutes: 4 reviewing the selected history and prior report, 14 examining and discussing management, and 4 documenting and placing the therapy referral."),
    (31, "Time: Total physician time on the date was 31 minutes: 5 reviewing the selected history and medications, 21 evaluating the exacerbation and explaining the plan, and 5 documenting and arranging prompt follow-up."),
    (19, "Time: Total physician time on this date was 19 minutes: 3 reviewing the relevant prior infection/renal information, 12 evaluating and counseling, and 4 prescribing, ordering external tests and documenting."),
    (16, "Time: Total physician time on this date was 16 minutes: 2 reviewing the recent episode, 11 obtaining interval history, examining and counseling, and 3 documenting."),
    (34, "Time: Total physician time on this date was 34 minutes: 8 reviewing the relevant prior report and medication history, 21 evaluating and counseling, and 5 documenting."),
    (24, "Time: Total physician time on the date was 24 minutes: 5 reviewing the selected history and current external report, 15 evaluating and counseling about administration, and 4 entering follow-up orders and documenting."),
    (44, "Total eligible physician/QHP time on this date was 44 minutes: 8 reviewing selected records, 27 obtaining history, examining and counseling, and 9 placing orders and documenting."),
    (26, "Total eligible physician/QHP time today was 26 minutes: 4 reviewing selected records, 18 in history and counseling, and 4 ordering and documenting."),
    (33, "Total eligible physician/QHP time today was 33 minutes: 6 reviewing records, 22 evaluating and counseling, and 5 prescribing, ordering and documenting."),
])
def test_all_frozen_corpus_time_attestations_remain_effective(minutes, text, kb):
    source = bundle(text + " " + FOLLOWING_CORPUS_TIME_CONTEXT[minutes])
    qualified = qualify_em_time(minutes, text, source).attestation
    assert qualified is not None and qualified.minutes == minutes
    facts, _ = CodingAgent(FactsLLM(minutes, text), kb).run(source)
    assert facts["total_time_evidenced"] is True
    assert _facts_to_em(facts, new_patient=False).total_time_minutes == minutes


@pytest.mark.parametrize("followup", [
    "This total excludes staff intake, waiting and separately reported services but includes nursing time.",
    "This total excludes staff intake, waiting and separately reported services; but includes nursing time.",
    "This total excludes staff intake, waiting and separately reported services. But includes nursing time.",
    "This total excludes an unrecognized component.",
])
def test_exclusion_statement_must_be_a_closed_list_without_hidden_inclusion(followup, kb):
    assert em(process(bundle(TOTAL + " " + followup), 42, TOTAL, kb)).code == "99212"


def test_long_quote_with_explicit_exclusions_keeps_exact_attestation_span(kb):
    total = "Total eligible physician/QHP time today was 33 minutes: 6 reviewing records, 22 evaluating and counseling, and 5 prescribing, ordering and documenting."
    text = total + " Staff, waiting, separately reported procedure time, and work on other dates are excluded."
    proposed = em(process(bundle(text), 33, text, kb))
    assert proposed.code == "99214"
    assert proposed.evidence[0].quote == total


def test_next_sentence_ineligible_included_component_invalidates_total(kb):
    text = TOTAL + " This includes 10 minutes of procedure time."
    assert em(process(bundle(text), 42, TOTAL, kb)).code == "99212"


@pytest.mark.parametrize("before,after", [
    ("Patient reports: ", ""),
    ("Pt stated: ", ""),
    ("Patient: ", ""),
    ("Per the patient: ", ""),
    ("Family says: ", ""),
    ("", " Of this, 30 minutes were nursing time."),
    ("", " That figure is incorrect; actual total today was 15 minutes."),
    ("", " This total included 30 minutes of nursing time."),
    ("", " This total was for today's encounter. Of this, 30 minutes were nursing time."),
])
def test_attribution_and_linked_corrections_cannot_be_trimmed_by_model(before, after, kb):
    quote = "Total physician time today was 45 minutes."
    proposed = em(process(bundle(before + quote + after), 45, quote, kb))
    assert proposed.code == "99212"
    assert any(n.startswith("em_time_held:") for n in proposed.notes)


def test_current_review_of_intake_is_not_patient_intake_time(kb):
    text = "Total physician time today: 42 minutes reviewing the intake questionnaire and counseling."
    assert em(process(bundle(text), 42, text, kb)).code == "99215"


def test_altered_numeral_does_not_pass_tolerant_quote_gate(kb):
    quote = "Total physician time on the date of the encounter, including chart review and counseling, was 45 minutes."
    result = process(bundle(quote.replace("45 minutes", "15 minutes")), 45, quote, kb)
    assert em(result).code == "99212"


@pytest.mark.parametrize("where", ["intake_text", "transcript"])
def test_patient_supplied_attestation_cannot_become_clinician_time(where, kb):
    source = bundle("", **{where: TOTAL})
    assert em(process(source, 42, TOTAL, kb)).code == "99212"


def test_explicit_qhp_transcript_attestation_preserves_source(kb):
    text = "QHP: My total QHP time on the date of service was 42 minutes."
    source = bundle("", transcript=text)
    proposed = em(process(source, 42, text, kb))
    assert proposed.code == "99215"
    assert proposed.evidence[0].source == "transcript"


def test_same_copied_text_in_intake_does_not_relabel_signed_attestation(kb):
    source = bundle(TOTAL, intake_text=TOTAL)
    proposed = em(process(source, 42, TOTAL, kb))
    assert proposed.code == "99215"
    assert proposed.evidence[0].source == "signed_note"


@pytest.mark.parametrize("other", ["signed_note", "transcript"])
def test_conflicting_current_totals_hold_time(other, kb):
    conflict = "QHP: Total QHP time on the date of service: 15 minutes."
    source = bundle(TOTAL + (" " + conflict if other == "signed_note" else ""),
                    transcript=conflict if other == "transcript" else "")
    proposed = em(process(source, 42, TOTAL, kb))
    assert proposed.code == "99212"
    assert "Conflicting clinician total-time" in proposed.rationale


def test_note_ambiguity_cannot_fall_through_to_a_convenient_transcript(kb):
    text = "Total QHP time today: 42 minutes."
    source = bundle(text + " " + text, transcript="QHP: " + text)
    assert em(process(source, 42, text, kb)).code == "99212"


def test_quote_may_not_trim_negation_from_the_attestation(kb):
    text = "Total physician time on the date of service: 42 minutes."
    assert em(process(bundle("No " + text), 42, text, kb)).code == "99212"


def test_qualified_amount_and_exact_span_are_bound_to_source(kb):
    source = bundle(TOTAL)
    facts, _ = CodingAgent(FactsLLM(42, TOTAL), kb).run(source)
    attestation = facts["_qualified_em_time"]
    assert source.signed_note[attestation.start:attestation.end] == attestation.quote
    assert attestation.source_sha256 == hashlib.sha256(source.signed_note.encode()).hexdigest()
    assert _facts_to_em(facts, new_patient=False).code == "99215"
    assert _facts_to_em({**facts, "total_time_minutes": 90}, new_patient=False).code == "99212"
    assert _facts_to_em({**facts, "total_time_quote": "42 minutes"}, new_patient=False).code == "99212"


def test_raw_json_flags_and_fake_attestation_do_not_authorize_time():
    raw = {**BASE, "total_time_minutes": 42, "total_time_quote": TOTAL,
           "total_time_evidenced": True, "_qualified_em_time": {"minutes": 42, "quote": TOTAL}}
    assert sanitize_facts(raw)["total_time_evidenced"] is False
    assert _facts_to_em(raw, new_patient=False).code == "99212"


def test_guardrail_and_consensus_keep_same_qualified_ceiling(kb):
    source = bundle(TOTAL)
    facts, proposed = CodingAgent(FactsLLM(42, TOTAL), kb).run(source)
    merged, _ = merge_facts(facts, {"total_time_minutes": 100, "ta4h_note": "synthetic"}, new_patient=False)
    assert _facts_to_em(merged, new_patient=False).code == "99215"
    original = next(s for s in proposed if s.kind == CodeKind.EM)
    lower = replace(original, code="99213")
    guarded, _ = ComplianceGuardrailAgent().run(merged, [lower], new_patient=False)
    assert guarded[0].code == "99213"
    without = {**facts, "_qualified_em_time": None}
    guarded, _ = ComplianceGuardrailAgent().run(without, [original], new_patient=False)
    assert guarded[0].code == "99212"


def test_valid_new_patient_time_stays_available(kb):
    text = TOTAL.replace("42", "60")
    source = replace(bundle(text), encounter_type="office_new")
    assert em(process(source, 60, text, kb)).code == "99205"


def test_missing_time_has_no_invented_attestation():
    assert qualify_em_time(None, "", bundle()).attestation is None
