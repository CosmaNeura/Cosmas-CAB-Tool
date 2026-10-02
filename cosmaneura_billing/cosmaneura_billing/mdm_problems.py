"""Bounded source qualification for the office E/M problem element.

Model names identify candidates; they never establish severity or chronicity.
This reader supports explicit clinician assessments and management statements,
not a general English diagnostic inference system. Unsupported relationships or
status wording remain reviewable. A held category can still have independently
established current clinical work, exposed privately by ``has_addressed``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re

from .knowledge.diagnosis_terms import normalize as _terminology_normalize
from .schemas import Evidence, VisitBundle

VERSION = "office-mdm-problems.2026-09-30.v4"
_MAX_EVENTS = 20
_BOUNDARY = re.compile(r"(?<!Dr\.)(?<!Mr\.)(?<!Ms\.)(?<=[.!?])\s+|\n")
_SPEAKER = re.compile(r"^\s*(?:physician|qhp|provider|doctor)\s*:\s*", re.I)
_COORDINATED_REQUEST = re.compile(r",\s*(?:plus|and) (?:a |the )?request for\b", re.I)
_BAD_HEADING = re.compile(r"\b(?:family|fhx|pmhx|pmh|past medical|history|patient|caregiver|"
                          r"reported|quotation|example|template|differential)\b", re.I)
_BAD_CONTEXT = re.compile(
    r"\b(?:hypothetical|example|template|retracted|incorrect|correction|corrected|"
    r"family history|fhx|pmhx|past medical history|"
    r"(?:patient|pt|mother|father|family|caregiver)\s*(?::|says?\b|said\b|reports?\b|states?\b)|"
    r"per (?:the )?patient|patient reported|"
    r"(?:mother|father|sister|brother|spouse|daughter|son|grandmother|grandfather|aunt|uncle)(?:'s)?\b|"
    r"(?:last|previous|prior) (?:visit|encounter|week|month|year)|yesterday|previously|"
    r"(?:will|might|may|would) (?:evaluate|assess|manage|treat)|"
    r"(?:not|never) (?:addressed|assessed|evaluated|managed|treated)|"
    r"(?:did|do|does) not (?:address|assess|evaluate|manage|treat))\b", re.I)
_NEGATIVE_IDENTITY = re.compile(
    r"\b(?:no|denies|denied|without|ruled out|rule out|screening for|at risk for|"
    r"at risk of|possible|suspected|probable|versus|vs|history of|h/o)\b", re.I)
_ABSENT = re.compile(r"\b(?:not (?:present|active|diagnosed|confirmed)|ruled out|resolved)\b", re.I)
_THIRD_ACTOR = re.compile(r"\b(?:nurse|staff|assistant|specialist|cardiologist|"
                          r"endocrinologist|Dr\.?\s+\w+)\s+(?:has |had )?"
                          r"(?:assessed|evaluated|managed|treated|continued|started|adjusted)\b", re.I)
_ACTION = re.compile(
    r"\b(?:assess(?:ed|ing)?|evaluat(?:ed|e|ing)|examin(?:ed|e)|manag(?:ed|e|ing)|"
    r"treat(?:ed|ing)?|discuss(?:ed)? (?:management|treatment|options|care|(?:a |the )?goals?)|"
    r"review(?:ed)? (?:control|response|symptoms|treatment|therapy)|"
    r"continu(?:e|ed|ing)|start(?:ed)?|begin|stop(?:ped)?|prescrib(?:e|ed)|adjust(?:ed)?|"
    r"increas(?:e|ed)|decreas(?:e|ed)|discontinu(?:e|ed)|order(?:ed)?|"
    r"recommend(?:ed)?|counsel(?:ed)?|monitor(?:ed)?|obtain(?:ed)?|"
    r"orders? (?:was |were )?placed(?: today)?|"
    r"(?:decided|decision) (?:against|not to)|no (?:change|intervention) (?:is )?indicated)\b", re.I)
_OWNER_PREFIX = re.compile(r"(?:for|regarding|chronic|acute|stable|new|symptomatic|newly (?:recognized|diagnosed)|"
    r"(?:acute uncomplicated|uncomplicated acute|stable acute|stable chronic|self limited|minor)|"
    r"(?:(?:acute |mild |moderate |severe )?exacerbation|progression) of|(?:the )?patient has)\s*")
# Terminology, not inferred stability: each still needs a clinician's status.
_CHRONIC_NAMES = re.compile(
    r"^(?:hypertension|type_[12] diabetes mellitus(?: with .+)?|asthma|"
    r"chronic obstructive pulmonary disease(?: with .+)?|hypothyroidism|"
    r"hyperlipidemia|hypercholesterolemia|chronic kidney disease(?: .+)?|"
    r"heart failure(?: .+)?|rheumatoid arthritis(?: .+)?|"
    r"(?:primary )?osteoarthritis(?: .+)?|(?:major )?depressive disorder(?: .+)?|"
    r"epilepsy(?: .+)?|psoriasis|crohn disease|ulcerative colitis)$")
_MODERATE = {"chronic_exacerbation", "undiagnosed_uncertain_prognosis", "acute_systemic", "complicated_injury"}
_HIGH = {"severe_chronic", "threat_to_life_or_function"}
_SYMPTOM = re.compile(r"^(?:cough|chest pain|abdominal pain|(?:left |right )?(?:knee|leg|arm|back|head|joint) pain|"
    r"pain|headache|fatigue|nausea|vomiting|dyspnea|shortness of breath|edema|swelling|fever|rash|"
    r"dizziness|syncope|palpitations|diarrhea|constipation|hemorrhage anus rectum|abdominal mass)$")


def normalize(text: str) -> str:
    # The diagnosis matcher intentionally removes whole-concept stable/control
    # wrappers. Here those clinical words are the evidence being evaluated.
    return _terminology_normalize(text + " __mdm_context__").removesuffix(" __mdm_context__")


def _positive(pattern, text: str) -> bool:
    """Find an affirmed predicate with negation scoped through a clause/list.

    A later verb after 'or' cannot escape 'did not review ... or start ...'.
    Contrast starts a new assertion; merely joining with 'and' does not.
    """
    for match in re.finditer(pattern, text, re.I) if isinstance(pattern, str) else pattern.finditer(text):
        before = re.split(r";|\b(?:but|however)\b", text[:match.start()], flags=re.I)[-1]
        after = text[match.end():]
        if re.search(r"\b(?:no|not|never|without|denies|denied|absence of|might|may|could|would|"
                     r"plan to|planned to|intend to|intended to)\b", before, re.I):
            continue
        if re.match(r"\s+(?:(?:is|was|are|were)\s+)?(?:denied|absent|excluded|not (?:present|anticipated|expected|confirmed|required))\b", after, re.I):
            continue
        return True
    return False


@dataclass(frozen=True)
class _Anchor:
    source: str
    quote: str
    start: int
    end: int


@dataclass(frozen=True)
class _Event:
    id: str
    subject: str
    key: str = ""
    status: str = "held"
    category: str | None = None
    reason: str = "Exact current problem, work and status evidence require review."
    evidence: tuple[_Anchor, ...] = ()
    addressed: bool = False
    care_evidence: tuple[_Anchor, ...] = ()


def _fingerprint(raw: object) -> str:
    try:
        return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                        allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return ""


@dataclass(frozen=True)
class QualifiedProblems:
    events: tuple[_Event, ...]
    extraction_sha256: str
    source_sha256: str

    def matches(self, raw: object) -> bool:
        return bool(self.extraction_sha256) and self.extraction_sha256 == _fingerprint(raw)

    @property
    def addressed_count(self) -> int:
        return len({e.key for e in self.events if e.addressed and e.status != "duplicate"})

    @property
    def has_addressed(self) -> bool:
        return self.addressed_count > 0

    @property
    def established(self) -> bool:
        return any(e.status == "counted" for e in self.events)

    @property
    def level(self) -> str:
        categories = [e.category for e in self.events if e.status == "counted"]
        if any(c in _HIGH for c in categories):
            return "high"
        if any(c in _MODERATE for c in categories) or categories.count("stable_chronic") >= 2:
            return "moderate"
        if any(c != "self_limited" for c in categories) or len(categories) >= 2:
            return "low"
        return "straightforward"

    @property
    def public(self) -> dict:
        return {"rule_version": VERSION, "level": self.level, "events": [
            {"id": e.id, "subject": e.subject, "status": e.status,
             "category": e.category, "reason": e.reason,
             "evidence": [{"source": a.source, "quote": a.quote} for a in e.evidence]}
            for e in self.events]}

    def evidence(self) -> list[Evidence]:
        return [Evidence(a.source, a.quote, f"MDM problems: {e.id}")
                for e in self.events if e.status == "counted" for a in e.evidence]

    def addressed_evidence_groups(self) -> list[list[Evidence]]:
        """Exact complete proofs of work, independent of problem complexity.

        Counted entries reuse their original evidence, avoiding a second copy.
        Held entries require identity AND action, never a status-only quote.
        The caller must also verify this object's extraction/source identity.
        """
        return [[Evidence(a.source, a.quote, f"{tag}: {event.id}") for a in anchors]
                for event in self.events if event.addressed and event.status != "duplicate"
                for tag, anchors in [(('MDM problems', event.evidence) if event.status == 'counted'
                                      else ('Current clinical work', event.care_evidence))]
                if anchors]

    def addressed_evidence(self) -> list[Evidence]:
        result, seen = [], set()
        for group in self.addressed_evidence_groups():
            for item in group:
                key = (item.source, item.quote, item.rationale)
                if key not in seen:
                    result.append(item)
                    seen.add(key)
        return result

    def supports_addressed(self, retained: list[Evidence]) -> bool:
        if not isinstance(retained, (list, tuple)):
            return False
        kept = {(e.source, e.quote, e.rationale) for e in retained if isinstance(e, Evidence)
                and all(isinstance(value, str) for value in (e.source, e.quote, e.rationale))}
        return any(all((e.source, e.quote, e.rationale) in kept for e in group)
                   for group in self.addressed_evidence_groups())


def _safe(value: object, maximum: int) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127
                        or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _sentences(text: str) -> list[tuple[int, int]]:
    ends = [0, *(m.end() for m in _BOUNDARY.finditer(text)), len(text)]
    return [(a, b) for a, b in zip(ends, ends[1:]) if text[a:b].strip()]


def _anchor(quote: object, source: str, bundle: VisitBundle, *, care_block=False) -> _Anchor | None:
    if source not in ("signed_note", "transcript") or not _safe(quote, 1000):
        return None
    text = getattr(bundle, source, "")
    if not isinstance(text, str) or len(text) > 500_000:
        return None
    pos = text.find(quote)
    if pos < 0 or text.find(quote, pos + 1) >= 0:
        return None
    headings = list(re.finditer(r"(?m)^\s*([A-Za-z][A-Za-z /'-]{0,60}):\s*$", text[:pos]))
    if headings and _BAD_HEADING.search(headings[-1][1]):
        return None
    spans = [(a, b) for a, b in _sentences(text) if max(a, pos) < min(b, pos + len(quote))]
    if not spans or (len(spans) > 3 and not care_block):
        return None
    start, end = spans[0][0], spans[-1][1]
    context = text[start:end].strip()
    if not _safe(context, 1000) or source == "transcript" and not _SPEAKER.match(context):
        return None
    start += len(text[start:end]) - len(text[start:end].lstrip())
    return _Anchor(source, context, start, start + len(context))


def _key(name: str) -> str:
    # Only status wrappers are removed. Type, cause, body site and laterality
    # are retained; generic diabetes is not silently typed from a sibling.
    name = normalize(name).strip(" .:;")
    name = re.sub(r"^(?:(?:well )?controlled|stable|newly diagnosed)\s+", "", name)
    name = re.sub(r"\s+(?:is )?(?:(?:well )?controlled|stable)$", "", name)
    if not name.startswith(("chronic kidney disease", "chronic obstructive pulmonary disease")):
        name = re.sub(r"^chronic\s+", "", name)
    return name


def _mention(key: str, text: str) -> re.Match | None:
    if not key:
        return None
    normalized = normalize(text)
    direct = re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", normalized)
    if direct:
        return direct
    # A comma closes an explicit subordinate background clause. Only then can
    # the same ordered head resume its absent-attribute assertion. Preserve the
    # entire matched span/indices; do not borrow a modifier from the background
    # problem, a relative clause, or a contrast. No terminology is added here.
    parts = key.split(" without ", 1)
    if len(parts) != 2 or not all(parts):
        return None
    bridge = r" (?:presentation )?in (?:a|the) patient with ([a-z0-9 /'-]{1,120}),\s*"
    match = re.search(r"(?<!\w)" + re.escape(parts[0]) + bridge
                      + "without " + re.escape(parts[1]) + r"(?!\w)", normalized)
    if not match or re.search(r"\b(?:but|however|although|yet|except|instead|which|who|that|"
                              r"also|plus|without|not|no)\b", match[1]):
        return None
    clause = re.split(r"[;.!?]", normalized[match.start():], maxsplit=1)[0]
    return None if re.search(r"\b(?:but|however|although|yet|except|instead)\b", clause) else match


def _destination_matches(key: str, text: str) -> bool:
    # A named but excluded destination ('X, not this condition') cannot own the
    # action. Apply the same clause/list polarity used for work predicates.
    return bool(key) and _positive(r"(?<!\w)" + re.escape(key) + r"(?!\w)", normalize(text))


def _clean(text: str) -> str:
    text = _SPEAKER.sub("", text).strip()
    text = re.sub(r"^(?:assessment(?: and plan)?|plan|problem)\s*(?::|is)\s*", "", text, flags=re.I)
    return normalize(re.sub(r"^\(?\d+[.)]?\s+", "", text))


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[;\n]|(?<=[.!?])\s+", _clean(text)) if part.strip()]


def _identity_parts(text: str, key: str):
    """Keep a condition's own predicate; do not apply a sibling's negation.

    Negative modifiers inside the exact identity (e.g. without aura) remain
    identity terms. A subsequent 'following a resolved illness' does not make
    the presenting cough resolved. This is not permission to drop modifiers.
    """
    for clause in _clauses(text):
        found = _mention(key, clause)
        if found:
            yield clause, clause[:found.start()], clause[found.end():]


def _absent_predicate(tail: str) -> bool:
    return bool(re.match(r"^[\s,:-]*(?:(?:is|was|are|remains?|appears?|has been|has|had)\s+)?"
        r"(?:(?:now|currently|clinically)\s+)?(?:not (?:present|active|diagnosed|confirmed|established)|"
        r"ruled out|resolved|absent|denied|excluded|unconfirmed|possible|suspected|probable)\b", tail))


def _affirmed(text: str, key: str) -> bool:
    clean = _clean(text)
    # A speaker/quotation prefix scopes the entire quoted assertion, including
    # semicolon continuations; a clipped quote cannot drop that attribution.
    lead = re.split(r"[;.!?]", clean, maxsplit=1)[0]
    if _BAD_CONTEXT.search(lead) or _THIRD_ACTOR.search(lead):
        return False
    for clause, prefix, tail in _identity_parts(text, key):
        if ("?" in clause or _BAD_CONTEXT.search(clause) or _THIRD_ACTOR.search(clause)
                or _NEGATIVE_IDENTITY.search(prefix) or _absent_predicate(tail)
                or re.search(r"\b(?:if|unless|may have|might have)\b", prefix)):
            continue
        return True
    return False


def _bound(anchor: _Anchor, identity: _Anchor, key: str, text: str, keys: set[str]) -> str | None:
    """Permit a repeated identity or the immediately adjacent owned statement.

    An unlabeled medication/action from another part of the note cannot be
    borrowed. A pronoun or continuation belongs only to a single preceding
    condition assertion, with no intervening text or other extracted condition.
    """
    if _request_only_identity(anchor.quote, key):
        return None
    if _mention(key, anchor.quote):
        return anchor.quote if _affirmed(anchor.quote, key) else None
    if _COORDINATED_REQUEST.search(identity.quote):
        return None
    if anchor.start < identity.end or anchor.end - identity.start > 1000:
        return None
    if "\n\n" in text[identity.end:anchor.start] or not _affirmed(identity.quote, key):
        return None
    block = text[identity.start:anchor.end]
    if any(k != key and _mention(k, block) and _affirmed(block, k)
           and not _background_identity(block, k) for k in keys):
        return None
    gap = text[identity.end:anchor.start]
    bridges = [_clean(gap[a:b]) for a, b in _sentences(gap)]
    if len(bridges) > 3 or any(not _continuation(part) for part in bridges):
        return None
    clean = _clean(anchor.quote)
    first = _clauses(clean)[0]
    if (_BAD_CONTEXT.search(first) or _THIRD_ACTOR.search(first) or "?" in first
            or re.search(r"\b(?:not addressed|not evaluated)\b", first)):
        return None
    if re.match(r"^(?:it|this (?:condition|problem|illness|injury)|current control)\b", clean):
        return key + re.sub(r"^(?:it|this (?:condition|problem|illness|injury)|current control)\b", "", clean)
    clean = re.sub(r"^after (?:shared discussion|reviewing (?:adherence|administration|adherence and administration)),\s*", "", clean)
    if re.match(r"^(?:i |we )?(?:continue|continued|start|started|begin|stop|stopped|adjust|adjusted|"
                r"increase|increased|decrease|decreased|discontinue|discontinued|"
                r"assessed|evaluated|discussed|ordered|orders? (?:was |were )?placed|obtain|obtained|"
                r"prescribe|prescribed|recommend|recommended|counsel|counseled|monitor|no intervention)\b", clean, re.I):
        return key + ": " + clean
    return None


def _continuation(part: str) -> bool:
    if _BAD_CONTEXT.search(part) or _THIRD_ACTOR.search(part) or "?" in part:
        return False
    if re.match(r"^(?:it|this (?:condition|problem|illness|injury)|current control|we discussed|i discussed)\b", part):
        return True
    # Excluding an alternative diagnosis does not introduce a new care owner.
    # Contrast/conjunction with a new affirmative assertion remains a barrier.
    return bool(re.match(r"^(?:there is no|there are no|no evidence of|"
                         r"the current (?:history|examination|history and examination) do(?:es)? not establish)\b", part)
                and not re.search(r"\b(?:but|however|yet|although|instead)\b|;", part))


def _background_identity(text: str, key: str) -> bool:
    """A subordinate patient-history phrase is not a second A/P owner.

    It cannot itself establish current work; separate/coordinated assessments
    of that condition still interrupt the unlabelled continuation.
    """
    parts = list(_identity_parts(text, key))
    return bool(parts) and all(re.search(r"\bin (?:a |the )?patient with\s+[^;:]*$", prefix)
                               and not re.search(r"\b(?:but|however|plus|also)\b", prefix)
                               for _, prefix, _ in parts)


def _request_only_identity(text: str, key: str) -> bool:
    """A request alone cannot establish current work for its named subject.

    It still remains a possible competing owner of an unlabelled order. A
    separate named completed act can establish care through normal validation.
    """
    parts = list(_identity_parts(text, key))
    return bool(parts) and all(
        re.search(r",\s*(?:plus|and) (?:a |the )?request for\s*$", prefix)
        and not tail.strip(" .")
        for _, prefix, tail in parts)


def _focused(text: str, key: str) -> str | None:
    # Status from a second semicolon-delimited problem cannot qualify the first.
    for clause in re.split(r"[;\n]|(?<=[.!?])\s+", _clean(text)):
        found = _mention(key, clause)
        if found and _affirmed(clause, key):
            prefix = clause[:found.start()].strip()
            tail = clause[found.end():].strip(" :,-.")
            tail = re.sub(r"^(?:is |remains |has been |currently |now |was assessed as |is assessed as )+", "", tail)
            if re.fullmatch(r"(?:acute uncomplicated|uncomplicated acute|stable acute|stable chronic|self limited|minor)", prefix):
                tail = prefix + " condition " + tail
            elif re.fullmatch(r"(?:(?:acute |mild |moderate |severe )?exacerbation|progression) of", prefix):
                tail = prefix.removesuffix(" of") + " " + tail
            # A differential belongs to the unresolved presenting problem;
            # its morbidity clause may follow a semicolon. Other statements
            # do not silently inherit this condition's complexity.
            following = _clean(text)[_clean(text).find(clause) + len(clause):]
            differential = re.match(r";\s*(differential\b[^;\n]*)", following)
            return tail + ("; " + differential[1] if differential else "")
    return None


def _diagnostic_specimen_work(clause: str, text: str, key: str, *, named_owner=False) -> bool:
    """Recognize an owned completed diagnostic decision, not a data unit.

    Laboratory noun heads identify the role of the specimen destination. They
    do not resolve a test, analyte, method, specimen or code. A second explicit
    condition destination or an unestablished/future order cannot be borrowed.
    """
    work = clause.split(":", 1)[-1].strip()
    match = re.fullmatch(r"(?:i |we )?obtain(?:ed)? (?:a |an |the )?"
        r"(?:[a-z]+\s+){0,3}specimen for (.+)", work)
    if not match:
        return False
    tests = re.split(r"\s+(?:before|after)\s+", match[1], maxsplit=1)[0].rstrip(" .")
    # Explicit indications must bind this same problem. A test list with a
    # foreign 'for/to evaluate' destination is not just a routing modifier.
    indication = re.search(r"\s+(?:for|to (?:evaluate|assess|investigate))\s+(.+)", tests)
    if indication:
        if not _destination_matches(key, indication[1]):
            return False
        tests = tests[:indication.start()]
    units = re.split(r",\s*(?:and\s+)?|\s+and\s+", tests)
    noun = r"(?:urinalysis|culture|microscopy|cytology|histology|analysis|assay|panel|blood count|tests?|testing)"
    if not units or any(not re.fullmatch(rf"(?:[a-z0-9/-]+\s+){{0,6}}{noun}", unit) for unit in units):
        return False
    completed = r"^(?:the |these )?tests (?:have been|were|are) ordered\b"
    for part in _clauses(text):
        match = re.match(completed, part)
        if not match or _BAD_CONTEXT.search(part):
            continue
        # The completion assertion owns its actor and indication too. Retain
        # only closed affirmative forms; a marker somewhere in a delegated or
        # foreign-condition sentence is insufficient evidence of this work.
        tail = part[match.end():].strip(" .")
        tail = re.sub(r"(?:,?\s*(?:but|and)\s+)(?:the )?results (?:are|remain) "
                      r"(?:not yet available|not available|pending)(?: today)?$", "", tail).strip()
        tail = re.sub(r"^today\b\s*", "", tail)
        tail = re.sub(r"^by me\b\s*", "", tail)
        if not tail and not named_owner:
            return True
        indication = re.fullmatch(r"(?:for|to (?:evaluate|assess|investigate))\s+(.+)", tail)
        if indication and normalize(indication[1]) == key:
            return True
    return False


def _addressed(text: str, key: str) -> bool:
    if not _affirmed(text, key):
        return False
    clean = _clean(text)
    owned = False
    # A definite initial act survives a separate conditional subsequent step.
    # The entire source assertion remains in its anchor, including that limit.
    for clause in re.split(r"[;\n]|(?<=[.!?])\s+|,\s*then\b", clean):
        clause = _clean(clause)
        clause = re.split(r",\s*(?:and\s+)?(?:gradually\s+)?(?=(?:begin|start|continue|increase|decrease|adjust)\b)", clause, maxsplit=1)[0]
        if (_BAD_CONTEXT.search(clause) or _THIRD_ACTOR.search(clause) or "?" in clause):
            owned = False
            continue
        mention = _mention(key, clause)
        if mention:
            prefix = clause[:mention.start()].strip()
            owned = (not prefix or bool(_OWNER_PREFIX.fullmatch(prefix))
                     or bool(re.fullmatch(r"(?:i |we )?(?:have )?(?:assessed|evaluated|examined|managed|treated|"
                         r"am managing|am treating|discussed (?:management|treatment|care|options) (?:of|for)|"
                         r"reviewed (?:control|response|symptoms|treatment|therapy) (?:of|for))\s*(?:the )?", prefix)))
            # A shared assessment/request clause cannot silently assign the
            # following unnamed work, even if extraction omits the request.
            owned = owned and not _COORDINATED_REQUEST.search(clause)
            action_start = _ACTION.search(clause)
            if action_start and re.fullmatch(r"(?:i|we)?\s*", clause[:action_start.start()]):
                destination = re.search(r"\b(?:for|of|about|to (?:treat|manage|address|control))\s+(.+)", clause[action_start.end():])
                owned = owned or bool(destination and _destination_matches(key, destination[1]))
        elif not re.match(r"^(?:(?:i|we)\b|(?:it|this (?:condition|problem|illness|injury))\b|"
                          r"differential\b|continue\b|start\b|begin\b|stop\b|adjust\b|increase\b|decrease\b|"
                          r"recommend\b|recommended\b|counsel\b|counseled\b|monitor\b|obtain\b|obtained\b|"
                          r"orders?\b|prescribe\b|prescribed\b|discontinue\b|no intervention\b)", clause):
            owned = False
        if not owned and _diagnostic_specimen_work(clause, clean, key, named_owner=True):
            owned = True
        if not owned:
            continue
        if not _ACTION.search(clause):
            # Explicit condition-owned A/P noun phrases document conservative
            # management. They do not establish any complexity category.
            fragment = clause[mention.end():].strip(" :-.") if mention else ""
            care = r"(?:dietary fiber|fiber|hydration|fluids|rest|ice|elevation|supportive care|wound care|activity modification)"
            if mention and re.fullmatch(rf"{care}(?:(?:,\s*|\s+and\s+){care})*", fragment):
                return True
            continue
        action_context = clause
        first_action = _ACTION.search(clause)
        if mention and first_action and mention.end() <= first_action.start():
            action_context = clause[mention.end():]
            before_action = clause[mention.end():first_action.start()]
            boundary = max(before_action.rfind(":"), before_action.rfind(","))
            if boundary >= 0:
                action_context = action_context[boundary + 1:]
        if not _positive(_ACTION, action_context):
            continue
        if re.search(r"\b(?:if|unless|will evaluate|plan to evaluate|not today|not this visit|"
                     r"after (?:a )?(?:future|next) (?:evaluation|visit))\b", clause):
            continue
        # A generic evaluation verb cannot borrow a different named object.
        generic = re.search(r"\b(?:assessed|evaluated|examined|managed|treated)\s+(.+)", clause)
        if generic and not (_mention(key, generic[1]) or re.match(r"(?:it|this (?:problem|condition|illness|injury))\b", generic[1])):
            continue
        if _diagnostic_specimen_work(clause, clean, key):
            return True
        destinations = re.finditer(r"\b(?:for|of|about|to (?:treat|manage|address|control))\s+(.+)", clause[_ACTION.search(clause).end():])
        topic = r"(?:dietary fiber|fiber|hydration|fluids|rest|nutrition|exercise|medication adherence|side effects|prevention|symptom relief|comfort(?: while walking)?)"
        duration = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve) (?:days?|weeks?|months?)\b"
        # A closed diagnostic service-purpose is not a disease indication.
        # Full-match the tail so an added 'of/for <other condition>' cannot be
        # clipped away. It applies only to an already owned affirmative order.
        diagnostic_order = bool(re.fullmatch(r"order(?:ed)?|orders? (?:was |were )?placed(?: today)?",
                                             _ACTION.search(clause)[0]))
        purpose = r"(?:prompt )?(?:(?:outpatient|inpatient) )?(?:assessment|evaluation)[.]?"
        if any(not (_destination_matches(key, d[1]) or re.match(r"(?:it|this (?:problem|condition|illness|injury)|\d+ (?:days?|weeks?|months?))\b", d[1])
                       or re.match(duration, d[1])
                       or re.fullmatch(rf"{topic}(?:(?:,\s*|\s+and\s+){topic})*[.]?", d[1])
                       or (diagnostic_order and d[0].startswith("for ") and re.fullmatch(purpose, d[1]))) for d in destinations):
            continue
        if re.search(r"\b(?:recommend(?:ed)?|monitor) (?:a |an |the )?(?:referral|consultation|evaluation|specialist)\b", clause):
            continue
        return True
    return False


def _care_denied(text: str, key: str) -> bool:
    """Read a retracted generic care object, not any negative drug sentence.

    Active and passive voice have the same ownership requirement. A separately
    named treatment or a different explicit indication cannot retract this act.
    The caller additionally binds this sentence to the action's source context.
    """
    obj = r"(?:(?:this|that|the) )?(?:treatment|medication|care|evaluation|work)|it"
    verb = r"start(?:ed)?|begin|began|begun|initiat(?:e|ed)|continu(?:e|ed)|prescrib(?:e|ed)|order(?:ed)?|perform(?:ed)?"
    patterns = (
        rf"(?:i|we) (?:(?:did|do|have|had) not|(?:(?:have|had) )?never) (?:actually )?(?:{verb}) (?:{obj})\b",
        rf"(?:{obj}) (?:(?:is|was|has been|had been) (?:not|never)|(?:has|had) (?:not|never) been) (?:actually )?(?:{verb})\b",
        rf"(?:{obj}) (?:is|was|has been|had been) (?:deferred|postponed|cancelled|canceled)\b",
        rf"(?:{obj}) will (?:only )?(?:start|begin|be (?:started|initiated|performed))\b",
    )
    for clause in _clauses(text):
        clause = re.sub(r"^(?:correction|corrected)\s*:\s*", "", clause)
        match = next((m for pattern in patterns if (m := re.match(pattern, clause))), None)
        if match:
            destination = re.search(r"\b(?:for|of|to (?:treat|evaluate|manage|address))\s+(.+)", clause[match.end():])
            indication = destination[1].strip(" .") if destination else ""
            # A reason, duration or patient qualifier is not a different care
            # owner. Only a recognized, disjoint condition can establish that
            # exception; an extracted candidate name alone cannot grant it.
            other_condition = (bool(_CHRONIC_NAMES.fullmatch(indication) or _SYMPTOM.fullmatch(indication))
                               and not _mention(key, indication) and not _mention(indication, key))
            if not other_condition:
                return True
    return False


def _care_block_action(anchor, identity, key, text, keys):
    """Find a complete owned act without discarding the submitted care block.

    Longer extraction spans are evidence containers, not one giant assertion.
    Each candidate still passes the ordinary three-sentence/actor/ownership
    checks. The full block remains the required care anchor; its attribution,
    retractions and source-wide current-work denials cannot be clipped away.
    """
    if (_BAD_CONTEXT.search(_clean(anchor.quote)) or _THIRD_ACTOR.search(anchor.quote)
            or "?" in anchor.quote):
        return None
    spans = _sentences(anchor.quote)
    for i in range(len(spans)):
        for width in (1, 2, 3):
            if i + width > len(spans):
                break
            fragment = anchor.quote[spans[i][0]:spans[i + width - 1][1]]
            quote = fragment.strip()
            offset = anchor.start + spans[i][0] + len(fragment) - len(fragment.lstrip())
            # The unique complete parent already pins these exact sentences.
            # Avoid re-searching a large source for every private projection;
            # only the full parent is retained as the proof of this selection.
            candidate = _Anchor(anchor.source, quote, offset, offset + len(quote))
            bound = _bound(candidate, identity, key, text, keys)
            if bound and _addressed(bound, key):
                return bound
    return None


def _category(text: str, key: str, identity: str) -> str | None:
    tail = _focused(text, key)
    if tail is None:
        return None
    # Strip no clinical terms. Unknown leading assertions remain held instead
    # of searching anywhere for an attractive severity word.
    tail = re.sub(r"^(?:a |an |the )", "", tail)
    if re.search(r"\b(?:may develop|might develop|could develop|if (?!untreated\b)|unless|"
                 r"not established|not confirmed|status (?:is )?(?:unknown|unclear))\b", tail):
        return None
    if re.search(r"\b(?:according to|per (?:the )?patient|reported by|in (?:the )?(?:(?:19|20)\d{2}|old|prior|previous|historical) (?:note|record|visit))\b", tail):
        return None
    # Chronicity must belong to this condition, not another diagnosis elsewhere
    # in the same quote. Its name alone still cannot establish control/severity.
    chronic = bool(_CHRONIC_NAMES.fullmatch(key) or re.search(r"\bchronic\s+" + re.escape(key) + r"\b", identity)
                   or re.search(re.escape(key) + r"\s+(?:is |remains )?(?:a )?chronic (?:condition|illness|problem)\b", identity))
    if re.match(r"(?:acute |chronic )?(?:illness |injury |condition )?(?:poses?|posing|presents?) (?:an? )?(?:current |immediate )?threat to (?:life|bodily function)", tail):
        if not re.search(r"\b(?:if|might|may|could|potential|possible|risk of)\b", tail) and _positive(r"threat to (?:life|bodily function)", tail):
            return "threat_to_life_or_function"
    if re.match(r"(?:life threatening|threatening (?:life|bodily function))\b", tail) and _positive(r"life threatening|threatening (?:life|bodily function)", tail):
        return "threat_to_life_or_function"
    if chronic and re.match(r"(?:in |with )?severe (?:exacerbation|progression|side effects? of treatment)\b", tail):
        if _positive(r"\b(?:organ (?:damage|failure|dysfunction)|threat(?:ens?|ening)? (?:to )?(?:life|bodily function)|"
                     r"(?:requiring|requires|necessitating) (?:hospitalization|hospital admission|urgent intervention))\b", tail):
            return "severe_chronic"
    if re.match(r"(?:acute )?complicated injury\b", tail) or re.match(
            r"(?:injury |fracture |sprain |laceration )?complicated by (?:neurovascular (?:compromise|injury)|"
            r"tendon (?:injury|damage)|nerve (?:injury|damage)|multiple (?:injuries|treatment options))\b", tail):
        if _positive(r"complicated (?:injury|by .+?(?:injury|damage|compromise|options))", tail):
            return "complicated_injury"
    if re.match(r"acute (?:illness|infection) with systemic (?:symptoms|involvement)\b", tail):
        # The clinician must document the serious consequence; fever or fatigue
        # accompanying a minor illness cannot alone become systemic complexity.
        if _positive(r"\b(?:high (?:risk of )?morbidity|risk (?:of|to) (?:organ damage|organ dysfunction|bodily function)|"
                     r"systemic involvement requiring treatment to prevent (?:organ damage|high morbidity))\b", tail):
            return "acute_systemic"
    if re.match(r"(?:undiagnosed |unexplained |new undiagnosed )?(?:problem |symptom )?(?:with )?uncertain prognosis\b", tail):
        if (_positive(r"\b(?:differential|diagnostic possibilities|concern for)\b", tail)
                and _positive(r"\b(?:likely|substantial|high|significant) (?:risk of )?morbidity\b", tail)
                and re.search(r"\b(?:without treatment|if untreated|without diagnosis and treatment)\b", tail)):
            return "undiagnosed_uncertain_prognosis"
    if chronic and re.match(r"(?:in |with )?(?:(?:acute |mild |moderate )?exacerbation|progression|"
                            r"side effects? (?:of|from) (?:treatment|therapy)|poorly controlled|not (?:at goal|well controlled)|"
                            r"(?:with )?(?:glycemia |control )?above (?:the )?(?:individualized |individual |preconception )?(?:goal|target))\b", tail):
        if _positive(r"exacerbation|progression|side effects? (?:of|from) (?:treatment|therapy)|poorly controlled|above .+?(?:goal|target)", tail) or re.match(r"not (?:at goal|well controlled)", tail):
            return "chronic_exacerbation"
    if chronic and re.match(r"(?:stable(?: chronic)?|well controlled|controlled|at (?:individualized |individual )?(?:goal|target)|"
                            r"meeting (?:individualized |individual )?(?:goals|targets))\b", tail):
        if not re.search(r"\b(?:(?:not at|above|not meeting|not reaching) (?:the )?(?:(?:individualized|individual|treatment|personal|preconception) )?(?:goals?|targets?)|"
                         r"(?:goals?|targets?) (?:(?:are|is|remain|remains|were) )?(?:not (?:met|achieved|reached)|unmet|unachieved)|"
                         r"poorly controlled|worsening|exacerbation|progression)\b", tail):
            if _positive(r"stable(?: chronic)?|well controlled|controlled|at (?:individualized |individual )?(?:goal|target)|meeting (?:individualized |individual )?(?:goals|targets)", tail):
                return "stable_chronic"
    if re.match(r"stable acute (?:illness|problem|condition)\b", tail) or re.match(
            r"acute (?:illness|problem|condition) (?:is )?(?:improving|stable)\b", tail):
        if re.search(r"\b(?:initial treatment|treatment initiated|responding to treatment|improving|stable)\b", tail):
            if _positive(r"stable acute (?:illness|problem|condition)|acute (?:illness|problem|condition) (?:is )?(?:improving|stable)", tail):
                return "stable_acute"
    if re.match(r"(?:acute uncomplicated|uncomplicated acute) (?:illness|injury|problem|condition|infection)\b", tail):
        if _positive(r"(?:acute uncomplicated|uncomplicated acute) (?:illness|injury|problem|condition|infection)", tail):
            return "acute_uncomplicated"
    if re.match(r"(?:self limited|minor) (?:illness|problem|condition|injury)\b", tail):
        if _positive(r"(?:self limited|minor) (?:illness|problem|condition|injury)", tail):
            return "self_limited"
    # Common clinician prose may establish a low problem without using an MDM
    # label. Both the acute course and expected uncomplicated recovery matter.
    if re.match(r"acute\b", tail) and re.search(r"\b(?:full recovery|complete recovery) (?:is )?expected\b", tail):
        if re.search(r"\b(?:without (?:functional impairment|lasting impairment|complications)|low risk of morbidity)\b", tail):
            return "acute_uncomplicated"
    return None


def _qualify(raw: object, index: int, bundle: VisitBundle, keys: set[str]) -> _Event:
    subject = raw.get("name") if isinstance(raw, dict) else None
    event = _Event(f"mdm-problem-{index + 1}", subject.strip() if _safe(subject, 160) else "Unclassified problem")
    if not isinstance(raw, dict) or not _safe(subject, 160):
        return event
    key = _key(subject)
    if not key or len(key) > 200:
        return event
    source = raw.get("source")
    if not isinstance(source, str):
        return event
    anchors = [_anchor(raw.get(field), source, bundle)
               for field in ("identity_quote", "addressed_quote", "status_quote")]
    extended = anchors[1] is None
    if extended:
        anchors[1] = _anchor(raw.get("addressed_quote"), source, bundle, care_block=True)
    evidence = tuple(dict.fromkeys(a for a in anchors if a))
    event = replace(event, key=key, evidence=evidence)
    identity, action, status = anchors
    if not identity or not action or not _affirmed(identity.quote, key):
        return event
    text = getattr(bundle, source)
    action_text = (_care_block_action(action, identity, key, text, keys) if extended
                   else _bound(action, identity, key, text, keys))
    if not action_text or not _addressed(action_text, key):
        return replace(event, reason="Current clinician evaluation or management of this problem is not established.")
    care = tuple(dict.fromkeys((identity, action)))
    # A source correction can invalidate work even when no complexity category
    # was established. Do this before the optional status branch.
    for start, end in _sentences(text):
        sentence = text[start:end].strip()
        if not _anchor(sentence, source, bundle):
            continue
        clean = _clean(sentence)
        attribution = re.sub(r"\b(?:correction|corrected|incorrect|retracted|"
            r"(?:not|never) (?:addressed|assessed|evaluated|managed|treated)|"
            r"(?:did|do|does) not (?:address|assess|evaluate|manage|treat))\b", "", clean)
        if _THIRD_ACTOR.search(clean) or _BAD_CONTEXT.search(attribution):
            continue
        adjacent = start >= action.end and not text[action.end:start].strip()
        within_action = start < action.end and end > action.start
        # A clipped extraction cannot hide a later same-object retraction.
        # Bound an anaphoric denial across a few neutral source sentences, but
        # stop at a paragraph, another act/actor, or another extracted problem.
        gap = text[action.end:start] if start >= action.end else ""
        nearby = (start >= action.end and len(gap) <= 1000 and "\n\n" not in gap
                  and len(_sentences(gap)) <= 3
                  and not (_ACTION.search(gap) or _BAD_CONTEXT.search(gap) or _THIRD_ACTOR.search(gap))
                  and not any(k != key and _mention(k, gap) for k in keys))
        if (within_action or nearby) and _care_denied(sentence, key):
            return replace(event, reason="Conflicting current work documentation requires clinician review.")
        if ((_mention(key, sentence) or adjacent or within_action)
                and re.search(r"\b(?:correction|corrected|incorrect|retracted)\b", clean)):
            return replace(event, reason="A documented correction prevents establishing current clinical work.")
        if _mention(key, sentence) and not _BAD_CONTEXT.search(clean):
            if any(_absent_predicate(tail) for _, _, tail in _identity_parts(sentence, key)):
                return replace(event, reason="Conflicting current problem identity requires clinician review.")
        passive_denial = _mention(key, sentence) and re.search(
            r"\b(?:not|never) (?:addressed|assessed|evaluated|managed|treated)\b", clean)
        active_denial = any(_destination_matches(key, match[1]) for match in re.finditer(
            r"\b(?:did|do|does) not (?:address|assess|evaluate|manage|treat)\s+([^;.!?]+)", clean))
        if passive_denial or active_denial:
            return replace(event, reason="Conflicting current work documentation requires clinician review.")
    event = replace(event, addressed=True, care_evidence=care,
                    reason="Current clinical work is established; problem complexity remains held for review.")
    if not status:
        return event
    status_text = _bound(status, identity, key, text, keys)
    if not status_text:
        return event
    category = _category(status_text, key, normalize(identity.quote))
    if not category:
        return event
    # Later explicit corrections or a conflicting current status cannot be
    # omitted merely by extracting an earlier affirmative sentence.
    for start, end in _sentences(text):
        sentence = text[start:end].strip()
        if sentence in {a.quote for a in evidence}:
            continue
        if not _anchor(sentence, source, bundle):
            continue
        adjacent = start >= status.end and not text[status.end:start].strip()
        if (_mention(key, sentence) or adjacent) and re.search(r"\b(?:correction|corrected|incorrect|retracted)\b", sentence, re.I):
            return replace(event, reason="Current clinical work is established; problem complexity remains held: a later correction prevents classification.")
        if (any(_absent_predicate(tail) for _, _, tail in _identity_parts(sentence, key))
                and not _BAD_CONTEXT.search(normalize(sentence))):
            return replace(event, reason="Current clinical work is established; problem complexity remains held: Conflicting documented status requires review.")
        if _mention(key, sentence) and _affirmed(sentence, key):
            other = _category(sentence, key, normalize(identity.quote))
            if other and other != category:
                return replace(event, reason="Current clinical work is established; problem complexity remains held: Conflicting documented status requires review.")
    return replace(event, status="counted", category=category,
                   reason="Current clinician work and condition-specific complexity are documented.")


def _deduplicate(events: list[_Event], bundle: VisitBundle) -> list[_Event]:
    groups: dict[str, list[int]] = {}
    for i, event in enumerate(events):
        if event.key and event.addressed:
            groups.setdefault(event.key, []).append(i)
    for members in groups.values():
        counted = [i for i in members if events[i].status == "counted"]
        categories = {events[i].category for i in counted}
        if len(categories) > 1:
            for i in counted:
                events[i] = replace(events[i], status="held", category=None,
                                    reason="Current clinical work is established; problem complexity remains held: Conflicting assessments of one condition require review.")
        keeper = (counted or members)[0]
        for i in members:
            if i != keeper:
                events[i] = replace(events[i], status="duplicate", category=None,
                                    reason="Another entry represents the same underlying condition.")
    # Only a documented causal relationship joins different named concepts.
    # Symptoms are not presumed independent when a source explicitly links them.
    relations = []
    for source in ("signed_note", "transcript"):
        text = getattr(bundle, source, "")
        if not isinstance(text, str) or len(text) > 500_000:
            continue
        for start, end in _sentences(text):
            sentence = text[start:end].strip()
            if re.search(r"\b(?:symptom of|due to|caused by|secondary to|part of)\b", sentence, re.I):
                anchor = _anchor(sentence, source, bundle)
                if anchor:
                    relations.append(normalize(anchor.quote))
    parents = list(range(len(events)))
    preferred = set()

    def group(i):
        while parents[i] != i:
            i = parents[i]
        return i

    for i, symptom in enumerate(events):
        if not symptom.addressed or symptom.status == "duplicate" or not _SYMPTOM.fullmatch(symptom.key):
            continue
        for j, condition in enumerate(events):
            if i == j or not condition.addressed or condition.status == "duplicate" or symptom.key == condition.key:
                continue
            for sentence in relations:
                first, second = _mention(symptom.key, sentence), _mention(condition.key, sentence)
                if not first or not second or not _affirmed(sentence, symptom.key):
                    continue
                middle = sentence[first.end():second.start()] if first.end() <= second.start() else ""
                if re.fullmatch(r"\s+(?:is |are )?(?:a symptom of|due to|caused by|secondary to|part of)\s+(?:the )?", middle):
                    parents[group(i)] = group(j)
                    preferred.add(j)
                elif re.fullmatch(r"\s+(?:may|might|could) be (?:a symptom of|due to|caused by|secondary to|part of)\s+(?:the )?", middle):
                    events[i] = replace(events[i], status="held", category=None,
                                        reason="Current clinical work is established; problem complexity remains held: independence from another addressed condition is not established.")
    merged = {}
    for i, e in enumerate(events):
        if e.addressed and e.status != "duplicate":
            merged.setdefault(group(i), []).append(i)
    rank = {"self_limited": 0, "acute_uncomplicated": 1, "stable_acute": 1,
            "stable_chronic": 1, **{k: 2 for k in _MODERATE}, **{k: 3 for k in _HIGH}}
    for members in merged.values():
        # One problem can have a serious presenting symptom and a subsequently
        # benign explanation. Retain its greatest SOURCE-established complexity
        # without multiplying the problem count or inferring an ICD diagnosis.
        keeper = max(members, key=lambda i: (rank.get(events[i].category, -1), i in preferred, -i))
        for i in members:
            if i != keeper:
                events[i] = replace(events[i], status="duplicate", category=None,
                                    reason="The source identifies this as part of another addressed condition.")
    return events


def qualify_problems(raw: object, bundle: VisitBundle, icd10=None) -> QualifiedProblems:
    """Qualify up to 20 exact-source candidates without requiring an ICD code.

    ``icd10`` is an optional future terminology dependency; no code lookup or
    network inference is needed to evaluate an undiagnosed addressed problem.
    Raw model flags and category labels are deliberately ignored.
    """
    source_hash = _fingerprint({s: getattr(bundle, s, "") for s in ("signed_note", "transcript")})
    fingerprint = _fingerprint(raw)
    if not isinstance(raw, list) or len(raw) > _MAX_EVENTS:
        return QualifiedProblems((_Event("mdm-problem-1", "Unclassified problem",
            reason="The bounded problem extraction is unavailable."),), fingerprint, source_hash)
    keys = {_key(e["name"]) for e in raw if isinstance(e, dict) and _safe(e.get("name"), 160)}
    events = [_qualify(e, i, bundle, keys) for i, e in enumerate(raw)]
    return QualifiedProblems(tuple(_deduplicate(events, bundle)), fingerprint, source_hash)
