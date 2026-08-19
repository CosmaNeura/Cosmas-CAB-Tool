"""Note completeness checker, telehealth.v1.

The acceptance criterion from the requirements synthesis: on synthetic telehealth
notes with seeded gaps, the checker flags exactly the seeded gaps, with **zero
false "complete"** on a missing duration or location. Those two are the specific
omissions behind MCD's Optum/UHC hard denials.
"""
import pytest

from cosmaneura_billing.completeness import (
    OverrideRequiresReason, applicable_rulepack, check_note,
)

COMPLETE_NOTE = (
    "This visit was conducted via two-way audio-video telehealth. "
    "Patient verbally consented to this telehealth visit. "
    "Patient was at home in Nashua, NH. "
    "Provider was at the St. Petersburg, FL office. "
    "Total time spent on the date of the encounter: 25 minutes. "
    "Assessment: hypertension, stable. Plan: continue lisinopril."
)

RULE_IDS = ["telehealth.modality", "telehealth.duration",
            "telehealth.patient_location", "telehealth.provider_location",
            "telehealth.consent"]


def _status(note, rule_id, **kw):
    report = check_note(note, **kw)
    return next(r for r in report.results if r.rule_id == rule_id).status


# --------------------------------------------------------------------------- #
# The complete case
# --------------------------------------------------------------------------- #
def test_complete_note_passes_every_rule():
    report = check_note(COMPLETE_NOTE)
    assert report.complete, [r.rule_id for r in report.outstanding]
    assert {r.rule_id for r in report.results} == set(RULE_IDS)
    assert all(r.evidence for r in report.results)


def test_every_result_carries_the_span_it_matched():
    for result in check_note(COMPLETE_NOTE).results:
        assert result.evidence in COMPLETE_NOTE


# --------------------------------------------------------------------------- #
# Seeded gaps — flag exactly the gap, nothing else
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("drop,expected_missing", [
    ("This visit was conducted via two-way audio-video telehealth. ",
     "telehealth.modality"),
    ("Total time spent on the date of the encounter: 25 minutes. ",
     "telehealth.duration"),
    ("Patient was at home in Nashua, NH. ", "telehealth.patient_location"),
    ("Provider was at the St. Petersburg, FL office. ", "telehealth.provider_location"),
    ("Patient verbally consented to this telehealth visit. ", "telehealth.consent"),
])
def test_removing_one_sentence_flags_exactly_that_rule(drop, expected_missing):
    note = COMPLETE_NOTE.replace(drop, "")
    report = check_note(note)
    assert not report.complete
    outstanding = [r.rule_id for r in report.outstanding]
    assert outstanding == [expected_missing], (
        f"expected only {expected_missing} outstanding, got {outstanding}")


def test_empty_note_flags_everything():
    report = check_note("")
    assert [r.rule_id for r in report.outstanding] == RULE_IDS
    assert all(r.status == "missing" for r in report.results)


# --------------------------------------------------------------------------- #
# Zero false "complete" on the two rules that actually cause denials
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("note", [
    "Telehealth visit. Total time was discussed with the patient.",
    "Telehealth visit. Duration noted in the chart.",
    "Telehealth visit. Time spent reviewing the chart beforehand.",
])
def test_duration_without_minutes_is_never_present(note):
    assert _status(note, "telehealth.duration") != "present"


@pytest.mark.parametrize("note", [
    "Telehealth visit conducted today. Patient location on file.",
    "Video visit. Patient location documented elsewhere.",
])
def test_patient_location_hedges_are_never_present(note):
    """Naming the topic without stating a place is not documentation."""
    assert _status(note, "telehealth.patient_location") != "present"


def test_incidental_location_still_counts():
    """"Patient was comfortable at home" does state where the patient was. The
    rule's job is to catch notes with no location, not to grade prose — treating
    this as a gap would nag providers over a note a payer would accept."""
    assert _status("Video visit. Patient was comfortable at home describing symptoms.",
                   "telehealth.patient_location") == "present"


def test_consent_sentence_does_not_satisfy_the_modality_rule():
    """The regression from the first run of this suite: a note whose only
    telehealth mention was the consent line reported a documented modality."""
    assert _status("Patient verbally consented to this telehealth visit.",
                   "telehealth.modality") != "present"


def test_topic_mentioned_without_the_fact_is_ambiguous_not_missing():
    """`ambiguous` tells the provider to sharpen what's there; `missing` tells
    them to write something new. Conflating them wastes their attention."""
    assert _status("Telehealth visit. Total time discussed.",
                   "telehealth.duration") == "ambiguous"
    assert _status("Telehealth visit. Nothing else recorded.",
                   "telehealth.consent") == "missing"


def test_ambiguous_still_blocks_completion():
    report = check_note("Telehealth visit. Total time discussed.")
    assert not report.complete


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
def test_override_requires_a_reason():
    with pytest.raises(OverrideRequiresReason):
        check_note(COMPLETE_NOTE, overrides={"telehealth.consent": ""})
    with pytest.raises(OverrideRequiresReason):
        check_note(COMPLETE_NOTE, overrides={"telehealth.consent": "   "})


def test_override_with_a_reason_satisfies_the_rule_and_records_why():
    note = COMPLETE_NOTE.replace("Patient verbally consented to this telehealth visit. ", "")
    report = check_note(note, overrides={
        "telehealth.consent": "standing annual consent on file for this patient"})
    assert report.complete
    consent = next(r for r in report.results if r.rule_id == "telehealth.consent")
    assert consent.status == "missing", "the finding is preserved, not rewritten"
    assert consent.satisfied and consent.overridden
    assert "standing annual consent" in consent.override_reason


# --------------------------------------------------------------------------- #
# Attestation block
# --------------------------------------------------------------------------- #
def test_attestation_withheld_until_the_note_passes():
    incomplete = check_note("Telehealth visit only.")
    assert incomplete.attestation_block() is None, (
        "attesting over a known gap would assert something untrue")


def test_attestation_renders_on_pass_with_pos_and_window():
    block = check_note(COMPLETE_NOTE).attestation_block(
        pos="10", modality="two-way audio-video")
    assert block is not None
    assert "Place of service: 10" in block
    assert "Telehealth provided in the patient's home" in block
    assert "48h" in block
    for rule_id in RULE_IDS:
        assert rule_id in block


def test_attestation_records_overrides_as_overrides():
    note = COMPLETE_NOTE.replace("Provider was at the St. Petersburg, FL office. ", "")
    block = check_note(note, overrides={
        "telehealth.provider_location": "provider address in the practice record"}
    ).attestation_block(pos="10")
    assert "provider override — provider address in the practice record" in block


# --------------------------------------------------------------------------- #
# Pack selection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pos,encounter_type,expected", [
    ("10", "telehealth_established", "telehealth.v1"),
    ("02", "office_established", "telehealth.v1"),
    ("11", "telehealth_established", "telehealth.v1"),   # typed telehealth
    ("11", "office_established", None),                  # no pack yet
])
def test_rulepack_selection(pos, encounter_type, expected):
    assert applicable_rulepack(pos, encounter_type) == expected


def test_unknown_rulepack_is_an_error_not_an_empty_pass():
    with pytest.raises(KeyError):
        check_note(COMPLETE_NOTE, rulepack="nope.v9")


def test_report_serializes_for_the_api():
    body = check_note(COMPLETE_NOTE).to_dict(pos="10")
    assert body["rulepack"] == "telehealth.v1" and body["complete"] is True
    first = body["results"][0]
    assert set(first) == {"rule_id", "status", "asks_for", "evidence",
                          "fix_hint", "provider_override"}
    assert first["provider_override"] == {"overridden": False, "reason": None}
