"""F2 acceptance (synthesis §F2): on synthetic telehealth notes with seeded
gaps the checker flags exactly the seeded gaps; ZERO false "complete" on
missing duration or location; override without a reason is rejected; the
attestation block renders only on pass/override; the LLM-locator seam cannot
fail open."""
from __future__ import annotations

import pytest

from cosmaneura_billing.completeness import (
    apply_override, attestation_block, check_note, is_complete, telehealth_v1,
)
from cosmaneura_billing.schemas import FeedbackKind

COMPLETE_NOTE = (
    "Telehealth video visit, established patient. Total visit time 22 minutes. "
    "Patient located at home in NH; provider located at the clinic. "
    "CC: medication follow-up. Plan: continue current regimen."
)

MISSING_DURATION = (
    "Telehealth video visit. Patient located at home; provider located at the "
    "clinic. CC: follow-up. Plan unchanged."
)

MISSING_PATIENT_LOCATION = (
    "Telehealth video visit, 18 minutes total. Provider located at the office. "
    "CC: rash follow-up."
)

MISSING_EVERYTHING = "Established patient follow-up. Doing well. Continue plan."

AMBIGUOUS_MODALITY = (
    "Spoke by phone with the patient for 15 minutes. Patient located at home; "
    "provider located at the clinic."
)


def _by_rule(results):
    return {r.rule_id: r for r in results}


def test_complete_note_passes_all_rules_and_attests():
    results = check_note(COMPLETE_NOTE)
    assert is_complete(results)
    assert {r.status for r in results} == {"present"}
    for r in results:
        assert r.evidence and r.evidence["text"] in COMPLETE_NOTE
        assert r.evidence["loc"].startswith("note:")
    block = attestation_block(results)
    assert block and block.splitlines()[0].startswith("--- Telehealth attestation")
    assert "22 minutes" in block


def test_seeded_duration_gap_is_flagged_exactly():
    r = _by_rule(check_note(MISSING_DURATION))
    assert r["telehealth.duration"].status == "missing"
    assert r["telehealth.duration"].evidence is None
    assert "minutes" in r["telehealth.duration"].fix_hint
    assert r["telehealth.modality"].status == "present"
    assert r["telehealth.patient_location"].status == "present"
    assert r["telehealth.provider_location"].status == "present"
    # zero false complete on missing duration
    assert not is_complete(check_note(MISSING_DURATION))
    assert attestation_block(check_note(MISSING_DURATION)) is None


def test_seeded_patient_location_gap_is_flagged_exactly():
    r = _by_rule(check_note(MISSING_PATIENT_LOCATION))
    assert r["telehealth.patient_location"].status == "missing"
    assert r["telehealth.duration"].status == "present"
    assert not is_complete(check_note(MISSING_PATIENT_LOCATION))


def test_bare_note_fails_every_rule():
    results = check_note(MISSING_EVERYTHING)
    assert all(res.status == "missing" for res in results)
    assert attestation_block(results) is None


def test_ambiguous_modality_never_passes():
    r = _by_rule(check_note(AMBIGUOUS_MODALITY))
    assert r["telehealth.modality"].status == "ambiguous"
    assert r["telehealth.modality"].evidence["text"].lower().startswith("phone") or \
        "phone" in r["telehealth.modality"].evidence["text"].lower()
    assert not is_complete(check_note(AMBIGUOUS_MODALITY))  # fail closed


def test_implausible_duration_does_not_count():
    note = "Telehealth video visit lasting 999 minutes. Patient located at home; provider located at the clinic."
    r = _by_rule(check_note(note))
    assert r["telehealth.duration"].status == "missing"


def test_override_requires_reason_and_logs_feedback():
    results = check_note(MISSING_DURATION)
    dur = _by_rule(results)["telehealth.duration"]
    with pytest.raises(ValueError, match="reason"):
        apply_override(dur, reason="   ", provider_id="dr-sherin")
    event = apply_override(dur, reason="Time documented in the EHR timer, not the note.",
                           provider_id="dr-sherin")
    assert dur.provider_override["overridden"] is True
    assert event.kind == FeedbackKind.DOC_GAP
    assert "telehealth.duration" in event.message
    # with the override in place the pack passes and the attestation shows it
    assert is_complete(results)
    block = attestation_block(results)
    assert block and "provider override" in block and "dr-sherin" in block


def test_override_of_passing_rule_is_refused():
    ok = _by_rule(check_note(COMPLETE_NOTE))["telehealth.duration"]
    with pytest.raises(ValueError, match="already passed"):
        apply_override(ok, reason="n/a", provider_id="dr-x")


def test_consent_rule_ships_as_config_off_by_default():
    default_ids = {r.rule_id for r in check_note(COMPLETE_NOTE)}
    assert "telehealth.consent" not in default_ids
    with_consent = check_note(COMPLETE_NOTE, pack=telehealth_v1(require_consent=True))
    assert _by_rule(with_consent)["telehealth.consent"].status == "missing"


def test_locator_seam_cannot_fail_open():
    """A hallucinating or crashing locator must be ignored — adjudication is
    deterministic and verifies every candidate against the note."""
    def lying_locator(rule_id, note):
        return ["Total visit time 45 minutes"]        # NOT in the note

    r = _by_rule(check_note(MISSING_DURATION, locator=lying_locator))
    assert r["telehealth.duration"].status == "missing"

    def crashing_locator(rule_id, note):
        raise RuntimeError("model unavailable")

    r = _by_rule(check_note(MISSING_DURATION, locator=crashing_locator))
    assert r["telehealth.duration"].status == "missing"

    def honest_locator(rule_id, note):
        return ["18 minutes total"] if rule_id == "telehealth.duration" else []

    r = _by_rule(check_note(MISSING_PATIENT_LOCATION, locator=honest_locator))
    assert r["telehealth.duration"].status == "present"
    assert r["telehealth.duration"].evidence["text"] == "18 minutes total"


def test_rule_result_contract_shape():
    j = check_note(MISSING_DURATION)[0].to_json()
    assert set(j) == {"rule_id", "status", "evidence", "fix_hint",
                      "provider_override"}
    assert j["provider_override"] == {"overridden": False, "reason": None}
