"""F4 acceptance (synthesis §F4): round-trip reproduces the source note
exactly; goldens across the note styles in this repo (inline demo style and
line-anchored CAPS template style); unmapped content is visible, never
dropped; copy payloads are the note's own characters."""
from __future__ import annotations

import pytest

from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.note_splitter import (
    NoteSection, reassemble, split_note, TARGET_UNPLACED,
)

CAPS_TEMPLATE_NOTE = """CHIEF COMPLAINT:
Annual follow-up, type 2 diabetes.

HISTORY OF PRESENT ILLNESS:
The patient returns for scheduled follow-up. Reports good adherence.

REVIEW OF SYSTEMS:
Negative except as noted in HPI.

PHYSICAL EXAM:
Well-appearing. No acute distress.

ASSESSMENT AND PLAN:
1. Type 2 diabetes - continue metformin, repeat A1C in 3 months.
2. Hyperlipidemia - continue statin.
"""

SEPARATE_A_AND_P_NOTE = (
    "Subjective: Feeling well, no complaints. "
    "Assessment: Stable hypertension. "
    "Plan: Continue lisinopril; recheck in 6 months."
)

HEADERLESS_NOTE = "Patient doing well overall. Continue current management."


def _tiles(sections: list[NoteSection], note: str) -> bool:
    if not sections:
        return note == ""
    if sections[0].start != 0 or sections[-1].end != len(note):
        return False
    return all(a.end == b.start for a, b in zip(sections, sections[1:]))


@pytest.mark.parametrize("note", [
    SAMPLE_VISIT.signed_note, CAPS_TEMPLATE_NOTE, SEPARATE_A_AND_P_NOTE,
    HEADERLESS_NOTE, "",
])
def test_round_trip_is_exact(note):
    sections = split_note(note)
    assert reassemble(sections) == note          # exact, not approximate
    assert _tiles(sections, note)                # contiguous, nothing dropped


def test_demo_note_golden_targets():
    sections = split_note(SAMPLE_VISIT.signed_note)
    by_target = {}
    for s in sections:
        by_target.setdefault(s.target, []).append(s)

    assert "intermittent hematochezia" in by_target["chief_complaint"][0].copy_text
    assert by_target["hpi_subjective"][0].copy_text.startswith("bright red blood")
    ap = by_target["assessment_plan"][0]
    assert "(1) Hematochezia" in ap.copy_text and "colonoscopy" in ap.copy_text
    # demographics lead-in, Data:, Vitals:, and the sign-off stay visible
    unplaced_text = " ".join(s.text for s in by_target[TARGET_UNPLACED])
    assert "26 y/o male" in unplaced_text
    assert "reviewed outside CBC" in unplaced_text
    assert "BMI 37.6" in unplaced_text
    assert "Electronically signed" in unplaced_text


def test_caps_template_golden_targets():
    sections = split_note(CAPS_TEMPLATE_NOTE)
    targets = [s.target for s in sections]
    assert targets == ["chief_complaint", "hpi_subjective", "ros",
                       TARGET_UNPLACED, "assessment_plan"]
    cc = sections[0]
    assert cc.copy_text == "Annual follow-up, type 2 diabetes."
    assert "metformin" in sections[-1].copy_text


def test_separate_assessment_and_plan_targets():
    sections = split_note(SEPARATE_A_AND_P_NOTE)
    by_target = {s.target: s for s in sections}
    assert by_target["assessment"].copy_text == "Stable hypertension."
    assert by_target["plan"].copy_text.startswith("Continue lisinopril")
    assert by_target["hpi_subjective"].copy_text.startswith("Feeling well")


def test_headerless_note_is_one_visible_unplaced_block():
    sections = split_note(HEADERLESS_NOTE)
    assert len(sections) == 1
    assert sections[0].target == TARGET_UNPLACED
    assert sections[0].text == HEADERLESS_NOTE


def test_copy_text_is_verbatim_content_without_header():
    sections = split_note(SAMPLE_VISIT.signed_note)
    for s in sections:
        assert s.copy_text in s.text             # never rewritten
        if s.header:
            assert not s.copy_text.startswith(s.header.strip())


def test_sections_serialize_with_locations():
    j = split_note(SAMPLE_VISIT.signed_note)[1].to_json()
    assert set(j) == {"target", "header", "text", "copy_text", "loc"}
    assert j["loc"].startswith("note:")
