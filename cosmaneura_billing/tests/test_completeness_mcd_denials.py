"""MCD commercial-denial golden fixtures for the completeness checker (F2 / N3).

MCD's live denials are commercial (Optum/UHC) and, on record (billers 8/6), stem
from telehealth notes that didn't clearly state the visit was telehealth, how
long it ran, and where each party was. These fixtures pin that behavior: on
synthetic telehealth notes (POS 02/10) with seeded gaps, the checker flags
exactly the seeded gap, never reports a gapped note complete, and only emits the
attestation (with the 24-48h billability line) once every rule passes or is
overridden with a reason.
"""
from __future__ import annotations

import pytest

from cosmaneura_billing.completeness import (
    OverrideRequiresReason,
    TELEHEALTH_DOCUMENTATION_HOURS,
    applicable_rulepack,
    check_note,
)

# One well-formed, audit-defensible sentence per telehealth.v1 rule.
_SENTENCES = {
    "telehealth.modality": "This visit was conducted via two-way audio-video telehealth.",
    "telehealth.duration": "Total time spent on the date of the encounter: 25 minutes.",
    "telehealth.patient_location": "Patient was at home in Nashua, NH.",
    "telehealth.provider_location": "Provider was located at the St. Petersburg, FL office.",
    "telehealth.consent": "Patient verbally consented to this telehealth visit.",
}
_ALL_RULES = set(_SENTENCES)
_FILLER = "HPI: intermittent cough for three days, no fever."


def _note(include: set[str]) -> str:
    ordered = [_SENTENCES[k] for k in _SENTENCES if k in include]
    return _FILLER + " " + " ".join(ordered)


def _status(report, rule_id):
    return next(r for r in report.results if r.rule_id == rule_id)


def test_complete_note_passes_and_emits_attestation():
    report = check_note(_note(_ALL_RULES))
    assert report.complete is True
    assert report.outstanding == []
    block = report.attestation_block(pos="10", modality="two-way audio-video")
    assert block is not None
    assert "Place of service: 10" in block
    assert f"within {TELEHEALTH_DOCUMENTATION_HOURS}h" in block


@pytest.mark.parametrize("dropped", sorted(_ALL_RULES))
def test_each_seeded_gap_is_flagged_exactly(dropped):
    report = check_note(_note(_ALL_RULES - {dropped}))
    assert report.complete is False
    # The dropped rule is the only unsatisfied one; the rest are present.
    outstanding_ids = {r.rule_id for r in report.outstanding}
    assert outstanding_ids == {dropped}
    assert _status(report, dropped).satisfied is False
    for rule_id in _ALL_RULES - {dropped}:
        assert _status(report, rule_id).status == "present"
    # A gapped note never emits an attestation.
    assert report.attestation_block(pos="10") is None


def test_missing_duration_and_location_never_reports_complete():
    # The two Optum/UHC denial drivers together.
    report = check_note(_note(_ALL_RULES - {"telehealth.duration", "telehealth.patient_location"}))
    assert report.complete is False
    assert {"telehealth.duration", "telehealth.patient_location"} <= {
        r.rule_id for r in report.outstanding
    }


def test_ambiguous_duration_is_not_counted_present():
    # "total time" with no number is a topic mention, not a billable fact.
    note = _note(_ALL_RULES - {"telehealth.duration"}) + " Total time was discussed."
    report = check_note(note)
    duration = _status(report, "telehealth.duration")
    assert duration.status == "ambiguous"
    assert duration.satisfied is False
    assert report.complete is False


def test_override_requires_reason_and_then_completes():
    gapped = _note(_ALL_RULES - {"telehealth.duration"})
    with pytest.raises(OverrideRequiresReason):
        check_note(gapped, overrides={"telehealth.duration": "  "})

    report = check_note(
        gapped,
        overrides={"telehealth.duration": "audio-only follow-up; time not payer-required"},
    )
    assert report.complete is True
    block = report.attestation_block(pos="02", modality="audio-only")
    assert block is not None
    assert "provider override" in block


@pytest.mark.parametrize(
    "pos,encounter_type,expected",
    [
        ("02", "", "telehealth.v1"),
        ("10", "", "telehealth.v1"),
        ("11", "office_established", None),
        ("11", "telehealth_established", "telehealth.v1"),
    ],
)
def test_rulepack_applies_to_telehealth_only(pos, encounter_type, expected):
    assert applicable_rulepack(pos, encounter_type) == expected
