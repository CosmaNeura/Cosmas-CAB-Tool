"""Management decisions, not names or model risk labels, qualify MDM risk."""
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from cosmaneura_billing.mdm_risk import ACTIONS, QualifiedRisk, qualify_risk
from cosmaneura_billing.schemas import VisitBundle


def bundle(note, **kwargs):
    return VisitBundle("SYN-MDM-RISK", 44, "F", "office_established", "11",
                       signed_note=note, **kwargs)


def event(subject, quote, action="medication_management", decision="", qualification="", **kwargs):
    return dict(action=action, subject=subject, source="signed_note", action_quote=quote,
                decision_quote=decision, qualification_quote=qualification, **kwargs)


def run(*events, note=None):
    note = note if note is not None else "\n".join(dict.fromkeys(
        e[k] for e in events for k in ("action_quote", "decision_quote", "qualification_quote") if e.get(k)))
    return qualify_risk(list(events), bundle(note))


@pytest.mark.parametrize("subject,quote", [
    ("metformin", "I started metformin 500 mg twice daily today for diabetes."),
    ("levothyroxine", "After reviewing adherence and administration, continue levothyroxine 75 micrograms daily rather than increasing the dose today."),
    ("sertraline", "I reviewed response and tolerability and will continue sertraline 50 mg daily."),
    ("losartan", "I reduced losartan from 50 mg to 25 mg today because of dizziness."),
    ("lisinopril", "I stopped lisinopril today because of angioedema."),
    ("letrozole", "I decided to start letrozole 2.5 mg daily for five days."),
    ("warfarin", "I avoided warfarin today because of bleeding."),
    ("amoxicillin", "If the culture is positive, start amoxicillin 500 mg three times daily for seven days."),
])
def test_actual_prescription_management(subject, quote):
    q = run(event(subject, quote))
    assert (q.level, q.established) == ("moderate", True)
    assert q.public["events"][0]["category"] == "prescription_management"
    assert q.evidence()[0].quote == quote
    assert q.evidence()[0].rationale == "MDM risk: mdm-risk-01"


@pytest.mark.parametrize("quote", [
    "Medications: metformin 500 mg twice daily.",
    "The patient requests a metformin refill.",
    "I refilled metformin today.",
    "Continue metformin 500 mg daily.",
    "I reviewed the medication list and continued metformin 500 mg daily.",
    "I did not start metformin today.",
    "I may start metformin next month.",
    "If A1c rises, consider starting metformin.",
    "The patient reports I started metformin today.",
    "The endocrinologist started metformin yesterday.",
    "I started my mother's metformin today.",
    "Example: I started metformin today.",
    "I started metformin today?",
    "Metformin remains on the list; I started levothyroxine today.",
])
def test_list_mentions_unperformed_and_wrong_actor_subject_hold(quote):
    q = run(event("metformin", quote))
    assert not q.established
    assert q.level == "straightforward"
    assert not q.evidence()


def test_name_prefix_and_unknown_formulation_do_not_resolve():
    for subject, quote in [("metformin", "I started metformin herbal blend today."),
                           ("insulin", "I started insulin today."),
                           ("diclofenac", "I started diclofenac 1% gel today.")]:
        assert not run(event(subject, quote)).established
    q = "I started prescription-only synthetic medicine 10 mg daily today."
    assert run(event("synthetic medicine", q)).level == "moderate"
    assert run(event("synthetic medicine", "I prescribed synthetic medicine 10 mg daily today.")).level == "straightforward"


@pytest.mark.parametrize("subject,quote", [
    ("acetaminophen", "I started acetaminophen 500 mg as needed today."),
    ("topical diclofenac 1% gel", "I started topical diclofenac 1% gel today; topical diclofenac 1% gel is over-the-counter."),
])
def test_active_otc_management_is_not_automatically_prescription(subject, quote):
    q = run(event(subject, quote))
    assert q.level == "low"
    assert q.public["events"][0]["category"] == "nonprescription_management"


@pytest.mark.parametrize("risk,expected", [
    ("For this patient, ibuprofen has moderate risk of bleeding because of prior GI bleeding.", "moderate"),
    ("For this patient, ibuprofen has high risk of life-threatening bleeding because of anticoagulation.", "high"),
])
def test_otc_risk_can_be_patient_specific(risk, expected):
    q = run(event("ibuprofen", "I avoided ibuprofen today because of bleeding.", qualification=risk))
    assert q.level == expected
    assert len(q.evidence()) == 2


def test_risk_adjective_or_another_drugs_context_cannot_authorize_high():
    act = "I started acetaminophen 500 mg today."
    for qual in ("Acetaminophen is high risk.",
                 "For this patient, warfarin has high risk of life-threatening bleeding because of anticoagulation."):
        assert run(event("acetaminophen", act, qualification=qual)).level != "high"


def test_clipped_negation_correction_and_report_heading_are_preserved():
    raw = event("metformin", "start metformin today")
    q = run(raw, note="I did not start metformin today.")
    assert not q.established
    assert q.public["events"][0]["evidence"][0]["quote"] == "I did not start metformin today."
    raw = event("metformin", "I started metformin today.")
    for note in (raw["action_quote"] + " Correction: this plan was canceled.",
                 "Patient report:\n" + raw["action_quote"],
                 "History:\n" + raw["action_quote"]):
        assert not run(raw, note=note).established


def test_transcript_requires_explicit_clinician_speaker_and_current_act():
    for prefix, expected in [("Physician: ", True), ("Patient: ", False), ("", False)]:
        quote = prefix + "I started metformin today."
        raw = event("metformin", quote)
        raw["source"] = "transcript"
        q = qualify_risk([raw], bundle("", transcript=quote))
        assert q.established == expected


def test_exact_source_and_unique_occurrence_required():
    e = event("metformin", "I started metformin today.")
    for note in ("I started metformin yesterday.", e["action_quote"] * 2):
        assert not run(e, note=note).established
    assert not run({**e, "source": "intake_text"}).established


TOXICITY = "I ordered CBC weekly to monitor for methotrexate-induced myelosuppression."


def test_intensive_toxicity_monitoring_requires_agent_harm_method_frequency():
    q = run(event("methotrexate", "I started methotrexate 10 mg weekly today.",
                  "toxicity_monitoring", qualification=TOXICITY))
    assert q.level == "high"
    assert q.public["events"][0]["category"] == "intensive_toxicity_monitoring"


@pytest.mark.parametrize("monitor", [
    "I ordered A1c quarterly to monitor metformin efficacy.",
    "I ordered CBC annually to monitor for methotrexate-induced myelosuppression.",
    "I ordered CBC to monitor for methotrexate-induced myelosuppression.",
    "I reviewed history and examination weekly to monitor for methotrexate-induced myelosuppression.",
    "I ordered CBC weekly to monitor for azathioprine-induced myelosuppression.",
    "The patient reports I ordered CBC weekly to monitor for methotrexate-induced myelosuppression.",
    "I did not order CBC weekly to monitor for methotrexate-induced myelosuppression.",
])
def test_routine_efficacy_unrelated_or_unperformed_monitoring_is_held(monitor):
    assert not run(event("methotrexate", "I started methotrexate 10 mg weekly today.",
                         "toxicity_monitoring", qualification=monitor)).established


@pytest.mark.parametrize("route,level", [("IV", "high"), ("intramuscularly", "high"), ("orally", "moderate")])
def test_parenteral_controlled_medication_requires_current_route(route, level):
    assert run(event("morphine", f"I started morphine 2 mg {route} today for pain.")).level == level


@pytest.mark.parametrize("qual,level,category", [
    ("Skin excision is a minor surgery without identified patient or procedure risk factors.", "low", "minor_surgery"),
    ("Skin excision is a minor surgery with increased risk of bleeding because of anticoagulation.", "moderate", "minor_surgery_with_risks"),
    ("Skin excision is an elective major surgery without identified risk factors.", "moderate", "elective_major_surgery"),
    ("Skin excision is an elective major surgery with increased risk of bleeding because of anticoagulation.", "high", "elective_major_surgery_with_risks"),
    ("Skin excision is an emergency major surgery.", "high", "emergency_major_surgery"),
])
def test_surgery_uses_clinician_characterization_not_procedure_name(qual, level, category):
    q = run(event("skin excision", "I recommended skin excision today.", "surgery_decision", qualification=qual))
    assert (q.level, q.public["events"][0]["category"]) == (level, category)


@pytest.mark.parametrize("qual", [
    "Colonoscopy is invasive.",
    "Colonoscopy is high risk.",
    "Colonoscopy is a minor surgery with risks discussed.",
    "Colonoscopy is an elective major surgery with risk because of age 70.",
    "Colonoscopy is not an emergency major surgery.",
])
def test_invasiveness_generic_risk_age_or_negated_type_cannot_level(qual):
    assert not run(event("colonoscopy", "I recommended colonoscopy today.", "surgery_decision", qualification=qual)).established


def test_surgical_referral_is_not_a_decision_to_operate():
    assert not run(event("hysterectomy", "I referred the patient to discuss hysterectomy.", "surgery_decision",
                         qualification="Hysterectomy is an elective major surgery without identified risk factors.")).established


@pytest.mark.parametrize("quote", [
    "I recommended hospital admission today because of hypoxemia.",
    "I considered hospital admission because of hypoxemia; after shared decision-making the patient declined.",
    "I considered hospital admission because of hypoxemia; following shared decision-making we decided on close outpatient treatment.",
])
def test_substantive_admission_decisions_include_declined_or_outpatient(quote):
    assert run(event("hospital admission", quote, "hospitalization_decision")).level == "high"


@pytest.mark.parametrize("quote", [
    "Go to the ED if shortness of breath worsens.",
    "If hypoxemia develops, I may consider hospital admission.",
    "Hospital admission was last year for hypoxemia.",
    "I recommended a cardiology referral for chest pain.",
])
def test_contingencies_historical_admission_and_referral_hold(quote):
    assert not run(event("hospital admission", quote, "hospitalization_decision")).established


def test_deescalation_needs_current_decision_and_prognosis_cause():
    quote = "I decided to forgo resuscitation because of terminal cancer and poor prognosis."
    assert run(event("resuscitation", quote, "life_sustaining_decision")).level == "high"
    for no in ("Code status: DNR.", "I decided to forgo resuscitation because of patient preference.",
               "We discussed resuscitation; prognosis is poor."):
        assert not run(event("resuscitation", no, "life_sustaining_decision")).established


def test_sdoh_needs_actual_significant_causal_care_limit():
    quote = "Treatment with insulin is significantly limited by inability to afford insulin."
    assert run(event("insulin", quote, "social_constraint")).level == "moderate"
    for no in ("The patient has financial insecurity and takes insulin.",
               "Treatment with insulin is not significantly limited by inability to afford insulin.",
               "Treatment with insulin is significantly limited by nausea."):
        assert not run(event("insulin", no, "social_constraint")).established


@pytest.mark.parametrize("subject,level", [("rest", "low"), ("hydration", "low"), ("reassurance", "straightforward")])
def test_current_conservative_and_minimal_management(subject, level):
    q = run(event(subject, f"I recommended {subject} today.", "individualized_management"))
    assert (q.level, q.established) == (level, True)


def test_duplicate_pathways_level_once_and_return_only_counted_evidence():
    act = "I started methotrexate 10 mg weekly today."
    q = run(event("methotrexate", act), event("methotrexate", act, "toxicity_monitoring", qualification=TOXICITY))
    assert q.level == "high"
    assert [e["status"] for e in q.public["events"]] == ["duplicate", "counted"]
    assert {e.rationale for e in q.evidence()} == {"MDM risk: mdm-risk-02"}


def test_raw_risk_labels_cannot_authorize_and_missing_is_not_proven_minimal():
    for raw in (None, [], {"level": "high"}, [{"level": "high"}], "high"):
        q = qualify_risk(raw, bundle(""))
        assert (q.level, q.established) == ("straightforward", False)
        assert not q.evidence()


def test_bounds_unknown_fields_private_snapshot_and_matching():
    e = event("metformin", "I started metformin today.")
    raw = [e]
    q = qualify_risk(raw, bundle(e["action_quote"]))
    assert q.matches(deepcopy(raw))
    raw[0]["subject"] = "unrelated"
    assert not q.matches(raw)
    pub = q.public
    pub["events"][0]["status"] = "held"
    assert q.established
    with pytest.raises(FrozenInstanceError):
        q.events = ()
    for bad in ([e] * 21, [{**e, "level": "high"}], [{**e, "decision_quote": None}],
                [{**e, "subject": "x" * 121}], [{**e, "action_quote": "x" * 1001}]):
        checked = qualify_risk(bad, bundle(e["action_quote"]))
        assert not checked.established
        assert len(checked.public["events"]) <= 20
    assert set(ACTIONS) == {"medication_management", "toxicity_monitoring", "surgery_decision",
                           "hospitalization_decision", "life_sustaining_decision", "social_constraint", "individualized_management"}
    assert isinstance(q, QualifiedRisk)


@pytest.mark.parametrize("quote", [
    "I started metformin today for her husband.",
    "I started metformin today for another patient.",
    "I continued metformin today; I did not review its tolerability.",
])
def test_current_decision_cannot_belong_to_somebody_else_or_negated_review(quote):
    assert not run(event("metformin", quote)).established


def test_prescription_considered_and_declined_is_a_documented_decision():
    quote = "I considered metformin today; after shared decision-making the patient declined."
    q = run(event("metformin", quote))
    assert q.level == "moderate"
    assert "considered" in q.evidence()[0].quote


@pytest.mark.parametrize("harm,method", [
    ("hepatotoxicity", "ALT monthly"),
    ("nephrotoxicity", "creatinine every 3 months"),
    ("QT prolongation", "ECG monthly"),
])
def test_distinct_objective_toxicity_methods(harm, method):
    drug = "methotrexate" if harm == "hepatotoxicity" else "lithium" if harm == "nephrotoxicity" else "amiodarone"
    q = run(event(drug, f"I started {drug} today.", "toxicity_monitoring",
                  qualification=f"I ordered {method} to monitor for {drug}-induced {harm}."))
    assert q.level == "high"


def test_toxicity_method_and_agent_cannot_be_borrowed_or_ineffective():
    for q in ("I ordered A1c weekly to monitor for methotrexate-induced myelosuppression.",
              "I ordered CBC weekly for anemia; methotrexate may cause myelosuppression.",
              "I ordered CBC weekly to monitor for methotrexate-induced myelosuppression; this order was canceled."):
        assert not run(event("methotrexate", "I started methotrexate today.", "toxicity_monitoring", qualification=q)).established


def test_cooccurring_different_treatment_cannot_supply_risk_context():
    first = "I avoided ibuprofen today."
    for q in ("For this patient, ibuprofen has low risk; warfarin has high risk of life-threatening bleeding because of anticoagulation.",
              "For this patient, ibuprofen has high risk of life-threatening bleeding because of her mother's kidney disease.",
              "For this patient, ibuprofen does not have high risk of life-threatening bleeding because of anticoagulation."):
        assert run(event("ibuprofen", first, qualification=q)).level != "high"
    q = "Colonoscopy is a minor surgery; laparotomy is an emergency major surgery."
    assert not run(event("colonoscopy", "I recommended colonoscopy today.", "surgery_decision", qualification=q)).established
    q = "Insulin remains listed; physical therapy is significantly limited by lack of transportation."
    assert not run(event("insulin", q, "social_constraint")).established
    q = "I recommended hospital admission today. Metformin was stopped because of sepsis."
    assert not run(event("hospital admission", q, "hospitalization_decision")).established


@pytest.mark.parametrize("tail", ["morphine IV is not indicated", "morphine IV was discussed but not selected"])
def test_unselected_or_negated_parenteral_route_is_not_borrowed(tail):
    q = run(event("morphine", f"I started morphine 5 mg orally today; {tail}."))
    assert q.level == "moderate"


def test_nonemergency_major_surgery_does_not_borrow_emergency_word():
    q = run(event("skin excision", "I recommended skin excision today.", "surgery_decision",
                  qualification="Skin excision is a major surgery, not an emergency."))
    assert not q.established


def test_prognosis_cause_must_belong_to_life_sustaining_decision():
    q = "I decided to stop dialysis because of catheter discomfort; poor prognosis is not the reason."
    assert not run(event("dialysis", q, "life_sustaining_decision")).established


def test_continuation_assessment_cannot_be_for_another_drug():
    q = run(event("metformin", "Continue metformin 500 mg daily.",
                  decision="Metformin is on the list; I reviewed response and tolerability of sertraline."))
    assert not q.established
    for other in ("sertraline", "unrecognized product"):
        q = f"I reviewed {other} response and tolerability and continued metformin 500 mg daily."
        assert not run(event("metformin", q)).established


def test_explicit_old_start_is_not_a_current_decision():
    assert not run(event("metformin", "I started metformin 500 mg daily in 2021.")).established


@pytest.mark.parametrize("q", [
    "I am starting metformin 500 mg daily today.",
    "I reviewed response and tolerability and refilled metformin 500 mg daily today.",
])
def test_progressive_start_and_assessed_refill_are_actual_decisions(q):
    assert run(event("metformin", q)).level == "moderate"


@pytest.mark.parametrize("task", ["review", "counseling"])
def test_recommendation_of_a_medication_related_task_is_not_rx_management(task):
    assert not run(event("metformin", f"I recommended metformin {task}.")).established
    assert not run(event("hysterectomy", f"I recommended hysterectomy {task}.", "surgery_decision",
                         qualification="Hysterectomy is an elective major surgery without identified risk factors.")).established


@pytest.mark.parametrize("quote", [
    "Begin metformin 500 mg daily today.",
    "I began losartan 25 mg daily today.",
    "I have begun sertraline 25 mg daily today.",
    "I am beginning amoxicillin 500 mg three times daily today.",
    "If the culture is positive, begin amoxicillin 500 mg three times daily for seven days.",
])
def test_affirmative_begin_forms_bind_the_current_whole_product(quote):
    subject = next(s for s in ("metformin", "losartan", "sertraline", "amoxicillin") if s in quote)
    q = run(event(subject, quote))
    assert (q.level, q.established) == ("moderate", True)
    assert [e.quote for e in q.evidence()] == [quote]
    assert q.public["rule_version"] == "office-mdm-risk.2026-09-30.v4"


@pytest.mark.parametrize("quote", [
    "I did not begin metformin 500 mg daily today.",
    "I am not beginning metformin today.",
    "I have never begun metformin today.",
    "I may begin metformin next month.",
    "I will consider beginning metformin next month.",
    "If A1c rises, begin metformin.",
    "The endocrinologist began metformin today.",
    "The patient reports I began metformin today.",
    "I began metformin today for her mother.",
    "I began metformin herbal blend today.",
])
def test_begin_does_not_relax_actor_negation_regimen_or_product_identity(quote):
    assert not run(event("metformin", quote)).established


# Exact excerpts from the frozen synthetic corpus. These are source-behavior
# fixtures, not clinician-reviewed risk or E/M gold labels. Unrelated counseling
# is intentionally supplied in the optional role to reproduce the live defect.
@pytest.mark.parametrize("subject,action,qualification", [
    ("metformin",
     "Start metformin 500 mg with the evening meal for one week, then 500 mg twice daily if tolerated.",
     "Discussed gastrointestinal effects, dehydration/sick-day precautions, and contacting the clinic for persistent symptoms."),  # SYN-CAB-20260930-01
    ("prednisone",
     "Prescribe prednisone 40 mg by mouth daily for five days; counsel about sleep disturbance, mood effects and possible transient glucose elevation.",
     "counsel about sleep disturbance, mood effects and possible transient glucose elevation."),  # -05, same assertion
    ("levothyroxine",
     "After reviewing adherence and administration, continue levothyroxine 75 micrograms daily rather than increasing the dose today. Take it consistently with water on an empty stomach and separate iron by at least four hours.",
     "Hypothyroidism with mildly elevated TSH; reduced medication absorption from simultaneous iron use is a plausible contributor."),  # -09
    ("Metformin",
     "Begin metformin 500 mg with the evening meal for seven days, then 500 mg twice daily if tolerated.",
     "Discussed gastrointestinal effects and holding medication during substantial dehydration."),  # -12
])
def test_source_fixture_base_decision_survives_optional_counseling(subject, action, qualification):
    note = action if qualification in action else action + "\n" + qualification
    q = run(event(subject, action, decision=action, qualification=qualification), note=note)
    assert (q.level, q.public["events"][0]["category"]) == ("moderate", "prescription_management")
    assert [e.quote for e in q.evidence()] == [action]
    assert len(q.public["events"][0]["reason"]) <= 300


def test_current_start_is_not_conditional_later_titration_or_future_consideration():
    action = "Begin metformin 500 mg daily today; increase to 500 mg twice daily if tolerated after seven days."
    q = run(event("metformin", action))
    assert q.level == "moderate"
    assert [e.quote for e in q.evidence()] == [action]
    no = "I might begin metformin 500 mg daily, then increase to 500 mg twice daily if tolerated."
    assert not run(event("metformin", no)).established


@pytest.mark.parametrize("qualification", [
    "Discussed medication side effects and return precautions.",
    "For this patient, warfarin has high risk of life-threatening bleeding because of anticoagulation.",
    "For this patient, ibuprofen does not have high risk of life-threatening bleeding because of anticoagulation.",
    "The patient reports ibuprofen has high risk of life-threatening bleeding because of anticoagulation.",
])
def test_optional_qualification_cannot_authorize_or_erase_independent_base(qualification):
    action = "I avoided ibuprofen today because of bleeding."
    q = run(event("ibuprofen", action, qualification=qualification))
    assert (q.level, q.established) == ("low", True)
    assert [e.quote for e in q.evidence()] == [action]
    assert "Optional context" in q.public["events"][0]["reason"]


def test_optional_decision_cannot_replace_qualification_role_for_enhancement():
    action = "I avoided ibuprofen today because of bleeding."
    risk = "For this patient, ibuprofen has high risk of life-threatening bleeding because of anticoagulation."
    q = run(event("ibuprofen", action, decision=risk))
    assert q.level == "low"
    assert [e.quote for e in q.evidence()] == [action]
    qualified = run(event("ibuprofen", action, decision="Discussed precautions.", qualification=risk))
    assert qualified.level == "high"
    assert [e.quote for e in qualified.evidence()] == [action, risk]


def test_continuation_can_use_own_assessment_or_required_decision_but_not_qualification():
    assessed = "After reviewing response and tolerability, continue sertraline 50 mg daily."
    q = run(event("sertraline", assessed, decision="Discussed nutrition.", qualification="Discussed follow-up."))
    assert q.level == "moderate"
    assert [e.quote for e in q.evidence()] == [assessed]
    action = "Continue sertraline 50 mg daily."
    decision = "I reviewed response and tolerability of sertraline."
    q = run(event("sertraline", action, decision=decision, qualification="Discussed follow-up."))
    assert q.level == "moderate"
    assert [e.quote for e in q.evidence()] == [action, decision]
    assert not run(event("sertraline", action, qualification=decision)).established


@pytest.mark.parametrize("context", [
    "I did not begin metformin today.",
    "The endocrinologist began metformin today.",
    "Correction: the metformin plan was canceled.",
])
def test_optional_roles_cannot_conceal_negated_reassigned_or_retracted_action(context):
    action = "Begin metformin 500 mg daily today."
    for role in ("decision", "qualification"):
        assert not run(event("metformin", action, **{role: context})).established


def test_negated_different_action_is_not_a_contradiction_of_proven_start():
    action = "Begin metformin 500 mg daily today."
    q = run(event("metformin", action, qualification="I did not increase metformin today."))
    assert q.level == "moderate"
    assert [e.quote for e in q.evidence()] == [action]


def test_optional_roles_must_still_be_exact_well_formed_source():
    action = "Begin metformin 500 mg daily today."
    for optional in (None, "x" * 1001, "Discussed benefits."):
        assert not run(event("metformin", action, qualification=optional), note=action).established


def test_optional_toxicity_needs_correct_role_and_same_agent():
    action = "Begin methotrexate 10 mg weekly today."
    q = run(event("methotrexate", action, "toxicity_monitoring", decision="Discussed nutrition.", qualification=TOXICITY))
    assert q.level == "high"
    assert [e.quote for e in q.evidence()] == [action, TOXICITY]
    assert not run(event("methotrexate", action, "toxicity_monitoring", decision=TOXICITY)).established
    assert not run(event("methotrexate", action, "toxicity_monitoring", qualification=TOXICITY.replace("methotrexate", "azathioprine"))).established


def test_product_status_support_is_retained_and_coverage_is_not_guessed():
    action = "Begin synthetic medicine 10 mg daily today."
    status = "Synthetic medicine is prescription-only."
    q = run(event("synthetic medicine", action, decision=status, qualification="Discussed follow-up."))
    assert q.level == "moderate"
    assert [e.quote for e in q.evidence()] == [action, status]
    for subject, statement in [
        ("metformin ER", "Begin metformin ER 500 mg daily today."),
        ("atenolol-chlorthalidone", "Begin atenolol-chlorthalidone 50-25 mg daily today."),
        ("nitrofurantoin monohydrate/macrocrystals", "Begin nitrofurantoin monohydrate/macrocrystals 100 mg orally twice daily for five days after specimen collection."),
        ("propranolol", "Begin propranolol 10 mg twice daily today."),
    ]:
        held = run(event(subject, statement, qualification="Discussed follow-up."))
        assert not held.established
        assert "exact formulation" in held.public["events"][0]["reason"]


@pytest.mark.parametrize("context", [
    "There is no prescription-only synthetic medicine available.",
    "We discussed prescription-only synthetic medicine alternatives.",
    "Synthetic medicine is not prescription-only.",
    "Synthetic medicine is prescription-only if a different formulation is selected.",
    "The patient reports synthetic medicine is prescription-only.",
])
def test_unfamiliar_product_needs_affirmative_status_not_passing_or_negated_mention(context):
    action = "Begin synthetic medicine 10 mg daily today."
    q = run(event("synthetic medicine", action, qualification=context))
    assert not q.established
    assert not q.evidence()


def test_conflicting_affirmative_product_status_is_not_resolved_by_anchor_order():
    action = "Begin synthetic medicine 10 mg daily today."
    rx = "Synthetic medicine is prescription-only."
    otc = "Synthetic medicine is over-the-counter."
    for decision, qualification in ((rx, otc), (otc, rx)):
        assert not run(event("synthetic medicine", action, decision=decision, qualification=qualification)).established


@pytest.mark.parametrize("context", [
    "A decision to begin metformin was not made today.",
    "Metformin was not prescribed by me today.",
])
def test_optional_passive_contradiction_cannot_disappear_from_decision_proof(context):
    q = run(event("metformin", "Begin metformin 500 mg daily today.", qualification=context))
    assert not q.established
    assert not q.evidence()
    assert context in [a["quote"] for a in q.public["events"][0]["evidence"]]


@pytest.mark.parametrize("action", [
    "Begin metformin 500 mg daily if the endocrinologist prescribes it next month.",
    "If the specialist authorizes it, begin metformin 500 mg daily.",
    "Begin metformin 500 mg herbal blend today.",
    "Begin metformin 500 mg extended-release today.",
])
def test_delegated_future_authority_and_post_dose_product_suffix_cannot_establish_decision(action):
    assert not run(event("metformin", action)).established


@pytest.mark.parametrize("action", [
    "Begin metformin 500 mg by mouth daily today.",
    "Begin metformin 500 mg with the evening meal today, then 500 mg twice daily if tolerated.",
    "If the culture is positive, begin amoxicillin 500 mg three times daily for seven days.",
    "Begin albuterol 2 puffs every six hours as needed today.",
])
def test_product_dose_boundary_preserves_current_regimens_and_clinical_conditions(action):
    subject = next(s for s in ("metformin", "amoxicillin", "albuterol") if s in action)
    assert run(event(subject, action)).level == "moderate"


@pytest.mark.parametrize("quote", [
    "I recommended dialysis because of poor prognosis. I stopped metformin today.",
    "I recommended dialysis because of poor prognosis and stopped metformin.",
    "I recommended dialysis because of poor prognosis; I decided against metformin.",
    "I did not stop dialysis today because of poor prognosis.",
    "The patient reports: I stopped dialysis today because of poor prognosis.",
    "The nurse stopped dialysis today because of poor prognosis.",
    "I stopped dialysis yesterday because of poor prognosis.",
])
def test_life_sustaining_withdrawal_cannot_borrow_another_action_or_actor(quote):
    q = run(event("dialysis", quote, "life_sustaining_decision"))
    assert not q.established and not q.evidence()


@pytest.mark.parametrize("quote", [
    "I stopped dialysis today because of poor prognosis.",
    "I decided to forgo dialysis because of terminal illness.",
    "I withdrew dialysis due to irreversible organ failure.",
    "I decided against dialysis because of end-stage disease.",
])
def test_life_sustaining_withdrawal_binds_action_and_cause_to_exact_intervention(quote):
    raw = event("dialysis", quote, "life_sustaining_decision")
    before = deepcopy(raw)
    q = run(raw)
    assert q.established and q.level == "high"
    assert q.public["events"][0]["category"] == "life_sustaining_deescalation"
    assert [e.quote for e in q.evidence()] == [quote]
    assert q.matches([raw]) and raw == before
