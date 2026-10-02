"""Clinician-time presentation requires retained, requalified pipeline evidence."""
from copy import deepcopy
from dataclasses import replace

import pytest

from cosmaneura_billing.billing_header_runtime import generated_header
from cosmaneura_billing.corpus.mcd_training import load_mcd_training_pack
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.schemas import (BillingResult, BillerWorksheet, CodeKind,
    CodeSuggestion, Disposition, Evidence, ProviderCard, VisitBundle)

QUOTE = "Total physician time on the date of service: 24 minutes."


def example(note=QUOTE, *, quote=QUOTE, source="signed_note", minutes=24, disposition=Disposition.CAPTURED):
    bundle = VisitBundle("SYN-HEADER-TIME", 40, "F", "office_established", "11", signed_note=note)
    # Typed completed-result presentation fixture; no code is adjudicated here.
    suggestion = CodeSuggestion(CodeKind.EM, "99213", "Office E/M", disposition,
        evidence=[Evidence(source, quote, f"documents {minutes} min total time")],
        notes=[f"em_total_time_minutes:{minutes}"], rationale="Synthetic completed-result fixture.")
    result = BillingResult(ProviderCard(bundle.encounter_id, "Synthetic header", [suggestion]),
                           BillerWorksheet(bundle.encounter_id, em_code=suggestion))
    return bundle, suggestion, result


def render(bundle, result):
    stored = MemoryEncounterStore([bundle], practice_id="header-time").get(bundle.encounter_id)
    before = deepcopy((stored, result))
    header = generated_header(stored, result, engine_version="synthetic-presentation")
    assert (stored, result) == before
    return {f["key"]: f for f in header["facts"]}, header


@pytest.mark.parametrize("quote", [QUOTE,
    "Total physician time on the date was 24 minutes.",
    "Total eligible physician/QHP time today was 24 minutes: 5 reviewing records, 15 counseling, and 4 documenting.",
])
def test_retained_clinician_time_has_exact_evidence_and_is_not_visit_duration(quote):
    bundle, _, result = example(note=quote, quote=quote)
    facts, header = render(bundle, result)
    assert facts["em_time"]["status"] == "documented" and facts["em_time"]["value"] == "24 minutes"
    assert facts["visit_duration"]["status"] == "not_documented"
    evidence = next(e for e in header["evidence"] if e["id"] == facts["em_time"]["evidenceIds"][0])
    assert evidence["source"] == "note" and evidence["text"] == quote
    assert bundle.signed_note[evidence["start"]:evidence["end"]] == quote


def test_unchanged_ninth_training_note_retains_its_documented_time_fact():
    visit = load_mcd_training_pack()["cases"][8]["visit"]
    bundle = VisitBundle(**visit)
    quote = next(line.split(". Staff time", 1)[0] + "." for line in bundle.signed_note.splitlines() if line.startswith("Time:"))
    _, _, result = example(quote=quote)
    result.provider_card.encounter_id = result.biller_worksheet.encounter_id = bundle.encounter_id
    facts, header = render(bundle, result)
    assert facts["em_time"]["value"] == "24 minutes" and facts["em_time"]["evidenceIds"]
    assert facts["visit_duration"]["status"] == "not_documented"
    assert visit["signed_note"] == bundle.signed_note


@pytest.mark.parametrize("prefix,suffix", [
    ("Patient reports:\n\n", ""), ("Template example:\n\n", ""),
    ("Reported history:\n\n", ""), ("No ", ""),
    ("", " That total is incorrect; actual total today was 15 minutes."),
    ("", " Total physician time today was 45 minutes."),
    ("", " " + QUOTE),
])
def test_reported_negated_corrected_conflicting_or_duplicate_time_cannot_fill_fact(prefix, suffix):
    bundle, _, result = example(note=prefix + QUOTE + suffix)
    facts, _ = render(bundle, result)
    assert facts["em_time"]["status"] == "not_documented" and not facts["em_time"]["evidenceIds"]


@pytest.mark.parametrize("change", ["no_minutes", "wrong_minutes", "no_evidence", "wrong_source", "rationale_only"])
def test_regex_code_and_rationale_do_not_replace_retained_time_support(change):
    bundle, suggestion, result = example()
    if change == "no_minutes": suggestion.notes = []
    elif change == "wrong_minutes": suggestion.notes = ["em_total_time_minutes:45"]
    elif change == "no_evidence": suggestion.evidence = []
    elif change == "wrong_source": suggestion.evidence[0].source = "intake_text"
    else:
        suggestion.notes = []
        suggestion.rationale = "Total time 24 min documented on the date of encounter supports 99213."
    facts, _ = render(bundle, result)
    assert facts["em_time"]["status"] == "not_documented"


def test_transcript_time_keeps_its_own_source_and_cannot_be_relabelled():
    quote = "Physician: Total physician time today was 24 minutes."
    bundle, _, result = example(note="", quote=quote, source="transcript")
    bundle = replace(bundle, transcript=quote)
    facts, header = render(bundle, result)
    evidence = next(e for e in header["evidence"] if e["id"] == facts["em_time"]["evidenceIds"][0])
    assert evidence["source"] == "transcript" and evidence["text"] == quote
    facts, _ = render(replace(bundle, signed_note=quote), result)
    assert facts["em_time"]["status"] == "not_documented"


def test_documented_time_fact_does_not_release_a_held_em_code():
    bundle, suggestion, result = example(disposition=Disposition.REVIEW)
    result.provider_card.captured_codes = []
    result.provider_card.declined_codes = [suggestion]
    facts, header = render(bundle, result)
    assert facts["em_time"]["status"] == "documented"
    assert not any(s["kind"] == "EM" for s in header["suggestions"])
