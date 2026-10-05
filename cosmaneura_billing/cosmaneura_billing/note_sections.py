"""Note section splitter (F4) — Athena-shaped paste blocks, content unaltered.

Christa (8/6) pastes the note into Athena's HPI field and then hand-cuts ROS,
assessment, and plan into their sections; Maura (7/24) copy-pastes into chief
complaint / subjective and adds assessments separately. Athena note
auto-populate (F7) was retired, so this manual copy/export path is the supported
boundary.

This is deterministic and lossless. It detects recognized section headers
(inline like ``CC:`` or at a line start) and returns the note as ordered blocks
mapped to Athena paste targets. Every character of the source is preserved in
exactly one block, in source order, so concatenating the blocks reproduces the
note **exactly** — sectioning and labeling only, never a content edit. Anything
before the first recognized header, or a note with no headers at all, lands in a
visible ``unplaced`` block rather than being dropped or guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Canonical Athena paste targets and the header aliases that map to them. A
# mislabeled header only changes a block's target, never its text, because the
# round-trip is over the exact source substring.
_ALIAS_TO_TARGET: list[tuple[str, str]] = [
    ("chief_complaint", "Chief Complaint"),
    ("chief_complaint", "CC"),
    ("hpi", "History of Present Illness"),
    ("hpi", "HPI"),
    ("hpi", "Subjective"),
    ("ros", "Review of Systems"),
    ("ros", "ROS"),
    ("objective", "Physical Examination"),
    ("objective", "Physical Exam"),
    ("objective", "Objective"),
    ("objective", "Exam"),
    ("objective", "PE"),
    ("objective", "Vitals"),
    ("objective", "Data"),
    ("assessment_plan", "Assessment/Plan"),
    ("assessment_plan", "Assessment and Plan"),
    ("assessment_plan", "Assessment & Plan"),
    ("assessment_plan", "Impression/Plan"),
    ("assessment_plan", "A/P"),
    ("assessment", "Assessment"),
    ("assessment", "Impression"),
    ("plan", "Plan"),
]

UNPLACED = "unplaced"

# Longest alias first so "Assessment/Plan" wins over "Assessment"/"Plan" and
# "History of Present Illness" over "HPI" at the same position.
_ALIASES_SORTED = sorted(_ALIAS_TO_TARGET, key=lambda pair: len(pair[1]), reverse=True)
_TARGET_BY_CASEFOLD_ALIAS = {alias.casefold(): target for target, alias in _ALIAS_TO_TARGET}

# A header is a recognized alias, not preceded by a letter (so it is a real
# boundary, not the tail of a word), followed by optional spaces and a colon.
_HEADER_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(alias) for _, alias in _ALIASES_SORTED) + r")\s*:",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NoteSection:
    """One contiguous block of the note.

    ``text`` is the exact source substring (header included) and is what
    guarantees the round-trip; ``body`` is the same block with the header
    stripped, for pasting the content alone.
    """

    target: str
    label: str
    text: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "label": self.label,
            "text": self.text,
            "body": self.body,
        }


def _target_for(alias: str) -> str:
    return _TARGET_BY_CASEFOLD_ALIAS.get(alias.casefold(), UNPLACED)


def split_note(note: str) -> list[NoteSection]:
    """Split ``note`` into ordered, lossless section blocks.

    ``"".join(s.text for s in split_note(note)) == note`` always holds.
    """
    if not isinstance(note, str):
        raise TypeError("note must be a string")
    if note == "":
        return []

    matches = list(_HEADER_RE.finditer(note))
    if not matches:
        return [NoteSection(target=UNPLACED, label="", text=note, body=note)]

    sections: list[NoteSection] = []
    first = matches[0]
    if first.start() > 0:
        lead = note[: first.start()]
        sections.append(NoteSection(target=UNPLACED, label="", text=lead, body=lead))

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(note)
        block = note[start:end]
        alias = match.group(1)
        header = note[match.start(): match.end()]
        body = note[match.end(): end]
        sections.append(NoteSection(
            target=_target_for(alias),
            label=alias.strip(),
            text=block,
            body=body,
        ))
    return sections


def grouped(sections: list[NoteSection]) -> dict[str, list[NoteSection]]:
    """Group blocks by canonical target, preserving source order within each."""
    out: dict[str, list[NoteSection]] = {}
    for section in sections:
        out.setdefault(section.target, []).append(section)
    return out


def to_paste_blocks(note: str) -> list[dict[str, Any]]:
    """Convenience: ordered dicts for a paste UI."""
    return [section.to_dict() for section in split_note(note)]


__all__ = [
    "UNPLACED",
    "NoteSection",
    "grouped",
    "split_note",
    "to_paste_blocks",
]
