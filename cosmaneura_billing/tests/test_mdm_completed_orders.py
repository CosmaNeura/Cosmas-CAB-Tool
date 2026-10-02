"""Completed-order grammar must preserve clinical test identity and evidence."""
import pytest

from cosmaneura_billing.mdm_data import VERSION, qualify_mdm_data
from cosmaneura_billing.schemas import VisitBundle


def qualified(quote, subjects, *, note=None, source="signed_note"):
    bundle = VisitBundle("SYN-ORDER-CONTRACT", 35, "F", "office_established", "11",
                         **{source: note if note is not None else quote})
    events = [{"subject": s, "action": "test_order", "source": source,
               "identity_quote": quote, "action_quote": quote,
               "qualification_quote": ""} for s in subjects]
    return qualify_mdm_data(events, bundle)


# These are unedited excerpts from the frozen synthetic notes, not target codes
# or a whitelist used by the parser. The remaining cases vary the clinical
# objects, actor, tense, coordination, method, source and reporting context.
FROZEN_URINE_ORDER = (
    "Obtain a urine specimen for external urinalysis and culture before the first antibiotic dose; "
    "the tests have been ordered but results are not yet available.")
FROZEN_PLACED_ORDERS = (
    "Orders placed today are TSH, prolactin, total testosterone, and pelvic ultrasound through outside services.")
FROZEN_ROUTED_ORDERS = (
    "I ordered quantitative serum hCG through a local laboratory and a transvaginal ultrasound "
    "for prompt outpatient assessment.")


@pytest.mark.parametrize("quote,subjects", [
    (FROZEN_URINE_ORDER, ["urinalysis", "urine culture"]),
    (FROZEN_PLACED_ORDERS, ["TSH", "prolactin", "total testosterone", "pelvic ultrasound"]),
    (FROZEN_ROUTED_ORDERS, ["quantitative serum hCG", "transvaginal ultrasound"]),
    ("Orders placed this encounter include ferritin and free T4.", ["ferritin", "free T4"]),
    ("The orders placed this visit are CBC with manual differential and CRP.", ["CBC with manual differential", "CRP"]),
    ("CBC and TSH have been ordered today.", ["CBC", "TSH"]),
    ("CBC with manual differential and TSH were ordered today by me.", ["CBC with manual differential", "TSH"]),
    ("The TSH has been ordered; results are pending.", ["TSH"]),
    ("I have personally ordered TSH through an external lab and free T4 via a reference laboratory.", ["TSH", "free T4"]),
    ("I ordered urine culture for routine outpatient evaluation.", ["urine culture"]),
    ("Order ferritin at the local laboratory today.", ["ferritin"]),
    ("Obtain a urine sample for urinalysis and culture; these tests were ordered today.", ["urinalysis", "urine culture"]),
    ("Collect the urine specimen for culture; the tests have been ordered.", ["urine culture"]),
    ("I ordered hemoglobin and hematocrit through a local lab and TSH for outpatient assessment.", ["hemoglobin and hematocrit", "TSH"]),
    ("Orders placed today are chlamydia and gonorrhea NAAT, TSH and CBC with automated differential.", ["chlamydia and gonorrhea NAAT", "TSH", "CBC with automated differential"]),
    ("I ordered right and left knee X-ray today.", ["bilateral knee X-ray"]),
    ("I ordered 1,25-dihydroxy vitamin D, TSH and ferritin today.", ["1,25-dihydroxy vitamin D", "TSH", "ferritin"]),
    ("Orders placed today are free T4, CBC and 1,25-dihydroxy vitamin D.", ["free T4", "CBC", "1,25-dihydroxy vitamin D"]),
    ("Continue metformin ER without dose change; order an external basic metabolic panel and A1c because the prior results are old.", ["basic metabolic panel", "A1c"]),
])
def test_completed_lists_preserve_each_known_test_and_exact_evidence(quote, subjects):
    result = qualified(quote, subjects)
    assert result.public["rule_version"] == VERSION == "office-mdm-data.2026-10-02.v3"
    assert result.public["category_one_units"] == len(subjects)
    assert all(e["status"] == "counted" for e in result.public["events"])
    assert all(e.quote == quote for e in result.evidence())


@pytest.mark.parametrize("quote,subjects", [
    ("The patient reports orders placed today are CBC and TSH.", ["CBC", "TSH"]),
    ("The nurse ordered CBC and TSH today.", ["CBC", "TSH"]),
    ("CBC and TSH have been ordered by the nurse today.", ["CBC", "TSH"]),
    ("CBC and TSH were ordered by Dr. Jones today.", ["CBC", "TSH"]),
    ("Orders placed today by the visiting physician are CBC and TSH.", ["CBC", "TSH"]),
    ("Orders to be placed today are CBC and TSH.", ["CBC", "TSH"]),
    ("Orders planned today are CBC and TSH.", ["CBC", "TSH"]),
    ("CBC and TSH will be ordered today.", ["CBC", "TSH"]),
    ("I will order CBC through a local laboratory and TSH for outpatient assessment.", ["CBC", "TSH"]),
    ("CBC and TSH were ordered last week for today's visit.", ["CBC", "TSH"]),
    ("Orders placed yesterday are CBC and TSH.", ["CBC", "TSH"]),
    ("Orders placed today are CBC and TSH if symptoms persist.", ["CBC", "TSH"]),
    ("Orders placed today are CBC and TSH; these orders were not placed.", ["CBC", "TSH"]),
    ("CBC and TSH have not been ordered today.", ["CBC", "TSH"]),
    ("CBC and TSH have been ordered but only CBC was actually placed.", ["CBC", "TSH"]),
    ("I ordered CBC and TSH but not CBC.", ["CBC", "TSH"]),
    ("Orders placed today are CBC and TSH?", ["CBC", "TSH"]),
    ("Orders placed today are CBC,", ["CBC"]),
    ("I ordered CBC through a local lab and TSH is normal.", ["CBC", "TSH"]),
    ("Orders placed today are TSH receptor binding assay and CBC.", ["TSH", "CBC"]),
    ("I ordered urine culture for fungi.", ["urine culture"]),
    ("I ordered urine culture for fungal assessment.", ["urine culture"]),
    ("I ordered urine fungal culture through a local laboratory.", ["urine culture"]),
    ("I ordered urine culture through a local laboratory for mycobacteria.", ["urine culture"]),
    ("I ordered culture through a local laboratory.", ["urine culture"]),
    ("I ordered urinalysis and culture today.", ["urinalysis", "urine culture"]),
    ("Obtain a urine specimen for urinalysis and culture before the first antibiotic dose.", ["urinalysis", "urine culture"]),
    ("Obtain a urine specimen for urinalysis and culture; the tests will be ordered.", ["urinalysis", "urine culture"]),
    ("Obtain a blood specimen for urinalysis and culture; the tests have been ordered.", ["urinalysis", "urine culture"]),
    ("Obtain a urine specimen for urinalysis and culture; other tests have been ordered.", ["urinalysis", "urine culture"]),
    ("Obtain a urine specimen for urinalysis and culture for fungi; the tests have been ordered.", ["urinalysis", "urine culture"]),
    ("Obtain a urine specimen for urinalysis and fungal culture; the tests have been ordered.", ["urinalysis", "urine culture"]),
    ("Obtain a urine specimen for urinalysis and culture; these tests were ordered by the patient.", ["urinalysis", "urine culture"]),
    ("Repeat TSH and free T4 in six to eight weeks after correcting timing; reassess symptoms and dosage then.", ["TSH", "free T4"]),
    ("Review home glucose twice weekly and repeat A1c in about three months.", ["A1c"]),
])
def test_noncompleted_wrong_actor_and_unknown_test_qualifiers_stay_held(quote, subjects):
    result = qualified(quote, subjects)
    assert result.public["category_one_units"] == 0
    assert all(e["status"] == "held" for e in result.public["events"])
    assert result.evidence() == []


@pytest.mark.parametrize("prefix,units", [("Physician: ", 2), ("Patient: ", 0), ("", 0)])
def test_completed_plural_orders_still_require_clinician_transcript_attribution(prefix, units):
    result = qualified(prefix + "CBC and TSH have been ordered today.", ["CBC", "TSH"], source="transcript")
    assert result.public["category_one_units"] == units


def test_patient_report_heading_cannot_be_clipped_from_completed_order_list():
    note = "Patient report:\n" + FROZEN_ROUTED_ORDERS
    assert qualified(FROZEN_ROUTED_ORDERS, ["quantitative serum hCG", "transvaginal ultrasound"], note=note).public["category_one_units"] == 0


@pytest.mark.parametrize("heading", [
    "Nurse note", "Nurse note ", "Nursing documentation", "Staff report", "Medical assistant note",
    "Outside clinician note", "External physician record", "Imported records",
    "Copied note", "Quoted record", "Prior orders", "Patient report",
])
def test_other_actor_and_quoted_record_ownership_persists_across_subheadings(heading):
    quote = "CBC and TSH have been ordered today."
    for middle in ("", "Tests:\n", "Orders:\n", "Plan:\n"):
        note = heading + ":\n" + middle + quote
        assert qualified(quote, ["CBC", "TSH"], note=note).public["category_one_units"] == 0


@pytest.mark.parametrize("quote", [
    "Nurse: I called the laboratory; CBC and TSH have been ordered today.",
    "Staff: I reviewed the record; orders placed today are CBC and TSH.",
    "Outside clinician note: I called the laboratory; CBC and TSH have been ordered today.",
])
def test_inline_other_actor_cannot_be_lost_when_order_clauses_are_split(quote):
    for fragment in (quote, quote.split("; ")[-1]):
        assert qualified(fragment, ["CBC", "TSH"], note=quote).public["category_one_units"] == 0


@pytest.mark.parametrize("reset", ["Assessment and plan", "A/P", "Current clinician note", "Physician"])
def test_current_clinician_reset_ends_an_other_actor_section(reset):
    quote = "I ordered CBC and TSH today."
    note = "Outside clinician note:\nPrior orders are available.\nTests:\nA1c is pending.\n" + reset + ":\n" + quote
    assert qualified(quote, ["CBC", "TSH"], note=note).public["category_one_units"] == 2
    inline = reset + ": " + quote
    assert qualified(inline, ["CBC", "TSH"], note="Outside clinician note:\nPrior orders are available.\n" + inline).public["category_one_units"] == 2


def test_direct_current_clinician_action_can_follow_review_of_another_note():
    quote = "I reviewed the nurse note; I ordered CBC and TSH today."
    assert qualified(quote, ["CBC", "TSH"]).public["category_one_units"] == 2


def test_completed_orders_preserve_panel_overlap_and_repeat_deduplication():
    quote = "Orders placed today are CMP, BMP, creatinine, A1c and repeat A1c through outside services."
    result = qualified(quote, ["CMP", "BMP", "creatinine", "A1c", "A1c"])
    assert result.public["category_one_units"] == 2
    assert [e["status"] for e in result.public["events"]].count("duplicate") == 3


def test_compound_alias_is_not_a_fallback_to_constituents_or_unknown_method():
    valid = "Orders placed today are hemoglobin and hematocrit through a local lab."
    assert qualified(valid, ["hemoglobin and hematocrit"]).public["category_one_units"] == 1
    assert qualified(valid, ["hemoglobin", "hematocrit"]).public["category_one_units"] == 0
    invalid = "Orders placed today are hemoglobin and hematocrit with an unusual assay."
    assert qualified(invalid, ["hemoglobin and hematocrit", "hemoglobin", "hematocrit"]).public["category_one_units"] == 0


def test_specimen_target_laterality_and_variant_are_not_normalized_away():
    assert qualified(FROZEN_URINE_ORDER, ["throat culture"]).public["category_one_units"] == 0
    assert qualified(FROZEN_URINE_ORDER, ["External urine culture"]).public["category_one_units"] == 0
    quote = "Orders placed today are left knee X-ray 4 views through outside services."
    assert qualified(quote, ["left knee X-ray 4 views"]).public["category_one_units"] == 1
    assert qualified(quote, ["right knee X-ray", "left knee X-ray"]).public["category_one_units"] == 0


def test_separate_professional_reporting_and_following_correction_still_exclude_orders():
    quote = "Orders placed today are pelvic ultrasound through outside services."
    for suffix in (" I separately reported pelvic ultrasound interpretation.",
                   " Correction: this order was canceled."):
        assert qualified(quote, ["pelvic ultrasound"], note=quote + suffix).public["category_one_units"] == 0


@pytest.mark.parametrize("report", [
    "I separately reported pelvic ultrasound through outside services.",
    "The pelvic ultrasound was separately reported by me.",
    "The pelvic ultrasound service was separately reported by me.",
    "The pelvic ultrasound interpretation will be separately billed by me.",
    "I separately reported pelvic ultrasound service for prompt outpatient assessment.",
    "I separately reported ECG; the pelvic ultrasound was separately billed by me.",
])
def test_same_test_reporting_exclusion_preserves_routing_and_passive_identity(report):
    quote = "Orders placed today are pelvic ultrasound through outside services."
    assert qualified(quote, ["pelvic ultrasound"], note=quote + " " + report).public["category_one_units"] == 0


@pytest.mark.parametrize("report", [
    "I separately reported ECG interpretation.",
    "The ECG service was separately reported by me.",
    "The pelvic ultrasound was separately reported by the radiologist.",
    "The pelvic ultrasound service was separately reported by another physician.",
    "I reviewed pelvic ultrasound; the ECG interpretation was separately reported by me.",
])
def test_reporting_another_test_or_by_another_clinician_does_not_exclude_own_order(report):
    quote = "Orders placed today are pelvic ultrasound through outside services."
    assert qualified(quote, ["pelvic ultrasound"], note=quote + " " + report).public["category_one_units"] == 1


def test_shared_specimen_cannot_be_borrowed_from_an_unrelated_assertion():
    quote = "I ordered culture through a local laboratory."
    note = "A urine specimen is available for urinalysis. " + quote
    assert qualified(quote, ["urine culture"], note=note).public["category_one_units"] == 0


def test_order_and_review_of_same_test_count_once_across_new_and_existing_grammar():
    order = "Orders placed today are quantitative serum hCG through a local laboratory."
    review = "I reviewed quantitative serum hCG today."
    note = order + "\n" + review
    bundle = VisitBundle("SYN-ORDER-REVIEW", 35, "F", "office_established", "11", signed_note=note)
    events = [{"subject": "quantitative serum hCG", "action": action, "source": "signed_note",
               "identity_quote": quote, "action_quote": quote, "qualification_quote": ""}
              for action, quote in (("test_order", order), ("test_result_review", review))]
    result = qualify_mdm_data(events, bundle).public
    assert result["category_one_units"] == 1
    assert sorted(e["status"] for e in result["events"]) == ["counted", "duplicate"]
