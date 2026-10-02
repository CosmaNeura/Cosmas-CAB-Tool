"""Source work and clinical status, rather than extracted labels, set problems."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from cosmaneura_billing.mdm_problems import VERSION, qualify_problems
from cosmaneura_billing.schemas import Evidence, VisitBundle


def bundle(note="", transcript=""):
    return VisitBundle("synthetic-problems", 40, "F", "office_established", "11",
                       signed_note=note, transcript=transcript)


def event(name, status, action=None, *, source="signed_note"):
    return {"name": name, "source": source, "identity_quote": status,
            "status_quote": status, "addressed_quote": action or status}


def qualify(*events, note=None):
    if note is None:
        note = "\n\n".join(" ".join(dict.fromkeys(e[k] for k in
            ("identity_quote", "addressed_quote", "status_quote"))) for e in events)
    return qualify_problems(list(events), bundle(note))


@pytest.mark.parametrize("name,status,category,level", [
    ("insect bite", "Insect bite: self-limited problem; I evaluated it and discussed care.", "self_limited", "straightforward"),
    ("ankle sprain", "Ankle sprain: acute uncomplicated injury; I evaluated it and started supportive treatment.", "acute_uncomplicated", "low"),
    ("cystitis", "Cystitis: acute uncomplicated illness requiring observation care; I evaluated it and continued treatment.", "acute_uncomplicated", "low"),
    ("pneumonia", "Pneumonia: stable acute illness responding to treatment; I evaluated it and continued therapy.", "stable_acute", "low"),
    ("hypertension", "Hypertension is at individualized goal; continue the current treatment.", "stable_chronic", "low"),
    ("asthma", "Asthma is in moderate exacerbation; I evaluated it and adjusted treatment.", "chronic_exacerbation", "moderate"),
    ("abdominal mass", "Abdominal mass: undiagnosed problem with uncertain prognosis; differential includes cancer with high morbidity if untreated; I evaluated it and ordered imaging.", "undiagnosed_uncertain_prognosis", "moderate"),
    ("pyelonephritis", "Pyelonephritis: acute illness with systemic symptoms and high risk of morbidity without treatment; I evaluated it and started antibiotics.", "acute_systemic", "moderate"),
    ("hand laceration", "Hand laceration: complicated injury; I evaluated it and managed tendon damage.", "complicated_injury", "moderate"),
    ("asthma", "Asthma is in severe exacerbation requiring hospitalization; I evaluated it and started urgent treatment.", "severe_chronic", "high"),
    ("chest pain", "Chest pain poses a current threat to life; I evaluated it and arranged emergency assessment.", "threat_to_life_or_function", "high"),
])
def test_complete_problem_grid(name, status, category, level):
    result = qualify(event(name, status))
    assert result.level == level
    assert result.public["events"][0]["category"] == category
    assert result.public["events"][0]["status"] == "counted"
    assert result.established and result.has_addressed and result.addressed_count == 1


def test_independent_counts_use_one_underlying_condition_and_not_severity_labels():
    first = event("hypertension", "Hypertension is well controlled; continue medication.")
    second = event("hypothyroidism", "Hypothyroidism is at goal; continue replacement therapy.")
    assert qualify(first, second).level == "moderate"
    minor1 = event("insect bite", "Insect bite: minor problem; I evaluated it and discussed care.")
    minor2 = event("abrasion", "Abrasion: self-limited injury; I evaluated it and managed wound care.")
    assert qualify(minor1, minor2).level == "low"
    dup = {**first, "name": "HTN", "severity": "high", "new": True}
    result = qualify(first, dup, note=first["identity_quote"])
    assert result.level == "low"
    assert [e["status"] for e in result.public["events"]] == ["counted", "duplicate"]
    assert result.addressed_count == 1


@pytest.mark.parametrize("name,text,category", [
    ("cystitis", "Acute uncomplicated cystitis; I evaluated it and started treatment.", "acute_uncomplicated"),
    ("dermatitis", "Self-limited dermatitis; I evaluated it and discussed care.", "self_limited"),
    ("asthma", "Moderate exacerbation of asthma; I evaluated it and adjusted treatment.", "chronic_exacerbation"),
    ("type 2 diabetes", "Type 2 diabetes with glycemia above the individualized goal despite initial lifestyle changes; start metformin.", "chronic_exacerbation"),
    ("hypothyroidism", "The patient has hypothyroidism at goal; continue replacement therapy.", "stable_chronic"),
    ("hypertension", "Assessment is hypertension well controlled; continue therapy.", "stable_chronic"),
    ("asthma", "Asthma with side effects from treatment; I evaluated it and adjusted therapy.", "chronic_exacerbation"),
])
def test_common_source_narrative_variants(name, text, category):
    q = qualify(event(name, text))
    assert q.public["events"][0]["category"] == category
    assert q.established


@pytest.mark.parametrize("status", [
    "Hypertension is unchanged; continue medication.",
    "Hypertension is new; I evaluated it and started therapy.",
    "Hypertension has high complexity; I evaluated it and continued therapy.",
    "Hypertension has uncertain cause; I evaluated it and ordered testing.",
    "Hypertension is stable but not at goal; I evaluated it and adjusted therapy.",
])
def test_addressed_does_not_invent_status_or_turn_new_into_moderate(status):
    raw = event("hypertension", status)
    raw.update(new=True, chronic=True, severity="high", status="severe", level="high")
    result = qualify(raw)
    assert result.has_addressed and not result.established
    assert result.level == "straightforward" and not result.evidence()
    assert result.public["events"][0]["status"] == "held"


@pytest.mark.parametrize("status", [
    "Patient reports: Hypertension is at goal; continue therapy.",
    "Family history: Hypertension is at goal; continue therapy.",
    "Mother's hypertension is at goal; continue therapy.",
    "Example: Hypertension is at goal; continue therapy.",
    "At the prior encounter hypertension was at goal; continue therapy.",
    "No hypertension is present; continue therapy.",
    "Possible hypertension is at goal; continue therapy.",
    "Hypertension is at goal; it was not addressed today.",
    "Hypertension is at goal; the nurse continued therapy.",
    "Hypertension is at goal; I will evaluate it tomorrow.",
    "Hypertension is at goal; continue therapy?",
    "Hypertension is at goal; no treatment decision or evaluation is recorded.",
])
def test_noncurrent_nonclinician_hypothetical_or_unperformed_work_is_held(status):
    result = qualify(event("hypertension", status))
    assert not result.has_addressed and not result.established and not result.evidence()


def test_exact_quotes_cannot_clip_negation_actor_or_omitted_correction():
    raw = event("hypertension", "Hypertension is at goal; continue therapy.")
    for prefix in ("Patient reports: ", "Family says: ", "Example: ", "No "):
        assert not qualify(raw, note=prefix + raw["identity_quote"]).has_addressed
    correction = raw["identity_quote"] + " Correction: this assessment is incorrect."
    q = qualify(raw, note=correction)
    assert not q.established and not q.evidence()


def test_wrong_problem_or_unrelated_work_and_status_cannot_be_borrowed():
    identity = "Hypertension is present."
    other = "Asthma is in severe exacerbation requiring hospitalization; I treated it today."
    raw = event("hypertension", identity, other)
    raw["status_quote"] = other
    result = qualify(raw, note=identity + "\n\n" + other)
    assert not result.has_addressed and not result.established
    raw = event("hypertension", "Hypertension is stable; asthma is in severe exacerbation requiring hospitalization; I treated asthma.")
    assert not qualify(raw).established
    raw = event("hypertension", "Hypertension is stable; I continued treatment for asthma.")
    assert not qualify(raw).has_addressed


def test_adjacent_condition_management_and_pronoun_are_supported():
    identity = "Hypertension is at goal."
    action = "Continue the current medication after reviewing its effect."
    result = qualify(event("hypertension", identity, action))
    assert result.level == "low"
    assert [e.quote for e in result.evidence()] == [identity, action]
    distant = identity + "\n\n" + action
    assert not qualify(event("hypertension", identity, action), note=distant).has_addressed


def test_identity_missing_ambiguous_forged_or_intake_only_cannot_qualify():
    quote = "Hypertension is controlled; continue therapy."
    raw = event("hypertension", quote)
    for change, note in [({"identity_quote": "Hypertension is well controlled."}, quote),
                         ({"name": "hypothyroidism"}, quote), ({}, quote * 2),
                         ({"source": "intake_text"}, quote), ({"source": []}, quote)]:
        result = qualify({**raw, **change}, note=note)
        assert not result.has_addressed and not result.established


@pytest.mark.parametrize("prefix,allowed", [("Physician: ", True), ("QHP: ", True),
                                           ("Patient: ", False), ("", False)])
def test_transcript_requires_an_explicit_clinician_speaker(prefix, allowed):
    quote = prefix + "Hypertension is at goal; continue therapy."
    raw = [event("hypertension", quote, source="transcript")]
    q = qualify_problems(raw, bundle(transcript=quote))
    assert q.established is allowed


@pytest.mark.parametrize("heading", ["Family history", "FHx", "Past medical history", "Patient report", "Template"])
def test_untrusted_heading_survives_quote_clipping(heading):
    quote = "Hypertension is stable; continue therapy."
    assert not qualify(event("hypertension", quote), note=heading + ":\n" + quote).established


@pytest.mark.parametrize("name,status", [
    ("viral infection", "Viral infection is acute with fever and fatigue; I evaluated it and recommended fluids."),
    ("mass", "Mass is new and undiagnosed; I evaluated it and ordered ultrasound."),
    ("mass", "Mass has uncertain prognosis; I evaluated it and ordered ultrasound."),
    ("asthma", "Asthma is in severe exacerbation; I evaluated it and started treatment."),
    ("chest pain", "Chest pain poses a threat to life if it later worsens; I evaluated it and managed symptoms."),
])
def test_systemic_uncertain_and_high_categories_require_actual_consequences(name, status):
    assert not qualify(event(name, status)).established


def test_undiagnosed_danger_does_not_require_available_billable_diagnosis():
    class ForbiddenLookup:
        def resolve(self, *args, **kwargs):
            raise AssertionError("Problem complexity must not depend on ICD availability")
    text = "Abdominal mass: uncertain prognosis with differential including malignancy and likely morbidity if untreated; I evaluated it and ordered imaging."
    q = qualify_problems([event("abdominal mass", text)], bundle(text), ForbiddenLookup())
    assert q.level == "moderate"


def test_explicit_symptom_relationship_collapses_count_without_inference():
    underlying = event("asthma", "Asthma is well controlled; continue treatment.")
    symptom = event("cough", "Cough: acute uncomplicated problem; I evaluated it and managed symptoms.")
    note = underlying["identity_quote"] + "\n\n" + symptom["identity_quote"] + "\nCough is a symptom of asthma."
    q = qualify(underlying, symptom, note=note)
    assert q.level == "low"
    assert q.public["events"][1]["status"] == "duplicate"
    assert q.addressed_count == 1
    assert all("Cough" not in e.quote for e in q.evidence())
    uncertain = note.replace("Cough is a symptom of asthma.", "Cough may be due to asthma.")
    q = qualify(underlying, symptom, note=uncertain)
    assert q.public["events"][1]["status"] == "held"


def test_contradictory_current_assessments_are_not_best_case_selected():
    first = event("asthma", "Asthma is well controlled; continue treatment.")
    second = "Asthma is in moderate exacerbation; I adjusted therapy."
    q = qualify(first, note=first["identity_quote"] + "\n" + second)
    assert q.has_addressed and not q.established
    assert "Conflicting" in q.public["events"][0]["reason"]
    q = qualify(first, note=second + "\n" + first["identity_quote"])
    assert not q.established


def test_serious_presenting_problem_not_erased_by_a_benign_cause():
    presenting = event("chest pain", "Chest pain poses a current threat to life; I evaluated it and arranged urgent testing.")
    cause = event("esophageal spasm", "Esophageal spasm: acute uncomplicated illness; I evaluated it and managed symptoms.")
    note = presenting["identity_quote"] + "\n\n" + cause["identity_quote"] + "\nChest pain is due to esophageal spasm."
    q = qualify(presenting, cause, note=note)
    assert q.level == "high" and q.addressed_count == 1
    assert [e["status"] for e in q.public["events"]] == ["counted", "duplicate"]


def test_unattributed_transcript_relationship_cannot_override_clinician_evidence():
    a = event("hypertension", "Hypertension is controlled; continue therapy.")
    b = event("hypothyroidism", "Hypothyroidism is at goal; continue therapy.")
    note = a["identity_quote"] + "\n\n" + b["identity_quote"]
    q = qualify_problems([a, b], bundle(note, transcript="Hypertension is due to hypothyroidism."))
    assert q.level == "moderate" and q.addressed_count == 2


@pytest.mark.parametrize("name,text", [
    ("asthma", "Asthma is in severe exacerbation without organ damage and not requiring hospitalization; I evaluated it and started treatment."),
    ("viral infection", "Viral infection: acute illness with systemic symptoms but no high risk of morbidity without treatment; I evaluated it and recommended fluids."),
    ("abdominal mass", "Abdominal mass: uncertain prognosis with differential including malignancy but no high morbidity if untreated; I evaluated it and ordered imaging."),
    ("chest pain", "Chest pain: life threatening is denied; I evaluated it and managed symptoms."),
    ("hypertension", "Hypertension is stable but not at the individualized goal; I evaluated it and continued treatment."),
    ("hypertension", "Hypertension is stable but treatment goals are not met; I evaluated it and continued treatment."),
    ("hypertension", "Hypertension is stable but above the individualized goal; I evaluated it and continued treatment."),
    ("hypertension", "Hypertension is stable but goals remain unmet; I evaluated it and continued treatment."),
    ("hypertension", "HTN is stable according to the patient; I evaluated it and continued medication."),
    ("hypertension", "HTN is stable in the 2020 note; I evaluated it today."),
    ("cystitis", "Cystitis is stable with chronic headaches also present; I evaluated it and continued treatment."),
])
def test_review_regressions_status_is_owned_affirmative_and_current(name, text):
    q = qualify(event(name, text))
    assert not q.established and not q.evidence()


@pytest.mark.parametrize("text", [
    "Hypertension is stable; I started levothyroxine for hypothyroidism.",
    "Hypertension is stable; I ordered a radiograph for ankle pain.",
    "Hypertension is stable; I might continue medication after a future evaluation.",
    "Hypertension is stable; I did not review control or start treatment.",
    "Hypertension is stable; I did not review control, start treatment, or continue medication.",
    "Hypertension is stable; I will continue medication after a future evaluation.",
    "Hypertension is stable; I started levothyroxine to treat hypothyroidism.",
    "Hypertension is stable; I reviewed control of hypothyroidism.",
    "Hypertension is stable; I counseled about treatment of hypothyroidism.",
])
def test_review_regressions_actual_management_belongs_to_this_problem(text):
    q = qualify(event("hypertension", text))
    assert not q.has_addressed and not q.established


@pytest.mark.parametrize("action", [
    "I started lisinopril to treat hypertension.",
    "I reviewed control of hypertension.",
    "I counseled about treatment of hypertension.",
])
def test_explicit_current_management_destinations_can_bind_this_problem(action):
    identity = "Hypertension is stable."
    assert qualify(event("hypertension", identity, action)).level == "low"


def test_named_but_excluded_destination_cannot_own_current_work():
    identity = "Hypertension is stable."
    action = "I reviewed control of hypothyroidism, not hypertension."
    q = qualify(event("hypertension", identity, action))
    assert not q.has_addressed and not q.established and not q.evidence()


@pytest.mark.parametrize("name", ["asthma", "hypertension"])
def test_chronic_wrapper_cannot_double_count_same_condition(name):
    text = f"Chronic {name} is stable; continue medication."
    q = qualify(event(name, text), event("chronic " + name, text), note=text)
    assert q.level == "low" and q.addressed_count == 1
    assert [e["status"] for e in q.public["events"]] == ["counted", "duplicate"]


def test_independently_managed_causal_comorbid_diseases_remain_two_problems():
    a = event("hypertension", "Hypertension is at goal; continue medication.")
    b = event("chronic kidney disease stage 3", "Chronic kidney disease stage 3 is stable; I evaluated it and continued treatment.")
    note = a["identity_quote"] + "\n\n" + b["identity_quote"] + "\nHypertension is due to chronic kidney disease stage 3."
    q = qualify(a, b, note=note)
    assert q.level == "moderate" and q.addressed_count == 2


@pytest.mark.parametrize("heading,other", [
    ("Family history", "Asthma is in moderate exacerbation."),
    ("Template", "Asthma is not present."),
])
def test_untrusted_other_sections_do_not_create_current_status_conflict(heading, other):
    own = "Asthma is well controlled; continue medication."
    q = qualify(event("asthma", own), note=own + "\n\n" + heading + ":\n" + other)
    assert q.level == "low" and q.established


@pytest.mark.parametrize("name,identity,action", [
    ("viral infection", "Viral infection is present.", "Recommend fluids and rest."),
    ("migraine", "Chronic migraine is present.", "After shared discussion, start preventive therapy."),
    ("constipation", "Constipation is present.", "Counsel about dietary fiber and hydration."),
    ("diarrhea", "Diarrhea is present.", "Monitor symptoms and response."),
])
def test_current_condition_owned_care_without_a_complexity_label(name, identity, action):
    q = qualify(event(name, identity, action))
    assert q.has_addressed and not q.established


def test_normal_ap_fragments_and_owned_continuation_block():
    text = "Assessment and plan: (1) Constipation - dietary fiber, hydration; (2) Diarrhea - monitor."
    q = qualify(event("constipation", text), event("diarrhea", text), note=text)
    assert q.addressed_count == 2 and not q.established
    identity = "Assessment is newly recognized type 2 diabetes."
    status = "Current control is above the preconception goal."
    action = "Begin metformin today."
    raw = event("type 2 diabetes", identity, action)
    raw["status_quote"] = status
    q = qualify(raw, note=" ".join((identity, status, action)))
    assert q.has_addressed and q.level == "moderate"
    foreign = "Hypothyroidism is also present."
    q = qualify(raw, note=" ".join((identity, status, foreign, action)))
    assert not q.has_addressed


def test_public_contract_private_identity_and_source_are_immutable():
    text = "Hypertension is controlled; continue treatment."
    raw = [event("hypertension", text)]
    q = qualify_problems(raw, bundle(text))
    assert q.matches(deepcopy(raw))
    assert json.loads(json.dumps(q.public)) == {
        "rule_version": VERSION, "level": "low", "events": [{
            "id": "mdm-problem-1", "subject": "hypertension", "status": "counted",
            "category": "stable_chronic",
            "reason": "Current clinician work and condition-specific complexity are documented.",
            "evidence": [{"source": "signed_note", "quote": text}]}]}
    public = q.public
    public["events"][0]["status"] = "held"
    assert q.established
    assert q.evidence()[0].rationale == "MDM problems: mdm-problem-1"
    raw[0]["status_quote"] = "altered"
    assert not q.matches(raw)
    assert q.source_sha256 != qualify_problems([], bundle(text + " ")).source_sha256
    with pytest.raises(FrozenInstanceError):
        q.events = ()


@pytest.mark.parametrize("raw", [None, "problems", {}, [None], [{"name": []}],
                               [{"name": "x\ud800"}], [{}] * 21])
def test_malformed_bounded_extraction_is_reviewable_and_cannot_count(raw):
    q = qualify_problems(raw, bundle())
    assert not q.established and not q.has_addressed and not q.evidence()
    json.dumps(q.public, allow_nan=False)


@pytest.mark.parametrize("name,identity,action", [
    ("migraine without aura", "Chronic migraine without aura, with frequent disabling episodes; current intractability and status migrainosus are not established.",
     "After shared discussion, start propranolol 20 mg twice daily for prevention."),
    ("postinfectious cough", "Improving postinfectious cough following a resolved respiratory illness.",
     "Recommend fluids, warm beverages and avoiding smoke or other irritants; a simple over-the-counter soothing preparation may be used if needed."),
    ("right lateral ankle sprain", "Right lateral ankle sprain in routine healing after initial emergency care.",
     "Continue the already supplied brace for comfort while walking, begin pain-limited range of motion and then balance exercises, and gradually increase activity."),
    ("type 2 diabetes", "Assessment is newly recognized type 2 diabetes.",
     "Begin metformin 500 mg with the evening meal for seven days, then 500 mg twice daily if tolerated."),
    ("right knee osteoarthritis", "Symptomatic right knee osteoarthritis without evidence of septic arthritis or acute fracture.",
     "Start topical diclofenac gel to the right knee as needed, following label limits."),
    ("cystitis", "Acute cystitis without hematuria is present.",
     "Begin the selected antibiotic for five days after specimen collection."),
])
def test_ordinary_current_care_is_not_erased_by_other_object_or_later_step(name, identity, action):
    q = qualify(event(name, identity, action))
    assert q.has_addressed
    assert q.supports_addressed(q.addressed_evidence())
    assert all(e.quote in identity + " " + action for e in q.addressed_evidence())


def test_current_care_survives_explicitly_excluded_alternative_between_anchors():
    identity = "Improving postinfectious cough following a resolved respiratory illness."
    exclusion = "The current history and examination do not establish pneumonia, asthma, a new bacterial infection or continuing active bronchitis."
    action = "Discussed the usual gradual resolution of cough after an infection. Recommend fluids and avoiding smoke."
    q = qualify(event("postinfectious cough", identity, action), note=" ".join((identity, exclusion, action)))
    assert q.has_addressed and not q.established
    assert [e.quote for e in q.addressed_evidence()] == [identity, action]
    assert q.public["events"][0]["reason"].startswith("Current clinical work is established; problem complexity remains held")
    for foreign in ("Pneumonia is also present.", "There is no asthma, but pneumonia is present."):
        assert not qualify(event("postinfectious cough", identity, action), note=" ".join((identity, foreign, action))).has_addressed


@pytest.mark.parametrize("name,identity,action", [
    ("migraine with aura", "Migraine without aura is present.", "Start preventive treatment."),
    ("intractable migraine", "Migraine is present; intractability is not established.", "Start preventive treatment."),
    ("type 1 diabetes", "Type 2 diabetes is present.", "Begin metformin today."),
    ("left ankle sprain", "Right ankle sprain is present.", "Continue the brace."),
    ("cough", "Cough has resolved after the respiratory illness.", "Recommend fluids."),
    ("asthma", "Asthma remains suspected.", "Begin treatment."),
    ("hypertension", "Hypertension is present.", "If readings increase, begin medication."),
    ("hypertension", "Hypertension is present.", "Begin medication if readings increase."),
    ("hypertension", "Hypertension is present.", "The nurse started medication."),
    ("hypertension", "Hypertension is present.", "Patient reports: I started medication."),
    ("hypertension", "Hypertension is present.", "Start medication for hypothyroidism, then adjust if needed."),
    ("hypertension", "Hypertension is present.", "I reviewed control of hypothyroidism, not hypertension."),
])
def test_care_proof_preserves_identity_uncertainty_conditionality_actor_and_destination(name, identity, action):
    q = qualify(event(name, identity, action))
    assert not q.has_addressed and not q.addressed_evidence() and not q.supports_addressed([])


def test_care_groups_require_complete_exact_role_bound_proof_without_status_credit():
    identity = "Migraine without aura is present."
    action = "Start preventive treatment."
    status = "Current control is unknown."
    raw = event("migraine without aura", identity, action)
    raw["status_quote"] = status
    q = qualify(raw, note=" ".join((identity, action, status)))
    assert q.has_addressed and not q.established and not q.evidence()
    group, = q.addressed_evidence_groups()
    assert [e.quote for e in group] == [identity, action]
    assert {e.rationale for e in group} == {"Current clinical work: mdm-problem-1"}
    assert q.supports_addressed(group)
    assert not q.supports_addressed(group[:1]) and not q.supports_addressed(group[1:])
    for changes in ({"quote": action.lower()}, {"rationale": "MDM problems: mdm-problem-1"}, {"source": "transcript"}):
        values = {"source": group[1].source, "quote": group[1].quote, "rationale": group[1].rationale, **changes}
        assert not q.supports_addressed([group[0], Evidence(**values)])
    assert not q.supports_addressed([group[0], {"source": group[1].source, "quote": action, "rationale": group[1].rationale}])
    assert not q.supports_addressed(None)
    public = q.public
    assert "care_evidence" not in public["events"][0]
    assert json.loads(json.dumps(public))["events"][0]["status"] == "held"
    group.clear()
    assert q.supports_addressed(q.addressed_evidence())  # Returned collections cannot mutate the proof.


def test_counted_care_reuses_existing_evidence_and_dedup_does_not_multiply_proofs():
    raw = event("hypertension", "Hypertension is controlled; continue therapy.")
    q = qualify(raw, {**raw, "name": "HTN"}, note=raw["identity_quote"])
    assert q.addressed_evidence() == q.evidence()
    assert len(q.addressed_evidence_groups()) == 1 and q.supports_addressed(q.evidence())
    other = event("migraine", "Migraine is present.", "Start preventive treatment.")
    held = qualify(other)
    assert not held.supports_addressed(q.evidence())


@pytest.mark.parametrize("later", [
    "Correction: that treatment was not started today.",
    "Hypertension was not addressed during this encounter.",
    "Hypertension is not present.",
])
def test_care_only_proof_does_not_survive_current_correction_or_contradiction(later):
    raw = event("hypertension", "Hypertension is present.", "Start medication today.")
    q = qualify(raw, note=" ".join((raw["identity_quote"], raw["addressed_quote"], later)))
    assert not q.has_addressed and not q.addressed_evidence()


def test_another_person_or_historical_noncare_statement_does_not_revoke_own_work():
    raw = event("hypertension", "Hypertension is present.", "Start medication today.")
    for unrelated in ("Mother's hypertension was not addressed.", "At the prior encounter hypertension was not addressed."):
        q = qualify(raw, note=" ".join((raw["identity_quote"], raw["addressed_quote"], unrelated)))
        assert q.has_addressed and q.supports_addressed(q.addressed_evidence())


def test_correction_inside_complete_action_quote_cannot_be_ignored():
    raw = event("hypertension", "Hypertension is present.",
                "Start medication today; correction: that treatment was not started today.")
    q = qualify(raw)
    assert not q.has_addressed and not q.addressed_evidence()


def test_other_resolved_illness_is_not_a_status_contradiction_for_current_condition():
    raw = event("asthma", "Asthma is controlled; continue the inhaler.")
    note = raw["identity_quote"] + " Asthma treatment continues following a resolved cold."
    q = qualify(raw, note=note)
    assert q.level == "low" and q.established


def test_completed_diagnostic_work_is_care_without_a_drug_or_resolved_test_identity():
    identity = "Assessment and plan: Acute cystitis presentation in a patient with recurrent urinary infections, without hematuria or features of pyelonephritis."
    action = "Obtain a urine specimen for external urinalysis and culture before the first antibiotic dose; the tests have been ordered but results are not yet available."
    q = qualify(event("acute cystitis", identity, action))
    assert q.has_addressed and not q.established and q.level == "straightforward"
    assert q.evidence() == []
    assert [e.quote for e in q.addressed_evidence()] == [identity, action]
    assert q.supports_addressed(q.addressed_evidence())
    assert not q.supports_addressed(q.addressed_evidence()[1:])
    # A different specimen/test role is supported without adding code aliases.
    q = qualify(event("neck mass", "Neck mass is present.",
        "Obtain a tissue specimen for cytology; the tests were ordered but results are pending."))
    assert q.has_addressed and not q.established


def test_background_recurrent_condition_is_not_a_second_owner_of_current_acute_work():
    identity = "Acute cystitis presentation in a patient with recurrent urinary infections, without hematuria."
    action = "Obtain a urine specimen for external urinalysis and culture; the tests have been ordered but results are pending."
    background = event("recurrent urinary infections", identity, "Discuss prevention at a future visit.")
    acute = event("acute cystitis", identity, action)
    note = " ".join((identity, action, background["addressed_quote"]))
    q = qualify(acute, background, note=note)
    assert q.has_addressed and q.addressed_count == 1
    assert q.supports_addressed(q.addressed_evidence())
    assert all(e.rationale.endswith("mdm-problem-1") for e in q.addressed_evidence())
    # A separate affirmative condition assessment breaks inherited ownership.
    note = " ".join((identity, "Recurrent urinary infections also require current evaluation.", action))
    assert not qualify(acute, background, note=note).has_addressed


@pytest.mark.parametrize("action", [
    "Obtain a urine specimen for external urinalysis and culture if symptoms persist; the tests will be ordered tomorrow.",
    "The nurse obtained a urine specimen for external urinalysis and culture; the tests have been ordered.",
    "Patient reports: obtain a urine specimen for external urinalysis and culture; the tests have been ordered.",
    "Obtain a urine specimen for hypothyroidism; the tests have been ordered.",
    "Obtain a urine specimen for culture to evaluate hypothyroidism; the tests have been ordered.",
    "Obtain a urine specimen for culture; no tests have been ordered.",
    "Obtain a urine specimen for culture; the tests have not been ordered.",
    "Obtain a urine specimen for culture; the tests have been ordered if symptoms persist.",
])
def test_diagnostic_care_does_not_borrow_actor_future_unperformed_or_foreign_work(action):
    q = qualify(event("cystitis", "Cystitis is present.", action))
    assert not q.has_addressed and not q.addressed_evidence()


@pytest.mark.parametrize("completion", [
    "the tests have been ordered for hypothyroidism.",
    "the tests have been ordered to evaluate hypothyroidism.",
    "the tests have been ordered by the nurse, not by me.",
    "the tests have been ordered by another clinician.",
])
def test_completed_marker_cannot_borrow_a_foreign_indication_or_actor(completion):
    q = qualify(event("cystitis", "Cystitis is present.",
                      "Obtain a urine specimen for culture; " + completion))
    assert not q.has_addressed and not q.supports_addressed(q.addressed_evidence())


@pytest.mark.parametrize("completion", [
    "the tests have been ordered to evaluate cystitis.",
    "the tests have been ordered for cystitis.",
    "the tests have been ordered today by me to evaluate cystitis.",
    "the tests have been ordered to evaluate cystitis, but results are pending.",
])
def test_completed_diagnostic_marker_can_explicitly_bind_same_condition(completion):
    identity = "Cystitis is present."
    action = "Obtain a urine specimen for culture; " + completion
    q = qualify(event("cystitis", identity, action))
    assert q.has_addressed and not q.established
    assert [e.quote for e in q.addressed_evidence()] == [identity, action]
    assert q.supports_addressed(q.addressed_evidence())


@pytest.mark.parametrize("later,expected", [
    ("I did not address hypertension during this encounter.", False),
    ("I did not evaluate hypertension today.", False),
    ("I did not address hypothyroidism during this encounter.", True),
    ("At the prior encounter I did not address hypertension.", True),
    ("Mother reports: I did not address hypertension.", True),
])
def test_active_denial_preserves_current_actor_and_condition_ownership(later, expected):
    raw = event("hypertension", "Hypertension is present.", "Start medication today.")
    q = qualify(raw, note=" ".join((raw["identity_quote"], raw["addressed_quote"], later)))
    assert q.has_addressed is expected
    assert q.supports_addressed(q.addressed_evidence()) is expected
