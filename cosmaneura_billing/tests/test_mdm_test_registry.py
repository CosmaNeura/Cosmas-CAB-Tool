"""Clinical-name identity and conservative overlap, not code/credit decisions."""
from dataclasses import FrozenInstanceError

import pytest

from cosmaneura_billing.knowledge.mdm_tests import (
    VERSION, TestIdentity, resolve_test, test_mentions,
)


def identity(name):
    result = resolve_test(name)
    assert isinstance(result, TestIdentity), name
    return result


def keys(text):
    return tuple(x.key for x in test_mentions(text))


@pytest.mark.parametrize("alias,canonical", [
    ("  BMP  ", "basic metabolic panel"),
    ("CMP", "comprehensive metabolic panel"),
    ("Liver function panel", "hepatic function panel"),
    ("HbA1c", "hemoglobin A1c"),
    ("TSH", "thyroid-stimulating hormone"),
    ("FT4", "free thyroxine"),
    ("TSH receptor antibodies", "TRAb"),
    ("CBC (with automated differential)", "complete blood count with automated differential"),
    ("Urine microalbumin/creatinine ratio", "UACR"),
    ("Serum beta-hCG quantitative", "quantitative serum hCG"),
    ("Urine pregnancy test", "qualitative urine hCG"),
    ("DHEA-S", "dehydroepiandrosterone sulfate"),
    ("17-hydroxyprogesterone", "17 OH progesterone"),
    ("AMH", "anti-Mullerian hormone"),
    ("SARS-CoV-2 PCR", "COVID NAAT"),
    ("TVUS", "transvaginal pelvic ultrasound"),
    ("Right knee x-ray", "radiographs of the right knee"),
    ("Left elbow radiographs 3 views", "xray left elbow 3 views"),
    ("12-lead EKG", "12 lead electrocardiogram"),
])
def test_exact_authored_aliases_share_identity(alias, canonical):
    assert identity(alias) == identity(canonical)


@pytest.mark.parametrize("name", [
    "blood work", "laboratory tests", "metabolic panel", "chem 7", "chem 8",
    "liver tests", "thyroid panel", "hormone panel", "fertility panel",
    "advanced lipid panel", "respiratory panel", "pregnancy test", "hCG",
    "beta hCG", "T4", "testosterone", "free androgen index", "calculated LDL",
    "calculated free testosterone", "rapid strep", "COVID test",
    "urinalysis with reflex culture", "TSH receptor binding assay",
    "TSH receptor antibodies panel", "CBC with pathologist interpretation",
    "right knee xray stress views", "pelvic ultrasound with Doppler",
    "exercise ECG", "ECG with stress testing", "vitamin D", "not TSH", "TSH and A1c",
])
def test_unknown_or_defining_qualifiers_never_fall_back(name):
    assert resolve_test(name) is None


def test_panel_atoms_do_not_turn_into_separate_data_credits():
    bmp, cmp, hepatic = map(identity, ("BMP", "CMP", "hepatic function panel"))
    assert bmp.components < cmp.components
    assert cmp.components & hepatic.components
    assert not bmp.components & hepatic.components
    assert len(test_mentions("Reviewed CMP.")) == 1
    assert identity("creatinine").components <= bmp.components
    assert identity("direct bilirubin").components <= hepatic.components
    assert not identity("direct bilirubin").components & cmp.components


def test_lipid_panel_does_not_invent_a_calculated_ldl_assay():
    panel = identity("lipid panel")
    assert identity("total cholesterol").components <= panel.components
    assert identity("HDL cholesterol").components <= panel.components
    assert identity("triglycerides").components <= panel.components
    assert not identity("direct LDL").components & panel.components
    assert resolve_test("calculated LDL cholesterol") is None


def test_generic_cbc_cannot_add_credit_beside_any_specific_variant():
    generic = identity("CBC")
    no_diff = identity("CBC without differential")
    auto = identity("CBC with automated differential")
    manual = identity("CBC with manual differential")
    assert "unspecified" in generic.label
    assert no_diff.components < auto.components
    assert auto.components <= generic.components
    assert manual.components <= generic.components
    assert not identity("manual differential").components & identity("automated differential").components
    for name in ("hemoglobin", "hematocrit", "platelet count", "WBC count"):
        assert identity(name).components <= no_diff.components


def test_urine_specimen_does_not_collapse_distinct_tests():
    dipstick, microscopy, both, unspecified, culture = map(identity, (
        "urine dipstick", "urine microscopy", "UA with microscopy", "urinalysis", "urine culture"))
    assert not dipstick.components & microscopy.components
    assert both.components == dipstick.components | microscopy.components
    assert unspecified.components == both.components
    assert not culture.components & both.components
    assert identity("manual urine dipstick") == identity("automated urine dipstick")
    assert identity("urine albumin").components < identity("UACR").components
    assert identity("UACR").components & identity("urine protein creatinine ratio").components
    assert not identity("urine creatinine").components & identity("serum creatinine").components


@pytest.mark.parametrize("first,second", [
    ("quantitative serum hCG", "qualitative serum hCG"),
    ("quantitative serum hCG", "urine hCG"),
    ("free T4", "total T4"),
    ("TSH", "TSH receptor antibody"),
    ("total testosterone", "free testosterone"),
    ("progesterone", "17 hydroxyprogesterone"),
    ("DHEA", "DHEAS"),
    ("CRP", "hs CRP"),
    ("COVID antigen", "COVID PCR"),
    ("HCV antibody", "HCV RNA"),
    ("hepatitis B surface antigen", "hepatitis B surface antibody"),
    ("transvaginal ultrasound", "transabdominal pelvic ultrasound"),
])
def test_defining_analyte_method_or_specimen_is_retained(first, second):
    a, b = identity(first), identity(second)
    assert a.key != b.key
    assert not a.components & b.components


def test_defined_infection_panel_overlaps_only_its_targets_and_methods():
    panel = identity("COVID flu A B NAAT panel")
    for name in ("COVID PCR", "influenza A PCR", "influenza B PCR"):
        assert identity(name).components <= panel.components
    assert not identity("COVID antigen").components & panel.components
    assert identity("chlamydia NAAT").components < identity("chlamydia and gonorrhea NAAT").components


def test_laterality_is_not_collapsed_by_modality_or_same_site():
    right = identity("right knee xray")
    left = identity("left knee xray")
    generic = identity("knee xray")
    bilateral = identity("bilateral knee xray")
    assert right.key != left.key
    assert not right.components & left.components
    assert generic.components == bilateral.components == right.components | left.components
    assert not right.components & identity("right elbow xray").components
    assert identity("right knee xray 4 views").key != right.key
    assert identity("right knee xray 4 views").components == right.components


def test_unspecified_pelvic_ultrasound_does_not_choose_a_technique():
    generic = identity("pelvic ultrasound")
    vaginal = identity("transvaginal ultrasound")
    abdominal = identity("transabdominal pelvic ultrasound")
    assert "unspecified" in generic.label
    assert generic.components == vaginal.components | abdominal.components


def test_professional_component_flag_does_not_claim_interpretation():
    for name in ("right knee xray", "pelvic ultrasound", "12 lead ECG"):
        assert identity(name).has_professional_component is True
    for name in ("CBC", "urine culture", "quantitative serum hCG", "COVID PCR"):
        assert identity(name).has_professional_component is False


def test_unspecified_ecg_and_chest_views_overlap_without_erasing_specific_identity():
    generic = identity("ECG")
    standard, rhythm = identity("12 lead ECG"), identity("ECG rhythm strip")
    assert generic.components == standard.components | rhythm.components
    assert not standard.components & rhythm.components
    plain, one, two = map(identity, ("CXR", "chest xray 1 view", "chest xray PA and lateral"))
    assert len({plain.key, one.key, two.key}) == 3
    assert plain.components == one.components == two.components


def test_longest_specific_names_shadow_embedded_generic_names():
    assert keys("Ordered TSH receptor antibodies, free T4, and total T3.") == (
        "lab.tsh_receptor_antibody", "lab.t4_free", "lab.t3_total")
    assert keys("Reviewed hemoglobin A1c and urine creatinine.") == ("lab.a1c", "lab.urine_creatinine")
    assert keys("CBC with manual differential was reviewed.") == ("lab.cbc_manual_diff",)
    assert keys("Ordered a right knee xray 4 views.") == ("imaging.xray.knee.right.views4",)


@pytest.mark.parametrize("text", [
    "Ordered TSH receptor binding assay.", "Ordered free beta hCG screening.",
    "Reviewed CBC with pathologist interpretation.", "Ordered ultrasensitive estradiol.",
    "Ordered serum testosterone mass spectrometry.", "Ordered pelvic ultrasound with Doppler.",
    "Reviewed right knee xray stress views.", "Ordered urinalysis with reflex culture.",
    "Reviewed calculated free testosterone.", "Reviewed advanced lipid panel.",
    "Ordered right knee xray 6 views.", "Ordered right knee xray, stress views.",
    "Ordered urine culture for fungi.",
])
def test_unknown_specific_phrase_cannot_supply_a_generic_mention(text):
    assert test_mentions(text) == ()


def test_list_result_and_alias_dedup_preserve_source_order():
    assert keys("Reviewed TSH 6.8, free T4 0.9 and A1c 7.2. A1c was elevated.") == (
        "lab.tsh", "lab.t4_free", "lab.a1c")
    assert keys("The thyroid panel includes TSH and free T4.") == ("lab.tsh", "lab.t4_free")
    assert keys("CBC was not ordered.") == ("lab.cbc_unspecified",)  # Name identification is not assertion/credit.


@pytest.mark.parametrize("text", [
    "Ordered right and left knee radiographs.",
    "Ordered right knee and left knee radiographs.",
    "Ordered left knee and right knee radiographs.",
])
def test_coordinated_laterality_is_bilateral_not_left_only(text):
    result = test_mentions(text)
    assert result == (identity("bilateral knee radiographs"),)
    assert result[0].components & identity("right knee xray").components
    assert result[0].components & identity("left knee xray").components


def test_parenthetical_cbc_method_does_not_expose_only_a_differential():
    result = test_mentions("Reviewed CBC (automated differential).")
    assert result == (identity("CBC with automated differential"),)
    assert result[0].components & identity("CBC without differential").components


def test_parenthetical_cbc_method_with_article_keeps_the_whole_test():
    result = test_mentions("Reviewed CBC (with a manual differential).")
    assert result == (identity("CBC with manual differential"),)


@pytest.mark.parametrize("text", [
    "Reviewed CBC, with manual differential.",
    "Reviewed CBC (with an unusual manual differential).",
    "Reviewed CBC with additional automated differential.",
])
def test_unknown_attached_cbc_phrase_never_exposes_a_standalone_constituent(text):
    assert test_mentions(text) == ()


def test_unknown_modifier_does_not_swallow_a_separate_comma_delimited_test():
    assert keys("Reviewed CBC with additional manual differential, A1c was elevated.") == ("lab.a1c",)


@pytest.mark.parametrize("separator", ["; ", ", ", ". ", "\n"])
def test_separate_test_names_cannot_be_joined_into_a_longer_alias(separator):
    phrase = "hemoglobin" + separator + "A1c"
    assert resolve_test(phrase) is None
    assert keys("Reviewed " + phrase + ".") == ("lab.hemoglobin", "lab.a1c")


@pytest.mark.parametrize("text", [
    "I considered chest X-ray; after shared decision-making the patient declined due to cost.",
    "I independently interpreted chest X-ray today: no focal infiltrate.",
    "I separately reported chest X-ray interpretation.",
    "No chest X-ray was ordered.",
    "The patient declined chest X-ray.",
    "Order chest X-ray if symptoms persist.",
])
def test_action_and_exclusion_words_do_not_hide_explicit_test_names(text):
    # The qualification layer must see names under BOTH eligible and ineligible
    # actions; merely returning an identity does not earn any MDM credit.
    assert keys(text) == ("study.chest_xray",)


def test_frozen_public_identity_and_bounded_inputs():
    value = identity("A1c")
    assert isinstance(value.components, frozenset)
    with pytest.raises(FrozenInstanceError):
        value.key = "other"
    assert VERSION == "mdm-tests.2026-09-30.v1"
    assert resolve_test(None) is None
    assert resolve_test("A1c" * 301) is None
    assert test_mentions(None) == ()
    assert test_mentions("A1c " * 30_000) == ()
