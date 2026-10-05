"""Tests for the note section splitter (F4 / N1).

The load-bearing property is lossless round-trip: concatenating the blocks
reproduces the source note exactly. Targeting/labeling is best-effort and never
edits content.
"""
from __future__ import annotations

import pytest

from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.note_sections import (
    UNPLACED,
    grouped,
    split_note,
    to_paste_blocks,
)


def _roundtrip(note: str) -> None:
    assert "".join(s.text for s in split_note(note)) == note


def test_demo_note_sections_and_roundtrip():
    note = SAMPLE_VISIT.signed_note
    sections = split_note(note)
    _roundtrip(note)
    targets = [s.target for s in sections]
    # Leading "26 y/o male, established patient. " precedes the first header.
    assert sections[0].target == UNPLACED
    assert "chief_complaint" in targets
    assert "hpi" in targets
    assert "objective" in targets           # Data: / Vitals:
    assert "assessment_plan" in targets     # "Assessment/Plan:" stays combined
    # Combined A/P must not be split into separate assessment + plan blocks.
    assert "assessment" not in targets
    assert "plan" not in targets


def test_roundtrip_is_exact_for_arbitrary_text():
    for note in [
        "No headers at all, just prose.",
        "   ",
        "CC: cough\nHPI: 3 days\nAssessment: URI\nPlan: fluids\n",
        "leading\nAssessment and Plan: (1) x; (2) y",
        "Plan: only a plan here",
    ]:
        _roundtrip(note)


def test_empty_note_returns_no_sections():
    assert split_note("") == []


def test_no_headers_is_single_unplaced_block():
    sections = split_note("Patient seen and evaluated.")
    assert len(sections) == 1
    assert sections[0].target == UNPLACED
    assert sections[0].label == ""
    assert sections[0].body == "Patient seen and evaluated."


def test_separate_assessment_and_plan():
    sections = split_note("Assessment: URI. Plan: rest and fluids.")
    targets = [s.target for s in sections]
    assert targets == ["assessment", "plan"]
    assert sections[0].body.strip() == "URI."
    assert sections[1].body.strip() == "rest and fluids."


def test_longest_alias_precedence():
    sections = split_note("History of Present Illness: 3 days of cough")
    assert [s.target for s in sections] == ["hpi"]
    assert sections[0].label.casefold() == "history of present illness"


def test_header_included_in_text_but_not_body():
    sections = split_note("CC: chest pain")
    assert sections[0].text == "CC: chest pain"
    assert sections[0].body == " chest pain"
    assert sections[0].label == "CC"
    assert sections[0].target == "chief_complaint"


def test_leading_text_before_first_header_is_unplaced():
    sections = split_note("26 y/o male. CC: bleeding")
    assert sections[0].target == UNPLACED
    assert sections[0].text == "26 y/o male. "
    assert sections[1].target == "chief_complaint"


def test_not_matched_inside_words():
    # "Complaint" alone, "Planning", and a mid-word "PE" must not be headers.
    note = "The patient is Planning a trip; no Complaint documented; HYPE: n/a"
    sections = split_note(note)
    assert len(sections) == 1
    assert sections[0].target == UNPLACED


def test_case_insensitive_headers():
    sections = split_note("cc: cough\nhpi: 2 days")
    assert [s.target for s in sections] == ["chief_complaint", "hpi"]


def test_grouped_collects_same_target():
    sections = split_note("Plan: A. More text. Plan: B.")
    groups = grouped(sections)
    assert len(groups["plan"]) == 2


def test_to_paste_blocks_shape():
    blocks = to_paste_blocks("CC: cough")
    assert blocks[0]["target"] == "chief_complaint"
    assert set(blocks[0]) == {"target", "label", "text", "body"}


def test_non_string_rejected():
    with pytest.raises(TypeError):
        split_note(None)  # type: ignore[arg-type]
