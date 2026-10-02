"""Direct time-qualification counterparts; original pipeline assertions stay in C6."""
from dataclasses import FrozenInstanceError
import hashlib

import pytest

from cosmaneura_billing.em_time import qualify_em_time
from cosmaneura_billing.corpus.mcd_training import load_mcd_training_pack
from cosmaneura_billing.schemas import VisitBundle

TOTAL = "Total time spent on the date of the encounter: 42 minutes."
PROBLEM = "Rash: self-limited problem; I evaluated it and discussed care."



def bundle(note=TOTAL, **changes):
    return VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11",
        signed_note=PROBLEM + " Reassurance only. " + note, **changes)


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
def test_ineligible_or_mismatched_total_is_not_qualified(text, minutes):
    assert qualify_em_time(minutes, text, bundle(text)).attestation is None


@pytest.mark.parametrize("text", [
    TOTAL,
    "Total physician time on the date of service: 42 minutes.",
    "Total QHP E/M time today: 42 minutes.",
    "I spent a total of 42 minutes today evaluating and managing the patient.",
    "Total physician time on the service date: 42 minutes, excluding separately reported procedures.",
    "Total physician time today: 42 minutes including 10 minutes of chart review and 32 minutes of counseling.",
])
def test_supported_totals_keep_exact_qualified_source(text):
    source = bundle(text)
    attestation = qualify_em_time(42, text, source).attestation
    assert attestation is not None and attestation.minutes == 42
    assert attestation.source == "signed_note" and attestation.quote == text
    assert source.signed_note[attestation.start:attestation.end] == text
    assert attestation.source_sha256 == hashlib.sha256(source.signed_note.encode()).hexdigest()


@pytest.mark.parametrize("followup", [
    "This total excludes staff intake, waiting and separately reported services but includes nursing time.",
    "This total excludes staff intake, waiting and separately reported services; but includes nursing time.",
    "This total excludes staff intake, waiting and separately reported services. But includes nursing time.",
    "This total excludes an unrecognized component.",
])
def test_exclusion_list_cannot_hide_an_ineligible_inclusion(followup):
    assert qualify_em_time(42, TOTAL, bundle(TOTAL + " " + followup)).attestation is None


def test_long_source_quote_keeps_the_attested_total_span():
    total = "Total eligible physician/QHP time today was 33 minutes: 6 reviewing records, 22 evaluating and counseling, and 5 prescribing, ordering and documenting."
    text = total + " Staff, waiting, separately reported procedure time, and work on other dates are excluded."
    attestation = qualify_em_time(33, text, bundle(text)).attestation
    assert attestation is not None and attestation.minutes == 33
    assert attestation.quote == total


def test_linked_ineligible_component_invalidates_total():
    text = TOTAL + " This includes 10 minutes of procedure time."
    assert qualify_em_time(42, TOTAL, bundle(text)).attestation is None


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
def test_clipped_quote_cannot_omit_attribution_or_linked_correction(before, after):
    quote = "Total physician time today was 45 minutes."
    assert qualify_em_time(45, quote, bundle(before + quote + after)).attestation is None


def test_current_clinician_review_of_intake_is_eligible():
    text = "Total physician time today: 42 minutes reviewing the intake questionnaire and counseling."
    attestation = qualify_em_time(42, text, bundle(text)).attestation
    assert attestation is not None and attestation.minutes == 42


def test_altered_numeral_cannot_qualify_an_exact_total():
    quote = "Total physician time on the date of the encounter, including chart review and counseling, was 45 minutes."
    assert qualify_em_time(45, quote, bundle(quote.replace("45 minutes", "15 minutes"))).attestation is None


@pytest.mark.parametrize("where", ["intake_text", "transcript"])
def test_patient_text_cannot_become_clinician_time(where):
    assert qualify_em_time(42, TOTAL, bundle("", **{where: TOTAL})).attestation is None


def test_explicit_qhp_speech_keeps_its_source():
    text = "QHP: My total QHP time on the date of service was 42 minutes."
    attestation = qualify_em_time(42, text, bundle("", transcript=text)).attestation
    assert attestation is not None and attestation.source == "transcript"
    assert attestation.quote == text


def test_intake_copy_does_not_relabel_signed_attestation():
    attestation = qualify_em_time(42, TOTAL, bundle(TOTAL, intake_text=TOTAL)).attestation
    assert attestation is not None and attestation.source == "signed_note"


@pytest.mark.parametrize("other", ["signed_note", "transcript"])
def test_conflicting_clinician_totals_are_not_qualified(other):
    conflict = "QHP: Total QHP time on the date of service: 15 minutes."
    source = bundle(TOTAL + (" " + conflict if other == "signed_note" else ""),
                    transcript=conflict if other == "transcript" else "")
    result = qualify_em_time(42, TOTAL, source)
    assert result.attestation is None and "Conflicting" in result.reason


def test_ambiguous_note_cannot_fall_back_to_transcript():
    text = "Total QHP time today: 42 minutes."
    source = bundle(text + " " + text, transcript="QHP: " + text)
    assert qualify_em_time(42, text, source).attestation is None


def test_exact_quote_cannot_trim_a_negated_total():
    text = "Total physician time on the date of service: 42 minutes."
    assert qualify_em_time(42, text, bundle("No " + text)).attestation is None


def test_direct_time_proof_is_frozen_and_binds_exact_source():
    source = bundle(TOTAL)
    attestation = qualify_em_time(42, TOTAL, source).attestation
    assert attestation is not None
    assert source.signed_note[attestation.start:attestation.end] == attestation.quote == TOTAL
    assert attestation.source_sha256 == hashlib.sha256(source.signed_note.encode()).hexdigest()
    with pytest.raises(FrozenInstanceError):
        attestation.minutes = 90


@pytest.mark.parametrize("minutes", [True, 42.0, "42", 0, -1, 1441])
def test_total_requires_bounded_integer_minutes(minutes):
    assert qualify_em_time(minutes, TOTAL, bundle(TOTAL)).attestation is None


def test_missing_time_has_no_invented_attestation():
    assert qualify_em_time(None, "", bundle()).attestation is None


@pytest.mark.parametrize("heading", [
    "Patient reports:", "Template example:", "Family says:", "Nursing notes:",
    "Prior physician note:", "External clinician report:",
])
@pytest.mark.parametrize("continuation", ["", "Time:\n\n", "Assessment:\n\n"])
def test_time_keeps_source_owner_across_paragraphs_and_neutral_labels(heading, continuation):
    quote = "Total physician time today was 45 minutes."
    note = heading + "\n\n" + continuation + quote
    source = VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11", signed_note=note)
    assert qualify_em_time(45, quote, source).attestation is None


@pytest.mark.parametrize("heading", ["Patient reports:", "Template example:"])
@pytest.mark.parametrize("where", ["signed_note", "transcript"])
def test_explicit_current_clinician_resets_reported_time_owner(heading, where):
    quote = "Physician: Total physician time today was 45 minutes."
    note = heading + "\n\nA previous discussion is quoted here.\n\n" + quote
    source = VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11", **{where: note})
    q = qualify_em_time(45, quote, source).attestation
    assert q is not None and q.minutes == 45 and q.source == where
    assert q.quote == note[q.start:q.end] == quote
    assert q.source_sha256 == hashlib.sha256(note.encode()).hexdigest()


@pytest.mark.parametrize("prefix", [
    "HPI:\nRash began today.\n\nAssessment:\nI examined the rash.\n\n",
    "Patient reports:\n\nAn earlier conversation.\n\nCurrent clinician note:\n\n",
])
def test_normal_multiline_note_and_explicit_current_note_keep_time(prefix):
    quote = "Total physician time today was 45 minutes."
    source = VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11", signed_note=prefix + quote)
    q = qualify_em_time(45, quote, source).attestation
    assert q is not None and q.quote == quote


def test_reported_note_total_cannot_fall_back_to_clinician_transcript():
    quote = "Total physician time today was 45 minutes."
    source = VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11",
        signed_note="Patient reports:\n\n" + quote, transcript="Physician: " + quote)
    assert qualify_em_time(45, quote, source).attestation is None


@pytest.mark.parametrize("index,minutes", tuple(enumerate((32, 23, 28, 22, 31, 19, 16, 34, 24, 44, 26, 33))))
def test_unchanged_training_note_metadata_does_not_relabel_clinician_time(index, minutes):
    # The loader validates the original pack hash; no note text is rewritten.
    visit = load_mcd_training_pack()["cases"][index]["visit"]
    source = VisitBundle(**visit)
    quote = next(line for line in source.signed_note.splitlines() if line.startswith(("Time:", "Total ")))
    q = qualify_em_time(minutes, quote, source).attestation
    assert q is not None and q.minutes == minutes and q.source == "signed_note"
    assert q.quote == source.signed_note[q.start:q.end]
    assert quote.startswith(q.quote)
    assert q.source_sha256 == hashlib.sha256(source.signed_note.encode()).hexdigest()
    assert visit["signed_note"] == source.signed_note


@pytest.mark.parametrize("metadata,value", [
    ("Patient name", "Example name"), ("Patient ID", "Example-001"),
    ("Patient demographics", "Age 40, sex F"), ("Patient age", "40"),
    ("Patient sex", "F"), ("Physician name", "Example clinician"),
    ("Fictional patient", "Example-001; age 40; sex F"),
    ("Patient", "Example-001; age 40; sex F"),
])
def test_identity_metadata_is_neutral_without_a_synthetic_name_exception(metadata, value):
    quote = "Total physician time today was 45 minutes."
    text = metadata + ": " + value + "\n\n" + quote
    source = VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11", signed_note=text)
    assert qualify_em_time(45, quote, source).attestation is not None


@pytest.mark.parametrize("reported", [
    "Patient", "Patient reports", "Fictional patient reports", "Patient-reported history",
    "Quoted patient", "Copied prior physician note", "Nursing notes", "Template example",
    "Reported", "Reported history", "Nursing assessment", "Staff assessment",
    "Quoted patient's statement", "Historical physician assessment",
    "Patient-provided account", "Patient assessment", "Fictional patient",
])
def test_identity_metadata_or_assessment_cannot_reset_genuine_report_scope(reported):
    quote = "Total physician time today was 45 minutes."
    text = reported + ":\n\nPatient ID: Example identifier\n\nAssessment:\n\nTime:\n\n" + quote
    source = VisitBundle("SYN-EM-TIME", 40, "F", "office_established", "11", signed_note=text)
    assert qualify_em_time(45, quote, source).attestation is None
