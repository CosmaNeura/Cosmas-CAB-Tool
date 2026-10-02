"""Direct encounter-care proof; pipeline effects are deferred to C6."""
from copy import deepcopy
from dataclasses import replace

import pytest

from cosmaneura_billing import encounter_care as care
from cosmaneura_billing.mdm_data import qualify_mdm_data
from cosmaneura_billing.mdm_problems import qualify_problems
from cosmaneura_billing.schemas import VisitBundle


ASSESSMENT = "Assessment is abdominal pain with cause unconfirmed, plus a request for preconception counseling."


ORDER = "I ordered TSH for prompt outpatient assessment."


CONTINUATION = "Polycystic ovary syndrome remains a differential diagnosis; obesity and irregular cycles alone do not establish it."


def inputs(*, assessment=ASSESSMENT, order=ORDER, name="abdominal pain", gap="", tail="", prefix="",
           data_action="test_order", time=False):
    note = prefix + assessment + " " + (gap + " " if gap else "") + order + tail
    raw = {"mdm_problem_events": [{"name": name, "source": "signed_note",
        "identity_quote": assessment, "addressed_quote": order, "status_quote": assessment}],
        "mdm_data_events": [{"action": data_action, "subject": "TSH", "source": "signed_note",
            "identity_quote": order, "action_quote": order, "qualification_quote": ""}],
        "mdm_risk_events": []}
    if time:
        quote = "Total physician time on the date of service: 26 minutes."
        note += "\n\n" + quote
        raw.update(total_time_minutes=26, total_time_quote=quote)
    bundle = VisitBundle("synthetic-encounter-care", 30, "F", "office_established", "11", signed_note=note)
    return bundle, raw


def qualify(bundle, raw):
    p = qualify_problems(raw["mdm_problem_events"], bundle)
    d = qualify_mdm_data(raw["mdm_data_events"], bundle, [])
    proof = care.qualify_encounter_care(raw["mdm_problem_events"], raw["mdm_data_events"], bundle, p, d)
    return proof, {**raw, "_qualified_problems": p, "_qualified_mdm_data": d}


@pytest.mark.parametrize("gap", ["", CONTINUATION])
def test_combined_assessment_supports_encounter_work_without_individual_ownership(gap):
    bundle, raw = inputs(gap=gap)
    proof, facts = qualify(bundle, raw)
    assert len(proof.groups) == 1 and proof.supports(facts, proof.evidence())
    assert not facts["_qualified_problems"].has_addressed
    assert not facts["_qualified_problems"].established
    assert facts["_qualified_mdm_data"].public["category_one_units"] == 1
    assert proof.evidence()[0].quote == bundle.signed_note
    if gap:
        assert CONTINUATION in proof.evidence()[0].quote
    assert all(e.quote in bundle.signed_note for e in proof.evidence())


@pytest.mark.parametrize("change", [
    {"assessment": "Assessment is a request for preconception counseling.", "name": "request for preconception counseling"},
    {"assessment": "Assessment is a request for abdominal pain evaluation.", "name": "abdominal pain"},
    {"assessment": "Assessment is requested evaluation of abdominal pain."},
    {"assessment": "Assessment is patient requests evaluation of abdominal pain."},
    {"assessment": "Assessment is abdominal pain screening."},
    {"assessment": "Assessment is a desire for abdominal pain counseling.", "name": "abdominal pain"},
    {"assessment": "Assessment is history of abdominal pain.", "name": "abdominal pain"},
    {"assessment": "Assessment is screening for thyroid disease.", "name": "screening for thyroid disease"},
    {"prefix": "Copied outside note:\n"},
    {"prefix": "Patient:\n"},
    {"prefix": "Family history:\n"},
    {"order": "The nurse ordered TSH for prompt outpatient assessment."},
    {"order": "I will order TSH for prompt outpatient assessment."},
    {"order": "If symptoms persist, I ordered TSH."},
    {"order": "I ordered TSH for another patient."},
    {"order": "I ordered TSH for hypothyroidism."},
    {"order": "I reviewed TSH today.", "data_action": "test_result_review"},
    {"gap": "\n\n"},
    {"gap": "I evaluated the hypertension."},
    {"gap": "Patient: I want a thyroid test."},
    {"gap": CONTINUATION + " A separate assessment is hypertension."},
])
def test_only_current_patient_assessment_and_completed_group_order_qualify(change):
    proof, facts = qualify(*inputs(**change))
    assert not proof.groups and not proof.supports(facts, proof.evidence())


@pytest.mark.parametrize("correction", [
    "Correction: those orders were never placed.",
    "The orders were canceled.",
    "I did not actually place those orders.",
    "Those orders belonged to another patient.",
    "The TSH was ordered by the nurse.",
    "Correction: the assessment and orders were copied from another patient.",
    "This evaluation was not performed.",
    "No orders were placed.",
    "None of these tests were ordered.",
    "These orders were entered in error.",
    "No current assessment was performed.",
    "No evaluation was performed.",
    "No current evaluation was performed.",
])
@pytest.mark.parametrize("separator", [" ", " " + "Results will be reviewed at follow-up. " * 12, "\n\n"])
def test_retraction_cannot_hide_beyond_short_order_lookahead(correction, separator):
    bundle, raw = inputs(tail=separator + correction)
    proof, facts = qualify(bundle, raw)
    assert not proof.groups
    assert not proof.supports(facts, proof.evidence())


@pytest.mark.parametrize("speaker,expected", [("Physician", True), ("Provider", False), ("Doctor", False)])
@pytest.mark.parametrize("source", ["transcript", "signed_note"])
def test_assessment_and_order_cannot_cross_speakers(speaker, expected, source):
    assessment, order = "Physician: " + ASSESSMENT, speaker + ": " + ORDER
    bundle, raw = inputs(assessment=assessment, order=order)
    note = bundle.signed_note.replace(". " + speaker + ":", ".\n" + speaker + ":")
    bundle.signed_note = note if source == "signed_note" else ""
    bundle.transcript = note if source == "transcript" else ""
    for key in ("mdm_problem_events", "mdm_data_events"):
        raw[key][0]["source"] = source
    raw["mdm_problem_events"][0]["addressed_quote"] = assessment
    proof, facts = qualify(bundle, raw)
    assert bool(proof.groups) is expected
    assert proof.supports(facts, proof.evidence()) is expected


@pytest.mark.parametrize("tail", [" No results are available.",
    " Polycystic ovary syndrome is not established.",
    " Nurse: No orders were placed."])
def test_missing_results_uncertain_diagnosis_and_other_speakers_do_not_retract_work(tail):
    proof, facts = qualify(*inputs(tail=tail))
    assert proof.groups and proof.supports(facts, proof.evidence())


@pytest.mark.parametrize("tamper", ["lost-context", "lost-order", "wrong-role", "altered-quote",
                                    "raw-problem", "raw-data", "source", "qualified-order"])
def test_every_component_of_the_private_proof_is_bound(tamper):
    bundle, raw = inputs()
    proof, facts = qualify(bundle, raw)
    evidence = proof.evidence()
    assert proof.supports(facts, evidence)
    if tamper == "lost-context":
        evidence = evidence[1:]
    elif tamper == "lost-order":
        evidence = evidence[:1]
    elif tamper == "wrong-role":
        evidence[0] = replace(evidence[0], rationale="MDM problems: mdm-problem-1")
    elif tamper == "altered-quote":
        evidence[0] = replace(evidence[0], quote=ORDER)
    elif tamper == "raw-problem":
        facts["mdm_problem_events"] = deepcopy(raw["mdm_problem_events"])
        facts["mdm_problem_events"][0]["name"] = "hypertension"
    elif tamper == "raw-data":
        facts["mdm_data_events"] = deepcopy(raw["mdm_data_events"])
        facts["mdm_data_events"][0]["subject"] = "CBC"
    elif tamper == "source":
        facts["_qualified_mdm_data"] = replace(facts["_qualified_mdm_data"], source_sha256="0" * 64)
    else:
        d = facts["_qualified_mdm_data"]
        facts["_qualified_mdm_data"] = replace(d, events=(replace(d.events[0], status="held"),))
    assert not proof.supports(facts, evidence)
