"""Bounded, exact-source qualification of office E/M total clinician time.

This is an attestation parser, not a duration finder. It does not add component
times, round ranges, infer a service date, or turn appointment/session duration
into clinician work. Unsupported wording stays on the MDM/review path.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from .schemas import VisitBundle

_MAX_MINUTES = 1440  # More than one day cannot be one clinician's same-day time.
_MAX_QUOTE = 2000
_MAX_SOURCE = 500_000
_MINUTES = re.compile(r"(?<![\w.,+\-])([1-9][0-9]{0,3})\s*(?:minutes?|mins?)\b", re.I)
_ROLE = r"(?:physician(?:/qhp)?|qhp|qualified health care professional|clinician|provider)"
_TOTAL = re.compile(
    rf"\b(?:total\s+(?:eligible\s+)?(?:same[- ]day\s+)?(?:{_ROLE}\s+)?"
    rf"(?:e/m\s+)?time(?:\s+spent)?|total\s+(?:same[- ]day\s+)?{_ROLE}\s+work|"
    r"I\s+spent\s+(?:a\s+)?total\s+of)\b", re.I)
_DAY = re.compile(
    r"\b(?:today|same[- ]day|on\s+(?:(?:the|this)\s+)?(?:"
    r"(?:date|day)\s+of\s+(?:(?:the|this)\s+)?(?:service|encounter|visit)|"
    r"(?:service|encounter|visit)\s+date)|on\s+(?:the|this)\s+date)\b", re.I)
_CLINICIAN = re.compile(r"\b(?:physician|qhp|qualified health care professional)\b", re.I)
_SPEAKER = re.compile(r"^\s*(?:physician|qhp|provider|doctor)\s*:", re.I)
_CLAUSE_START = re.compile(
    r"^\s*(?:time\s*:\s*)?(?:(?:physician|qhp|provider|doctor)\s*:\s*)?"
    r"(?:my\s+)?(?=total\b|I\s+spent\b)", re.I)
_CARE = re.compile(r"\b(?:evaluat|manag|examin|counsel|document|review|prescrib|order|coordinat)\w*\b", re.I)
_FOLLOWUP = re.compile(
    r"^\s*(?:of\s+(?:this|that)\b|(?:this|that|the)\s+(?:time|total|figure|amount)\b|"
    r"(?:(?:this|but)\s+)?includ(?:es|ed|ing)\b|correction\b|actually\b|actual\s+total\b|"
    r"(?:the\s+)?correct(?:ed)?\s+total\b)", re.I)
_CORRECTION = re.compile(r"\b(?:incorrect|corrected|correction|revised|actual|erroneous|instead)\b|"
                         r"\brather\s+than\b|\bin\s+error\b", re.I)
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n\s*\n")
# An explicit exclusion does not add excluded work to the total. Only remove
# these narrow phrases for the rejection check, never arbitrary trailing text.
_EXCLUSION = re.compile(
    r"\b(?:excluding|excludes|not including)\s+(?:any\s+|all\s+)?"
    r"(?:separately\s+(?:reported|billed)\s+)?"
    r"(?:procedures?|procedure time|services|staff time|nursing time)\b", re.I)
_EXCLUSION_STATEMENT = re.compile(r"(?:this|that|the)\s+(?:time|total)\s+excludes\s*(.*)", re.I)
_EXCLUDED_COMPONENT = re.compile(
    r"(?:any\s+|all\s+)?(?:staff(?:\s+(?:time|work|intake|specimen handling))?|"
    r"nursing(?:\s+(?:time|work))?|waiting(?:\s+time)?|travel|commuting|"
    r"technical setup|connection setup|specimen collection|work on other dates|"
    r"separately\s+(?:reported|billed)\s+(?:procedures?(?:\s+time)?|services?|laboratory work))", re.I)
_INELIGIBLE = re.compile(
    r"\b(?:yesterday|tomorrow|historical|last\s+(?:night|week|month|year)|"
    r"another\s+day|other\s+day|planned|scheduled|estimated|"
    r"session|appointment|incorrect|retracted|hypothetical|example|template|"
    r"travel|commuting|commute|exercis\w*|intake|"
    r"waiting|waited|triage|nurse|nursing|staff|assistant|procedure|procedures|"
    r"surgery|psychotherapy|anesthesia|separately\s+(?:reported|billed))\b|"
    r"\bpatient(?:'s)?\s+(?:time|spent|completed)|"
    r"\b(?:patient|pt)\s*(?::|(?:reports?|reported|states?|stated|said|says|claims?)\b)|"
    r"\bpatient[- ]reported\b|\bper\s+(?:the\s+)?patient\b|"
    r"\b(?:will|would)\s+(?:spend|take)|"
    r"\b(?:up\s+to|at\s+least|at\s+most|(?:less|fewer|more|greater)\s+than|"
    r"or\s+(?:less|fewer|more))\b|"
    r"\b(?:under|over|below|above|nearly|about|approximately)\s+\d|"
    r"\b(?:across|over)\s+(?:two|multiple|\d+)\s+days\b|"
    r"\b(?:prior|previous|other|another)\s+(?:day|date|encounter|visit)s?\b|"
    r"\b(?:no|not|unknown|unclear|uncertain|unconfirmed|denied)\b", re.I)


@dataclass(frozen=True)
class QualifiedEMTime:
    minutes: int
    source: str
    quote: str
    start: int
    end: int
    source_sha256: str


@dataclass(frozen=True)
class TimeQualification:
    attestation: QualifiedEMTime | None
    reason: str


def _contexts(text: str):
    boundaries = [0, *(m.end() for m in _BOUNDARY.finditer(text)), len(text)]
    for start, end in zip(boundaries, boundaries[1:]):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            yield start, end, text[start:end]


def _excluded_owner_label(label: str, value: str) -> bool:
    """Patient identity metadata is neutral; actual reporting scopes persist."""
    # Keep every prior non-patient attribution marker, including unfamiliar
    # nursing/quoted/historical section suffixes.
    if re.search(r"\b(?:family|caregiver|reported|quotation|example|template|"
        r"nurse|nursing|staff|assistant|outside|external|imported|copied|quoted|prior|previous|historical)\b", label, re.I):
        return True
    if not re.search(r"\b(?:patient|pt)\b", label, re.I):
        return False
    if re.fullmatch(r"(?:patient|pt)(?:'s)?\s+(?:name|id|identifier|mrn|age|sex|demographics)", label, re.I):
        return False
    # A complete bounded demographic record establishes metadata by its value,
    # never by a synthetic label/name. A bare patient speech label still holds.
    identity = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./-]{0,120};\s*age\s+\d{1,3};\s*sex\s+(?:F|M|X|female|male|other|unknown)[.]?", value.strip(), re.I)
    metadata_label = re.fullmatch(r"(?:[A-Za-z][A-Za-z -]{0,30}\s+)?(?:patient|pt)", label, re.I)
    return not (identity and metadata_label)


def _owner_transitions(text: str):
    """Carry reported/template ownership past paragraphs and neutral headings."""
    for heading in re.finditer(r"(?:^[ \t]*|;[ \t]*)([A-Za-z][A-Za-z /'-]{0,60}):", text, re.M):
        label = heading[1].strip()
        line_end = text.find("\n", heading.end())
        value = text[heading.end():line_end if line_end >= 0 else len(text)]
        if _excluded_owner_label(label, value):
            yield heading.start(), True
        elif re.fullmatch(r"(?:current\s+)?(?:physician|qhp|provider|doctor|clinician)"
            r"(?:\s+(?:note|assessment(?: and plan)?|plan))?", label, re.I):
            yield heading.start(), False
        # A subordinate Time/Assessment label cannot change who owns the text.


def _affirmative_amount(context, total, duration):
    """Only supported affirmative numeric clauses, not arbitrary qualifiers.

    Unknown wording before/after the amount is held instead of trying to
    enumerate every English inequality, uncertainty or reported-speech form.
    """
    before = context[total.end():duration.start()].strip()
    if re.fullmatch(
        rf"(?:personally\s+)?(?:spent\s+)?(?:{_DAY.pattern}\s*)?"
        r"(?:(?:was|is)\s*)?[:=]?\s*", before, re.I,
    ) is None:
        return False
    tail = context[duration.end():].strip().rstrip(".").strip()
    day = _DAY.match(tail)
    if day:
        tail = tail[day.end():].strip()
    tail = tail.lstrip(" ,:;")
    if not tail:
        return True
    if _EXCLUSION.fullmatch(tail):
        return True
    if re.match(r"including\b", tail, re.I):
        tail = re.sub(r"^including\s+", "", tail, flags=re.I)
    if re.match(r"\d", tail):
        parts = re.split(r",\s*(?:and\s+)?(?=\d)|\band\s+(?=\d)", tail)
        for part in parts:
            component = re.fullmatch(r"([1-9][0-9]{0,3})\s+(.+)", part.strip())
            if component is None or int(component[1]) > int(duration[1]):
                return False
            activity = component[2]
            if not (_CARE.search(activity) or re.search(r"\bin\s+(?:the\s+)?(?:video\s+)?encounter\b", activity, re.I)):
                return False
        return True
    return _CARE.match(tail) is not None


def _claims(context: str, source: str, followup: str = ""):
    """Return explicit total amounts, never a sum of durations in the text."""
    if len(context) > _MAX_QUOTE or "?" in context or not _DAY.search(context):
        return []
    clause = _CLAUSE_START.match(context)
    total = _TOTAL.match(context, clause.end()) if clause else None
    if total is None:
        return []
    if source != "signed_note" and not (_SPEAKER.search(context) and _CLINICIAN.search(context)):
        return []
    # VisitBundle.signed_note is explicitly the physician-signed clinical note.
    # Transcript speech needs a clinician speaker AND an explicit role claim;
    # a patient's account of a clinician's time is not an attestation.
    if _CORRECTION.search(context + " " + followup):
        return []
    checked = _EXCLUSION.sub("", context + " " + followup)
    # Reviewing a prior report or intake questionnaire today is current
    # clinician work; it is not the patient's time completing that form.
    checked = re.sub(r"\breview(?:ed|ing)?\s+(?:the\s+)?intake\b", "review", checked, flags=re.I)
    if _INELIGIBLE.search(checked):
        return []
    if total.group().lower().startswith("i ") and not _CARE.search(context):
        return []
    duration = _MINUTES.search(context, total.end(), min(len(context), total.end() + 180))
    if duration is None or not _affirmative_amount(context, total, duration):
        return []
    minutes = int(duration[1])
    return [(minutes, duration.start(), duration.end())] if minutes <= _MAX_MINUTES else []


def qualify_em_time(minutes: object, quote: object, bundle: VisitBundle) -> TimeQualification:
    """Authenticate amount and eligibility before the time pathway is usable.

    Nothing in model output can supply QualifiedEMTime. Exact signed-note
    occurrences take precedence over transcript occurrences. Conflicting
    qualifying totals in either clinician source hold the time pathway.
    """
    if type(minutes) is not int or not 0 < minutes <= _MAX_MINUTES:
        return TimeQualification(None, "No bounded positive total-minute value was documented.")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > _MAX_QUOTE:
        return TimeQualification(None, "An exact total-time source quotation is required.")
    quoted_amounts = [int(m[1]) for m in _MINUTES.finditer(quote)]
    if minutes not in quoted_amounts:
        return TimeQualification(None, "Extracted minutes do not match the exact quoted amount.")

    sources = (("signed_note", bundle.signed_note), ("transcript", bundle.transcript))
    eligible = []
    selected = None
    occurrence_seen = False
    for name, text in sources:
        if not isinstance(text, str) or len(text) > _MAX_SOURCE:
            return TimeQualification(None, "The time source is unavailable or outside supported bounds.")
        occurrences = [m.start() for m in re.finditer(re.escape(quote), text)]
        choose_here = bool(occurrences) and not occurrence_seen
        if occurrences:
            occurrence_seen = True
        contexts = list(_contexts(text))
        owners = iter(_owner_transitions(text))
        owner = next(owners, None)
        excluded_owner = False
        for index, (start, end, context) in enumerate(contexts):
            while owner is not None and owner[0] <= start:
                excluded_owner = owner[1]
                owner = next(owners, None)
            linked = []
            unsupported_exclusion = False
            for _, _, following in contexts[index + 1:index + 4]:
                if not _FOLLOWUP.match(following):
                    break
                excluded = _EXCLUSION_STATEMENT.fullmatch(following.strip().rstrip("."))
                if excluded:
                    parts = re.split(r",\s*(?:and\s+)?|\s+and\s+", excluded[1].strip())
                    if 1 <= len(parts) <= 12 and all(_EXCLUDED_COMPONENT.fullmatch(p.strip()) for p in parts):
                        continue
                    # An unrecognized item or appended inclusion/correction
                    # cannot hide inside a sentence labelled an exclusion.
                    unsupported_exclusion = True
                linked.append(following)
            claims = [] if unsupported_exclusion or excluded_owner else _claims(context, name, " ".join(linked))
            eligible.extend(amount for amount, _, _ in claims)
            if not choose_here or len(occurrences) != 1:
                continue
            pos = occurrences[0]
            if not max(start, pos) < min(end, pos + len(quote)):
                continue
            matches = [(amount, a, b) for amount, a, b in claims
                       if amount == minutes and pos <= start + a and start + b <= pos + len(quote)]
            if len(matches) == 1:
                selected = QualifiedEMTime(minutes, name, context, start, end,
                    hashlib.sha256(text.encode("utf-8")).hexdigest())
    if len(set(eligible)) > 1:
        return TimeQualification(None, "Conflicting clinician total-time attestations require review.")
    if selected is None:
        return TimeQualification(None,
            "Exact clinician total time on this service date could not be established; review the source.")
    return TimeQualification(selected, "Exact clinician service-date total time verified.")
