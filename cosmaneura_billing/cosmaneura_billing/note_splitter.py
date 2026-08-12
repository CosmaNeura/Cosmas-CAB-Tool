"""F4 — note section splitter, Athena-shaped (synthesis §F4).

Christa pastes the whole note into HPI and hand-cuts ROS/assessment/plan into
sections; Maura copy-pastes into chief complaint/subjective. This splitter
kills the manual chopping today, ahead of real Athena auto-populate (F7,
sandbox-gated): the generated note becomes labeled blocks matching Athena's
paste targets, each with its own copy payload.

Guarantees (clinical-first principle — sectioning only, never editing):
  * every section's ``text`` is a **contiguous verbatim slice** of the source
    note, headers included — concatenating all sections reproduces the source
    **exactly** (the round-trip acceptance test asserts equality, not
    approximate equality);
  * content that maps to no known target lands in a visible ``unplaced``
    section — never dropped;
  * ``copy_text`` is the paste payload: the same content with the section
    header stripped and edges trimmed, otherwise untouched.

The Athena target names/labels below are the working set; the real section
names arrive with the Aug 13 screen-share (§8 Q6) — they are configuration,
not code. Combined "Assessment/Plan" blocks stay combined (splitting them
would mean rewriting content); they carry ``target="assessment_plan"``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_UNPLACED = "unplaced"

# target -> header patterns. Order within the note decides sectioning; this
# table only classifies. Extend per the Aug 13 Athena screen-share.
DEFAULT_TARGET_PATTERNS: dict[str, tuple[str, ...]] = {
    "chief_complaint": (r"CC:", r"CHIEF COMPLAINT:?", r"Chief Complaint:"),
    "hpi_subjective": (r"HPI:", r"HISTORY OF PRESENT ILLNESS:?",
                       r"Subjective:", r"SUBJECTIVE:?"),
    "ros": (r"ROS:", r"REVIEW OF SYSTEMS:?"),
    "assessment_plan": (r"Assessment/Plan:", r"ASSESSMENT AND PLAN:?",
                        r"A/P:"),
    "assessment": (r"Assessment:", r"ASSESSMENT:?"),
    "plan": (r"Plan:", r"PLAN:?"),
}

# headers that end the previous section but have no Athena paste target —
# their content stays visible under "unplaced"
_BREAK_ONLY_PATTERNS: tuple[str, ...] = (
    r"Data:", r"Vitals:", r"VITALS:?", r"PHYSICAL EXAM:?", r"PE:",
    r"EXAM:?", r"MEDICATIONS:?", r"ALLERGIES:?", r"RESULTS:?",
    r"INSTRUCTIONS:?", r"Return if\b", r"Electronically signed",
)


@dataclass
class NoteSection:
    target: str            # athena paste target, or "unplaced"
    header: str            # the header text as it appeared ("" for unplaced)
    text: str              # verbatim contiguous slice, header included
    start: int
    end: int

    @property
    def copy_text(self) -> str:
        """Paste payload: content with the header stripped, edges trimmed,
        otherwise character-for-character the note's own text."""
        body = self.text[len(self.header):] if self.header else self.text
        return body.strip()

    def to_json(self) -> dict:
        return {"target": self.target, "header": self.header.strip(),
                "text": self.text, "copy_text": self.copy_text,
                "loc": f"note:{self.start}-{self.end}"}


def _compile(patterns: dict[str, tuple[str, ...]]):
    compiled = []
    for target, pats in patterns.items():
        for p in pats:
            # a header counts at a line start or after sentence-ish whitespace
            compiled.append((target, re.compile(
                rf"(?:(?<=^)|(?<=\n)|(?<=\. )|(?<=; ))({p})", re.M)))
    for p in _BREAK_ONLY_PATTERNS:
        compiled.append((TARGET_UNPLACED, re.compile(
            rf"(?:(?<=^)|(?<=\n)|(?<=\. )|(?<=; ))({p})", re.M)))
    return compiled


def split_note(note: str,
               target_patterns: dict[str, tuple[str, ...]] | None = None
               ) -> list[NoteSection]:
    """Split a note into Athena-target sections. Deterministic; the sections
    tile the source exactly: section[0].start == 0, each start == previous
    end, last end == len(note)."""
    if not note:
        return []
    compiled = _compile(target_patterns or DEFAULT_TARGET_PATTERNS)

    marks: list[tuple[int, int, str, str]] = []   # (start, end, target, header)
    for target, rx in compiled:
        for m in rx.finditer(note):
            marks.append((m.start(1), m.end(1), target, m.group(1)))
    # earliest offset wins; at the same offset the LONGEST header wins
    # ("ASSESSMENT AND PLAN:" must beat "ASSESSMENT:"); overlaps drop
    marks.sort(key=lambda t: (t[0], -t[1]))
    chosen: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, target, header in marks:
        if start >= last_end:
            chosen.append((start, end, target, header))
            last_end = end

    sections: list[NoteSection] = []
    if not chosen:
        return [NoteSection(TARGET_UNPLACED, "", note, 0, len(note))]

    first_start = chosen[0][0]
    if first_start > 0:
        sections.append(NoteSection(TARGET_UNPLACED, "", note[:first_start],
                                    0, first_start))
    for i, (start, hdr_end, target, header) in enumerate(chosen):
        end = chosen[i + 1][0] if i + 1 < len(chosen) else len(note)
        sections.append(NoteSection(target, header, note[start:end],
                                    start, end))
    return sections


def reassemble(sections: list[NoteSection]) -> str:
    """The inverse operation — used by the round-trip acceptance test."""
    return "".join(s.text for s in sections)
