"""Current assessment-group work, independent of individual MDM problems.

An order can belong to a combined current assessment without establishing which
individual condition owns it. This proof authorizes only the encounter-care
gate, never a diagnosis, MDM element, service level or claim.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from . import mdm_data as data
from . import mdm_problems as problems
from .schemas import Evidence, VisitBundle

VERSION = "office-encounter-care.2026-09-30.v1"
EXPLANATION = ("Current encounter assessment and diagnostic work are documented; "
               "individual problem linkage remains unresolved.")
_ASSESSMENT = re.compile(r"^(?:assessment(?: and plan)?\s*(?::|is))\s+", re.I)
_NONPROBLEM = re.compile(r"\b(?:request(?:s|ed|ing)?|desire(?:s|d)?|screening|administrative|paperwork|"
                         r"counseling|counselling|history of)\b", re.I)
_BREAK = re.compile(r"\n\s*\n|\b(?:another|different|wrong) patient\b|"
                    r"\b(?:copied|quoted|imported|prior|previous|external) "
                    r"(?:note|record|assessment|orders?)\b", re.I)
_RETRACTION = re.compile(r"\b(?:correction|corrected|retract(?:ed|ion)?|cancel(?:led|ed)?|"
    r"deferred|postponed|never (?:actually )?(?:ordered|placed|performed|done)|"
    r"not (?:actually )?(?:ordered|placed|performed|done)|"
    r"(?:did|do|does|have|has) not (?:actually )?(?:order|place|perform|provide|assess|evaluate|manage|treat)|"
    r"(?:no|zero|(?:none|neither) of (?:these|those|the)) "
    r"(?:(?:current|diagnostic|actual) )?(?:orders?|tests?|assessment|evaluation|work|care) "
    r"(?:(?:was|were|has been|have been) )?(?:actually )?(?:placed|ordered|performed|done|provided)|"
    r"entered in error|"
    r"wrong patient|another patient|different patient|"
    r"belonged to|(?:placed|ordered|performed) by (?!me\b|us\b))\b", re.I)
_REFERENCE = re.compile(r"\b(?:orders?|tests?|assessment|evaluation|work|care|these|those|it|above)\b", re.I)


@dataclass(frozen=True)
class _Group:
    id: str
    source: str
    context: str
    start: int
    end: int
    order: data._Event

    def evidence(self):
        prefix = f"Encounter care {VERSION}: {self.id}"
        return [Evidence(self.source, self.context, prefix + ": assessment context"),
                *[Evidence(a.source, a.quote, prefix + f": order anchor {i + 1}")
                  for i, a in enumerate(self.order.evidence)]]


@dataclass(frozen=True)
class QualifiedEncounterCare:
    groups: tuple[_Group, ...]
    problem_sha256: str
    data_sha256: str
    source_sha256: str
    data_source_sha256: str

    def matches(self, facts: dict) -> bool:
        p, d = facts.get("_qualified_problems"), facts.get("_qualified_mdm_data")
        return (isinstance(p, problems.QualifiedProblems) and isinstance(d, data.QualifiedMDMData)
                and p.matches(facts.get("mdm_problem_events"))
                and d.matches(facts.get("mdm_data_events"))
                and p.extraction_sha256 == self.problem_sha256
                and d.extraction_sha256 == self.data_sha256
                and p.source_sha256 == self.source_sha256
                and d.source_sha256 == self.data_source_sha256
                and all(group.order in d.events for group in self.groups))

    def evidence(self) -> list[Evidence]:
        return [e for group in self.groups for e in group.evidence()]

    def supports(self, facts: dict, retained) -> bool:
        if not self.matches(facts) or not isinstance(retained, (list, tuple)):
            return False
        kept = {(e.source, e.quote, e.rationale) for e in retained if isinstance(e, Evidence)
                and all(isinstance(v, str) for v in (e.source, e.quote, e.rationale))}
        return any(all((e.source, e.quote, e.rationale) in kept for e in group.evidence())
                   for group in self.groups)


def _assessment(raw, bundle):
    if not isinstance(raw, dict) or not problems._safe(raw.get("name"), 160):
        return None
    key = problems._key(raw["name"])
    if _NONPROBLEM.search(key):
        return None
    anchor = problems._anchor(raw.get("identity_quote"), raw.get("source"), bundle)
    if (not anchor or len(problems._sentences(anchor.quote)) != 1
            or not _ASSESSMENT.match(problems._SPEAKER.sub("", anchor.quote))
            or not problems._affirmed(anchor.quote, key)
            or problems._request_only_identity(anchor.quote, key)
            or problems._background_identity(anchor.quote, key)):
        return None
    # The model can omit "request for" from its subject. Inspect the source
    # relation too; a coordinated present concern may follow a separate request.
    current = False
    for clause, prefix, tail in problems._identity_parts(anchor.quote, key):
        scoped = re.split(r",\s*(?:with|and|plus)\s+", prefix)[-1].strip()
        owned = (not scoped or scoped in ("a", "an", "the")
                 or bool(problems._OWNER_PREFIX.fullmatch(scoped)))
        # A separately coordinated request does not erase an active concern;
        # a service-only modifier of this concern does not establish one.
        tail = re.split(r",\s*(?:with|and|plus)\s+", tail, maxsplit=1)[0]
        if owned and not _NONPROBLEM.search(scoped + " " + tail) and problems._affirmed(clause, key):
            current = True
    if not current:
        return None
    # Use the data reader's stricter source-owner stack as well. An A/P label
    # inside a copied record cannot silently become this clinician's work.
    source = getattr(bundle, anchor.source)
    if data._other_owner_context(source, anchor.start, anchor.quote):
        return None
    return anchor, key


def _continuation(text):
    """A complete uncertainty assessment stays in the proof, not an ICD fact."""
    clean = problems.normalize(text).strip()
    if (_BREAK.search(text) or problems._ACTION.search(clean)
            or problems._BAD_CONTEXT.search(clean) or data._THIRD_PARTY.search(clean)):
        return False
    concept = r"[a-z0-9 /'-]{1,160}"
    return bool(re.fullmatch(
        rf"{concept} (?:remains?|is|are) (?:a |an )?differential diagnosis"
        rf"(?:; {concept} (?:alone )?do(?:es)? not establish (?:it|this diagnosis|the diagnosis))?[.]?",
        clean))


def _same_speaker(block, source):
    labels = [match.group().split(":", 1)[0].strip().casefold()
              for start, end in problems._sentences(block)
              if (match := problems._SPEAKER.match(block[start:end]))]
    # Two different clinician role labels are not proof of one speaker.
    # Transcript assessment/order anchors each require their own label.
    return (source != "transcript" or len(labels) >= 2) and len(set(labels)) <= 1


def _corrected(group, bundle, key):
    """Do not let a clipped order escape later correction of that work.

    The entire source is inspected. Generic references are conservative because
    assigning a correction to another order would require its own source proof.
    Diagnostic uncertainty is not a correction of the clinician's work.
    """
    text = getattr(bundle, group.source)
    for start, end, sentence in data._sentences(text):
        if data._other_owner_context(text, start, sentence):
            continue
        if problems._care_denied(sentence, key):
            return True
        if not _RETRACTION.search(sentence):
            continue
        relevant = (data._test_in(group.order.test, sentence)
                    or problems._mention(key, sentence)
                    or _REFERENCE.search(sentence))
        if relevant and (start >= group.start or max(start, group.start) < min(end, group.end)):
            return True
    return False


def qualify_encounter_care(raw_problems, raw_data, bundle: VisitBundle,
                           qualified_problems, qualified_data) -> QualifiedEncounterCare:
    source_sha = problems._fingerprint({s: getattr(bundle, s, "")
                                       for s in ("signed_note", "transcript")})
    data_source_sha = hashlib.sha256((bundle.signed_note + "\0" + bundle.transcript).encode()).hexdigest()
    empty = QualifiedEncounterCare((), problems._fingerprint(raw_problems),
                                   data._fingerprint(raw_data), source_sha, data_source_sha)
    if (not isinstance(qualified_problems, problems.QualifiedProblems)
            or not isinstance(qualified_data, data.QualifiedMDMData)
            or not qualified_problems.matches(raw_problems)
            or not qualified_data.matches(raw_data)
            or qualified_problems.source_sha256 != source_sha
            or qualified_data.source_sha256 != data_source_sha
            or not isinstance(raw_problems, list) or len(raw_problems) > 20):
        return empty
    if qualified_problems.has_addressed:
        # Preserve the existing per-problem proof when it already establishes
        # encounter work; do not burden the header with duplicate evidence.
        return empty
    assessments = [a for raw in raw_problems if (a := _assessment(raw, bundle))]
    groups, seen = [], set()
    for event in qualified_data.events:
        if event.status != "counted" or event.action != "test_order" or not event.test:
            continue
        for order in event.evidence:
            if not data._test_action_clause(event.test, order.quote, "test_order"):
                continue
            for assessment, key in assessments:
                if order.source != assessment.source or order.start < assessment.end:
                    continue
                text = getattr(bundle, order.source)
                block = text[assessment.start:order.end]
                identity = (order.source, assessment.start, order.end)
                if (identity in seen or len(block) > 1000 or _BREAK.search(block)
                        or not _same_speaker(block, order.source)
                        or data._other_owner_context(text, assessment.start, block)
                        or not all(a.source == order.source and a.start >= assessment.start
                                   and a.end <= order.end for a in event.evidence)):
                    continue
                gap = text[assessment.end:order.start].strip()
                if gap and (len(problems._sentences(gap)) != 1 or not _continuation(gap)):
                    continue
                # A stated destination must name a concern in this assessment
                # or be a closed service-purpose phrase; no test-name inference.
                destinations = list(re.finditer(r"\b(?:for|to (?:evaluate|assess|investigate))\s+(.+)",
                                                problems.normalize(order.quote)))
                purpose = r"(?:prompt )?(?:(?:outpatient|inpatient) )?(?:assessment|evaluation)[.]?"
                if any(not re.fullmatch(purpose, m[1]) and not
                       any(problems._destination_matches(k, m[1]) for a, k in assessments
                           if a == assessment) for m in destinations):
                    continue
                group = _Group("group-" + problems._fingerprint([identity, block, event.id])[:16], order.source,
                               block, assessment.start, order.end, event)
                if _corrected(group, bundle, key):
                    continue
                seen.add(identity)
                groups.append(group)
                if len(groups) >= 10:
                    return QualifiedEncounterCare(tuple(groups), empty.problem_sha256,
                                                   empty.data_sha256, source_sha, data_source_sha)
    return QualifiedEncounterCare(tuple(groups), empty.problem_sha256, empty.data_sha256,
                                  source_sha, data_source_sha)
