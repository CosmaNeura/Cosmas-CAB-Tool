"""Exact direct product regressions retained from 8d0caca clinical recognition.

The CASES-dependent dose/evidence parity replay remains in the C6 selection.
"""
from copy import deepcopy

import pytest

from cosmaneura_billing import mdm_risk as risk
from cosmaneura_billing.schemas import VisitBundle


def source(note):
    return VisitBundle("synthetic-recognition", 40, "F", "office_established", "11", signed_note=note)


def medication(subject, action):
    return dict(action="medication_management", subject=subject, source="signed_note",
                action_quote=action, decision_quote=action, qualification_quote="")


@pytest.mark.parametrize("suffix", ["herbal blend", "extended-release", "and another product", "with another product"])
@pytest.mark.parametrize("subject,regimen", [
    ("metformin", "500 mg daily"), ("metformin 500 mg", "500 mg daily"),
    ("metformin 500 mg daily", "500 mg daily"), ("metformin 500mg", "500mg daily"),
    ("metformin 500 mg orally daily", "500 mg orally daily"),
])
def test_source_product_suffix_cannot_hide_after_a_recognized_regimen(subject, regimen, suffix):
    action = f"Start metformin {regimen} {suffix} today."
    q = risk.qualify_risk([medication(subject, action)], source(action))
    assert not q.established and not q.evidence()


@pytest.mark.parametrize("purpose", ["for topical use", "during topical application", "because of extended-release formulation"])
@pytest.mark.parametrize("subject", ["metformin", "metformin 500 mg"])
def test_administration_purpose_is_not_a_clinical_indication_outside_product(subject, purpose):
    action = f"Start metformin 500 mg daily {purpose}."
    assert not risk.qualify_risk([medication(subject, action)], source(action)).established


@pytest.mark.parametrize("suffix", ["herbal blend", "extended-release", "with another product", "for topical use"])
@pytest.mark.parametrize("subject", ["metformin", "metformin 500 mg daily"])
def test_considered_medication_retains_the_complete_source_product_boundary(subject, suffix):
    action = f"Considered metformin 500 mg daily {suffix} after shared decision-making; patient declined."
    raw = medication(subject, action); before = deepcopy(raw)
    q = risk.qualify_risk([raw], source(action))
    assert not q.established and not q.evidence()
    assert q.public["events"][0]["subject"] == subject and q.matches([raw])
    assert raw == before


@pytest.mark.parametrize("decision", [
    "after shared decision-making; patient declined.",
    "today; after shared decision-making the patient declined.",
])
def test_considered_declined_regimen_and_bare_product_keep_exact_same_decision(decision):
    action = "I considered metformin 500 mg daily " + decision
    results = [risk.qualify_risk([medication(subject, action)], source(action))
               for subject in ("metformin", "metformin 500 mg daily")]
    assert all(q.established and q.level == "moderate" for q in results)
    assert results[0].evidence() == results[1].evidence()
    assert [e.quote for e in results[0].evidence()] == [action]


@pytest.mark.parametrize("subject,action", [
    ("metformin 500 mg extended-release", "Start metformin 500 mg extended-release today."),
    ("metformin ER 500 mg", "Start metformin ER 500 mg daily today."),
    ("nitrofurantoin monohydrate/macrocrystals 100 mg orally", "Begin nitrofurantoin monohydrate/macrocrystals 100 mg orally twice daily for five days after specimen collection."),
    ("controller inhaler", "Continue the controller inhaler and review technique."),
    ("levothyroxine 75 micrograms daily", "Continue levothyroxine 75 micrograms daily."),
])
def test_unknown_whole_products_and_unassessed_continuation_stay_held(subject, action):
    q = risk.qualify_risk([medication(subject, action)], source(action))
    assert not q.established and not q.evidence()


def test_product_concentration_formulation_and_otc_status_are_not_erased():
    action = "Start topical diclofenac 1% gel today; topical diclofenac 1% gel is over-the-counter."
    q = risk.qualify_risk([medication("topical diclofenac 1% gel", action)], source(action))
    assert q.level == "low" and q.events[0].category == "nonprescription_management"
    assert q.events[0].subject == "topical diclofenac 1% gel"
    action = "Start ibuprofen 200 mg orally daily today."
    q = risk.qualify_risk([medication("ibuprofen 200 mg orally daily", action)], source(action))
    assert q.level == "low" and q.events[0].category == "nonprescription_management"


def test_full_source_object_status_is_not_lost_when_its_regimen_is_separated():
    action = "Start synthetic medicine 20 mg daily today."
    raw = medication("synthetic medicine 20 mg daily", action)
    for status, level in (("prescription-only", "moderate"), ("over-the-counter", "low")):
        quote = f"Synthetic medicine 20 mg daily is {status}."
        raw["qualification_quote"] = quote
        q = risk.qualify_risk([raw], source(action + " " + quote))
        assert q.level == level and q.established
        assert quote in [e.quote for e in q.evidence()]
    conflicting = "Synthetic medicine is prescription-only. Synthetic medicine 20 mg daily is over-the-counter."
    raw["qualification_quote"] = conflicting
    assert not risk.qualify_risk([raw], source(action + " " + conflicting)).established


@pytest.mark.parametrize("action", [
    "Patient reports: Start prednisone 40 mg daily today.",
    "The nurse started prednisone 40 mg daily today.",
    "I did not start prednisone 40 mg daily today.",
    "I may start prednisone 40 mg daily next month.",
    "I started prednisone 40 mg daily yesterday.",
    "Begin prednisone 40 mg daily if the specialist authorizes it next month.",
])
def test_projected_medication_identity_never_overrides_actor_timing_or_polarity(action):
    assert not risk.qualify_risk([medication("prednisone 40 mg daily", action)], source(action)).established


@pytest.mark.parametrize("action", [
    "Begin metformin at 500 mg extended-release daily today.",
    "Begin metformin orally as extended-release tablets today.",
    "Begin metformin by mouth at 500 mg extended-release daily today.",
    "Begin metformin from 500 mg to 1000 mg extended-release daily today.",
    "Considered metformin orally as extended-release tablets after shared decision-making; patient declined.",
])
def test_unknown_product_cannot_hide_behind_preposition_or_route(action):
    raw = medication("metformin", action)
    before = deepcopy(raw)
    q = risk.qualify_risk([raw], source(action))
    assert not q.established and not q.evidence()
    assert q.matches([raw]) and raw == before


@pytest.mark.parametrize("regimen", ["at 500 mg daily", "orally", "by mouth at 500 mg daily"])
def test_supported_route_and_preposition_keep_exact_source(regimen):
    action = f"Begin metformin {regimen} today."
    q = risk.qualify_risk([medication("metformin", action)], source(action))
    assert q.established and q.level == "moderate"
    assert [e.quote for e in q.evidence()] == [action]


def test_whole_unknown_formulation_needs_its_own_exact_product_status():
    action = "Begin metformin extended-release at 500 mg daily today."
    status = "Metformin extended-release is prescription-only."
    raw = {**medication("metformin extended-release", action), "qualification_quote": status}
    q = risk.qualify_risk([raw], source(action + " " + status))
    assert q.established and q.level == "moderate"
    assert {e.quote for e in q.evidence()} == {action, status}
    assert not risk.qualify_risk([{**raw, "subject": "metformin"}], source(action + " " + status)).established
