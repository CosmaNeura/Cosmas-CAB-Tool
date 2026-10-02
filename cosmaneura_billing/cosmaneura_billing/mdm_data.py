"""Source-qualified office E/M data work, independent of model labels/counts.

This deliberately bounded attestation reader is not a general English or CPT
parser. Unknown terminology/attribution remains reviewable. The registry and
these evidence policies implement conservative supported paths; a held event
is not a declaration that the service is ineligible under a payer's rules.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re

from .knowledge.mdm_tests import TestIdentity, resolve_test, test_mentions
from .schemas import Evidence, VisitBundle

VERSION = "office-mdm-data.2026-10-02.v3"
ACTIONS = ("test_order", "test_result_review", "test_considered",
           "external_note_review", "independent_historian",
           "independent_interpretation", "external_discussion")
_MAX_EVENTS = 40
_BOUNDARY = re.compile(r"(?<!Dr\.)(?<!Mr\.)(?<!Ms\.)(?<=[.!?])\s+|\n")
_SPEAKER = re.compile(r"^\s*(?:physician|qhp|provider|doctor)\s*:", re.I)
_BAD_ASSERTION = re.compile(
    r"\b(?:hypothetical|example|template|incorrect|retracted|correction|"
    r"not\s+(?:actually\s+)?(?:ordered|placed|reviewed|read|obtained|interpreted|discussed)|"
    r"(?:did|do|does|have|has)\s+not|never|unable\s+to\s+(?:review|order)|"
    r"(?:patient|pt)\s*(?::|says?\b|said\b|reports?\b|reported\b|states?\b|stated\b)|"
    r"per\s+(?:the\s+)?patient|patient[- ]reported|"
    r"(?:will|would|might|may)\s+(?:order|review|read|interpret|discuss|obtain)|"
    r"(?:plan|planned|planning|intend|intended|recommend|recommended)\s+to\b)", re.I)
_ORDER = re.compile(r"\b(?:order(?:ed)?|have\s+ordered)\b", re.I)
_REVIEW = re.compile(r"\b(?:reviewed|read|analyzed)\b", re.I)
_CURRENT = re.compile(r"\b(?:today|this\s+(?:visit|encounter)|current\s+(?:visit|encounter))\b", re.I)
_HISTORICAL = re.compile(r"\b(?:yesterday|last\s+(?:week|month|year)|"
    r"(?:prior|previous)\s+(?:visit|encounter)|previously|historically)\b", re.I)
_THIRD_PARTY = re.compile(r"\b(?:nurse|staff|assistant|patient|mother|father|spouse|"
    r"radiologist|cardiologist|endocrinologist|Dr\.?\s+[A-Z][a-z]+)\s+"
    r"(?:has\s+|had\s+)?(?:ordered|reviewed|read|interpreted|discussed)\b", re.I)
_SEPARATE = re.compile(r"\bseparately\s+(?:report(?:ed|ing|s)?|bill(?:ed|ing|s)?)\b", re.I)
_NOT_SEPARATE = re.compile(r"\b(?:I\s+(?:(?:am|will)\s+not|did\s+not|do\s+not)|"
    r"not|without)\s+separately\s+(?:report(?:ing|ed)?|bill(?:ing|ed)?)\b|"
    r"\b(?:interpretation|service)\s+(?:is|was|will\s+be)\s+not\s+separately\s+(?:reported|billed)\b", re.I)
_SPECIALTIES = re.compile(r"\b(?:endocrinology|cardiology|neurology|nephrology|"
    r"urology|rheumatology|dermatology|radiology|oncology|hematology|psychiatry|"
    r"pediatrics|family medicine|internal medicine|obstetrics(?: and gynecology)?|"
    r"gynecology|orthopedics|gastroenterology|pulmonology|reproductive endocrinology)\b", re.I)
_ORGANIZATION = re.compile(r"\b(?:from|at|of)\s+(?:the\s+)?"
    r"((?:[A-Z][\w'-]*\s+){1,6}(?:Clinic|Hospital|Center|Practice|Group|Laboratory))\b")
_HISTORIAN = re.compile(r"\b(?:mother|father|parent|spouse|wife|husband|daughter|son|"
    r"caregiver|guardian|grandmother|grandfather|sister|brother)\b", re.I)
_UNFINISHED = re.compile(r"\b(?:pending|awaiting|incomplete|unconfirmed|hypothetical|"
    r"not yet|not completed|planned|could|might|possibly|uncertain whether)\b", re.I)


@dataclass(frozen=True)
class _Anchor:
    source: str
    quote: str
    start: int
    end: int


@dataclass(frozen=True)
class _Event:
    id: str
    action: str
    subject: str
    status: str = "held"
    category: str | None = None
    reason: str = "Exact identity and completed-work evidence require review."
    evidence: tuple[_Anchor, ...] = ()
    test: TestIdentity | None = None
    external_source: tuple[str, str] | None = None


def _fingerprint(events: object) -> str:
    try:
        encoded = json.dumps(events, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        return ""
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class QualifiedMDMData:
    events: tuple[_Event, ...]
    extraction_sha256: str
    source_sha256: str

    def matches(self, events: object) -> bool:
        return bool(self.extraction_sha256) and self.extraction_sha256 == _fingerprint(events)

    @property
    def public(self) -> dict:
        counted = [e for e in self.events if e.status == "counted"]
        historian = any(e.category == "historian" for e in counted)
        units = sum(e.category == "category_one" for e in counted) + int(historian)
        interpretation = any(e.category == "interpretation" for e in counted)
        discussion = any(e.category == "discussion" for e in counted)
        categories = (["category_one"] if units >= 3 else []) + (
            ["interpretation"] if interpretation else []) + (["discussion"] if discussion else [])
        level = ("high" if len(categories) >= 2 else "moderate" if categories else
                 "low" if units - int(historian) >= 2 or historian else "straightforward")
        return {
            "rule_version": VERSION, "level": level, "category_one_units": units,
            "independent_historian": historian, "independent_interpretation": interpretation,
            "external_discussion": discussion, "qualifying_categories": categories,
            "events": [{"id": e.id, "action": e.action, "subject": e.subject,
                "status": e.status, "category": e.category, "reason": e.reason,
                "evidence": [{"source": a.source, "quote": a.quote} for a in e.evidence]}
                for e in self.events],
        }

    @property
    def level(self) -> str:
        return self.public["level"]

    def evidence(self) -> list[Evidence]:
        return [Evidence(a.source, a.quote, f"MDM data: {e.id}")
                for e in self.events if e.status == "counted" for a in e.evidence]


def _sentences(text: str):
    ends = [0, *(m.end() for m in _BOUNDARY.finditer(text)), len(text)]
    return [(a, b, text[a:b].strip()) for a, b in zip(ends, ends[1:]) if text[a:b].strip()]


_OWNER_LABEL = re.compile(r"(?:^[ \t]*|;[ \t]*)([A-Za-z][A-Za-z /'-]{0,60}):", re.M)
_CURRENT_OWNER = re.compile(
    r"(?:assessment and plan|a/p|(?:current\s+)?(?:clinician|physician|qhp|provider|doctor)"
    r"(?:\s+(?:note|assessment(?: and plan)?|plan))?)", re.I)


def _excluded_owner(label: str) -> bool:
    label = label.strip()
    return bool(
        re.search(r"\b(?:patient|family|caregiver|reported|quotation|example|template)\b", label, re.I) or
        re.fullmatch(r"(?:nurse|nursing|staff|medical assistant)(?:\s+(?:notes?|records?|report|documentation))?", label, re.I) or
        re.search(r"\b(?:outside|external|imported|copied|quoted|prior|previous)\b", label, re.I) and
        re.search(r"\b(?:notes?|records?|clinician|physician|report|consultation|orders?)\b", label, re.I))


def _other_owner_context(text: str, start: int, context: str) -> bool:
    excluded = False
    for heading in _OWNER_LABEL.finditer(text[:start]):
        if _excluded_owner(heading[1]):
            excluded = True
        elif _CURRENT_OWNER.fullmatch(heading[1].strip()):
            excluded = False
        # A subordinate "Tests:"/"Orders:" label cannot terminate another
        # actor's record. Only a current-clinician or A/P reset does so.
    inline = list(_OWNER_LABEL.finditer(context))
    if inline and not context[:inline[0].start()].strip() and _CURRENT_OWNER.fullmatch(inline[0][1].strip()):
        excluded = False
    return excluded or any(_excluded_owner(h[1]) for h in inline)


def _anchor(quote: object, source: str, bundle: VisitBundle) -> _Anchor | None:
    if source not in ("signed_note", "transcript") or not isinstance(quote, str) or not quote.strip() or len(quote) > 1000:
        return None
    text = getattr(bundle, source)
    if not isinstance(text, str) or len(text) > 500_000 or text.count(quote) != 1:
        return None
    pos = text.index(quote)
    contexts = [(a, b) for a, b, _ in _sentences(text) if max(a, pos) < min(b, pos + len(quote))]
    if not contexts:
        return None
    a, b = contexts[0][0], contexts[-1][1]
    # Expand model fragments to full assertions; the model cannot clip a
    # negation, patient attribution, or future-tense prefix off an action.
    context = text[a:b].strip()
    if (len(context) > 1000 or _other_owner_context(text, a, context) or
            (source == "transcript" and not _SPEAKER.match(context))):
        return None
    start = a + len(text[a:b]) - len(text[a:b].lstrip())
    return _Anchor(source, context, start, start + len(context))


def _test_in(test: TestIdentity, text: str) -> bool:
    return any(t.key == test.key for t in test_mentions(text))


def _external_identity(text: str, bundle: VisitBundle) -> tuple[str, str] | None:
    organizations = {re.sub(r"\s+", " ", m[1]).casefold() for m in _ORGANIZATION.finditer(text)}
    specialties = {m[0].casefold() for m in _SPECIALTIES.finditer(text)}
    if len(organizations) > 1 or len(specialties) > 1:
        return None
    org = next(iter(organizations), "")
    specialty = next(iter(specialties), "")
    outside = bool(re.search(r"\b(?:outside|external|different\s+(?:practice|organization|group))\b", text, re.I))
    different_specialty = bool(specialty and specialty not in bundle.provider_specialty.casefold())
    # A name alone neither establishes externality nor creates a new source.
    if not (org or specialty) or not (outside or different_specialty):
        return None
    return org, specialty


def _same_source(a: tuple[str, str], b: tuple[str, str]) -> bool:
    # Unknown organization/specialty cannot manufacture a distinct source.
    return (not a[0] or not b[0] or a[0] == b[0]) and (not a[1] or not b[1] or a[1] == b[1])


# These are grammatical contexts, not additional terminology aliases. A test
# name still resolves as a complete registry identity before its venue/purpose
# is accepted. In particular, "culture for fungi" and unknown methods do not
# become generic culture by discarding everything after "for".
_ORDER_CONTEXT = re.compile(
    r"(?:today|now|by me|for this (?:visit|encounter))\b|"
    r"(?:through|via|at)\s+(?:(?:a|an|the)\s+)?"
    r"(?:(?:local|outside|external|reference)\s+)?"
    r"(?:laboratory|lab|imaging cent(?:er|re)|radiology department|services)\b|"
    r"for\s+(?:(?:prompt|routine|urgent)\s+)?(?:outpatient\s+)?"
    r"(?:assessment|evaluation|follow-up)\b|"
    r"before\s+(?:the\s+)?(?:first|next)\s+antibiotic dose\b", re.I)
_ORDER_CONTEXT_START = re.compile(r"\s+(?=today\b|now\b|by\b|through\b|via\b|at\b|for\b|before\b|because\b)", re.I)
_ORDER_SEPARATOR = re.compile(r"(?:(?<!\d),|,(?!\d))\s*(?:and\s+)?|\s+and\s+", re.I)
_PENDING_RESULT = re.compile(
    r"\s*(?:;|,?\s+but)\s+(?:the\s+)?results?\s+(?:are|is)\s+"
    r"(?:not yet available|pending|not back yet)\s*$", re.I)


def _order_context_supported(text: str) -> bool:
    remaining = text.strip().rstrip(".").strip()
    while remaining:
        # A reason is not a method/specimen qualifier. It must follow the
        # explicit noun phrase, and cannot contain another order-list item.
        if re.fullmatch(r"because\s+[^:;.!?]+", remaining, re.I):
            return True
        match = _ORDER_CONTEXT.match(remaining)
        if not match:
            return False
        remaining = remaining[match.end():].strip()
    return True


def _ordered_noun(text: str, specimen: str | None) -> TestIdentity | None:
    name = re.sub(r"^(?:(?:an?|the|external|repeat|new)\s+)+", "", text.strip(), flags=re.I)
    parts = _ORDER_CONTEXT_START.split(name, maxsplit=1)
    name = parts[0].strip()
    if len(parts) > 1 and not _order_context_supported(parts[1]):
        return None
    # This expansion is licensed by an explicit shared specimen in the same
    # completed assertion. Urinalysis elsewhere is not a specimen attestation.
    if specimen == "urine" and name.casefold() == "culture":
        name = "urine culture"
    return resolve_test(name)


def _ordered_objects(text: str, specimen: str | None = None) -> tuple[TestIdentity, ...]:
    """Parse a complete coordinated list, preserving compound test names.

    Longest complete phrases win: "hemoglobin and hematocrit" is one registry
    identity, not two units. Unknown objects or leftover clinical modifiers
    invalidate the list instead of donating an embedded known test.
    """
    text = text.strip().rstrip(".").strip()
    separators = list(_ORDER_SEPARATOR.finditer(text))
    if not text or text.endswith(",") or len(separators) >= _MAX_EVENTS:
        return ()
    cache: dict[int, tuple[TestIdentity, ...]] = {}

    def parse(start: int) -> tuple[TestIdentity, ...]:
        if start in cache:
            return cache[start]
        ends = [(m.start(), m.end()) for m in separators if m.start() >= start]
        for end, following in reversed([*ends, (len(text), len(text))]):
            identity = _ordered_noun(text[start:end], specimen)
            if identity is None:
                continue
            if following == len(text):
                cache[start] = (identity,)
                return cache[start]
            rest = parse(following)
            if rest:
                cache[start] = (identity, *rest)
                return cache[start]
        cache[start] = ()
        return ()

    return parse(0)


def _order_body(text: str) -> str:
    text = _SPEAKER.sub("", text).strip()
    return re.sub(rf"^(?:{_CURRENT_OWNER.pattern}|plan|data|tests)\s*:\s*", "", text, flags=re.I)


def _completed_order_objects(text: str) -> tuple[TestIdentity, ...]:
    """Read current active/passive order assertions; never infer from a plan.

    Negation, authorship, timing and corrections are checked on the complete
    source assertion by the caller. This reader supplies only object binding.
    """
    objects: list[TestIdentity] = []
    for _, _, sentence in _sentences(text):
        body = _order_body(sentence).rstrip(".").strip()
        body = _PENDING_RESULT.sub("", body)
        # A contrasting clause may retract or restrict an order. Only the
        # closed result-pending exception above is removed.
        if re.search(r"\bbut\b", body, re.I):
            continue
        shared = re.fullmatch(
            r"(?:obtain|collect|obtained|collected)\s+(?:a|the)\s+urine\s+"
            r"(?:specimen|sample)\s+for\s+([^;]+);\s*"
            r"(?:the|these)\s+tests\s+(?:have been|were)\s+ordered(.*)", body, re.I)
        if shared and _order_context_supported(shared[2]):
            objects.extend(_ordered_objects(shared[1], specimen="urine"))
            continue
        for clause in body.split(";"):
            clause = _order_body(clause)
            active = re.fullmatch(
                r"(?:(?:I|we)\s+(?:(?:have|personally|today)\s+){0,2})?"
                r"(?:order|ordered)\s+(.+)", clause, re.I)
            placed = re.fullmatch(
                r"(?:the\s+)?orders\s+placed\s+(?:today|this visit|this encounter)\s+"
                r"(?:are|include)\s+(.+)", clause, re.I)
            passive = re.fullmatch(
                r"(.+?)\s+(?:was|were|has been|have been)\s+ordered(.*)", clause, re.I)
            if active or placed:
                objects.extend(_ordered_objects((active or placed)[1]))
            elif passive and _order_context_supported(passive[2]):
                objects.extend(_ordered_objects(passive[1]))
    return tuple(dict.fromkeys(objects))


def _action_supported(text: str, action: str) -> bool:
    checked = _NOT_SEPARATE.sub("", text)
    if action == "test_considered":
        # A shared decision against testing is a positive considered-test
        # action. This exception cannot remove negation of consideration.
        checked = re.sub(r"\bnot ordered\b", "", checked, flags=re.I)
    if _BAD_ASSERTION.search(checked) or _THIRD_PARTY.search(checked) or "?" in checked:
        return False
    # A prior report may be reviewed now; a prior act of review is not new
    # work. Off-date discussions must explicitly relate to this encounter.
    if _HISTORICAL.search(checked) and not _CURRENT.search(checked):
        return False
    if action == "test_order":
        return bool(_completed_order_objects(checked)) and not re.search(r"\b(?:consider|considered|declined|deferred|cancelled|canceled|if|unless)\b", checked, re.I)
    if action in ("test_result_review", "external_note_review"):
        return bool(_REVIEW.search(checked))
    if action == "test_considered":
        return bool(re.search(r"\bconsidered\b", checked, re.I) and
                    re.search(r"\b(?:shared decision[- ]making|shared discussion|discussed (?:the )?(?:risks|benefits|options))\b", checked, re.I) and
                    re.search(r"\b(?:patient declined|patient chose (?:to )?(?:defer|forgo)|decided (?:together )?(?:against|not to))\b", checked, re.I))
    if action == "independent_interpretation":
        return bool(re.search(r"\bindependently (?:interpreted|reviewed)\b", checked, re.I))
    if action == "external_discussion":
        return bool(re.search(r"\b(?:discussed|spoke|exchanged)\b", checked, re.I) and
                    re.search(r"\b(?:management|treatment|diagnosis|interpretation|findings|plan|care)\b", checked, re.I) and
                    not re.search(r"\b(?:sent|left|awaiting|pending|referral|message sent)\b", checked, re.I))
    return bool(re.search(r"\b(?:history (?:was )?(?:obtained|provided|confirmed)|obtained (?:additional )?history|provided (?:additional |confirmatory )?history)\b", checked, re.I))


def _actor_bound(text: str, action: str, test: TestIdentity | None) -> bool:
    """Accept explicit clinician statements or tightly bounded passive tests.

    Finding a verb anywhere in a sentence would let reported speech and a
    visiting clinician's work become the E/M clinician's action.
    """
    text = _SPEAKER.sub("", text).strip()
    text = re.sub(r"^(?:assessment and plan|plan|data|tests)\s*:\s*", "", text, flags=re.I)
    verb = re.search(r"\b(?:order(?:ed)?|reviewed|read|analyzed|considered|"
                     r"independently (?:interpreted|reviewed)|discussed|spoke|exchanged|"
                     r"history (?:was )?(?:obtained|provided|confirmed)|obtained (?:additional )?history)\b", text, re.I)
    if not verb:
        return False
    prefix = text[:verb.start()].strip()
    if re.fullmatch(r"(?:(?:I|we)(?:\s+(?:have|personally|today)){0,2})?", prefix, re.I):
        return True
    if test and action in ("test_order", "test_result_review"):
        prefix = re.sub(r"^(?:the|an?)\s+", "", prefix, flags=re.I)
        prefix = re.sub(r"\s+(?:(?:results?|report|images?)\s+)?(?:was|were|has been|have been)$", "", prefix, flags=re.I)
        return resolve_test(prefix) == test
    return False


def _test_action_clause(test: TestIdentity, text: str, action: str) -> str | None:
    """Read one explicit test action/list, not arbitrary sentence co-occurrence."""
    if action == "test_order":
        return text if test in _completed_order_objects(text) else None
    for clause in re.split(r";|\bbut\b|(?<=[.!?])\s+", text, flags=re.I):
        if not _test_in(test, clause) or not _actor_bound(clause, action, test):
            continue
        if action in ("test_considered", "independent_interpretation"):
            # The named test must follow the actual action verb before any
            # findings/shared-decision clause. Qualifiers are checked on the
            # complete assertion, so this cannot clip an exclusion.
            verb = re.search(r"\b(?:considered|independently (?:interpreted|reviewed))\s+", clause, re.I)
        else:
            verb = (_ORDER if action == "test_order" else _REVIEW).search(clause)
        if not verb:
            continue
        tail = clause[verb.end():].strip()
        if re.match(r"(?:yesterday|last\s+(?:week|month|year)|previously)\b", tail, re.I):
            continue
        # Passive 'CBC was reviewed' has no test object after the verb.
        if not _test_in(test, tail):
            prefix = re.sub(r"^(?:the|an?)\s+", "", clause[:verb.start()].strip(), flags=re.I)
            prefix = re.sub(r"\s+(?:(?:results?|report)\s+)?(?:was|were|has been|have been)$", "", prefix, flags=re.I)
            if resolve_test(prefix) == test:
                return clause
            continue
        tail = re.split(r"\b(?:today|now|through|because|for|at|with findings)\b|:", tail, maxsplit=1, flags=re.I)[0]
        # Every item in the explicit object list must be a known full test
        # phrase. 'TSH and free T4 is normal' is not an order for free T4.
        pieces = re.split(r",\s*(?:and\s+)?|\s+and\s+", tail.strip().rstrip("."), flags=re.I)
        objects = []
        for piece in pieces:
            name = re.sub(r"^(?:(?:an?|the|external|repeat|new)\s+)+", "", piece.strip(), flags=re.I)
            objects.append(resolve_test(name))
        if test in objects and all(objects):
            return clause
    return None


def _reporting_attested(action: _Anchor, qualification: _Anchor | None,
                        bundle: VisitBundle, kind: str, test: TestIdentity | None = None) -> bool:
    if not qualification or not _NOT_SEPARATE.search(qualification.quote):
        return False
    # Only an affirmative service-specific nonbilling statement qualifies;
    # 'if I do not bill ...' and a promise about another test do not.
    clause = re.split(r";|(?<=[.!?])\s+", qualification.quote)[-1].strip().rstrip(".")
    attestation = re.fullmatch(rf"I (?:(?:will|do|did) not separately (?:report|bill)|"
        rf"am not separately (?:reporting|billing)) (?:this|the|its) (?:(.*?) )?({kind})", clause, re.I)
    passive = re.fullmatch(rf"(?:This|The) ({kind}) (?:is|was|will be) not separately (?:reported|billed)(?: by me)?", clause, re.I)
    if not attestation and not passive:
        return False
    if attestation and attestation[1] and (test is None or resolve_test(attestation[1]) != test):
        return False
    if qualification == action:
        return True
    # An adjacent explicit 'this interpretation/discussion' attestation is
    # linked. A distant no-charge sentence about another service is not.
    if qualification.start < action.end:
        return False
    between = getattr(bundle, action.source)[action.end:qualification.start]
    return not between.strip()


def _interpretation_findings(text: str) -> bool:
    """A bounded clinical finding vocabulary, not punctuation-as-evidence."""
    if _UNFINISHED.search(text):
        return False
    pieces = re.split(r":|\b(?:showed|shows|demonstrated|demonstrates|found|findings)\b", text, flags=re.I)
    if len(pieces) < 2:
        return False
    findings = pieces[-1].strip()
    if re.search(r"\b(?:ordered|evaluate|evaluation|indication|rule out|assess for|"
                 r"look for|determine|question of|not assessed|not evaluated)\b", findings, re.I):
        return False
    # The finding must begin with a clinical observation/absence, not a
    # statement of why the test was ordered or that interpretation is complete.
    return bool(re.match(r"(?:(?:no(?: evidence of)?|without|small|large|mild|moderate|"
        r"severe|acute|new|focal|pleural|pulmonary|bilateral|right|left)\s+)*"
        r"(?:normal|sinus|rhythm|rate|tachycardia|bradycardia|atrial|"
        r"interval|qt|qtc|st[- ]segment|ischemi[ac]|axis|infiltrate|consolidation|"
        r"opacity|opacities|effusion|pneumothorax|cardiomegaly|atelectasis|edema|"
        r"fracture|dislocation|alignment|joint[- ]space|degenerative|osteophytes|"
        r"intrauterine|endometri(?:um|al)|ovary|ovaries|cyst|gestational sac|"
        r"hydronephrosis|nodule|mass|lesion)\b", findings, re.I))


def _reporting_conflict(test: TestIdentity, source: str, action: _Anchor) -> bool:
    for start, _, sentence in _sentences(source):
        for report in _SEPARATE.finditer(sentence):
            prefix, tail = sentence[:report.start()], sentence[report.end():]
            prefix = re.split(r";|\bbut\b|\band(?=\s+I\b)", prefix, flags=re.I)[-1]
            # Scope negation to THIS reporting verb. A no-charge statement
            # about another test cannot erase an earlier affirmative charge.
            if re.search(r"\b(?:not|without)\s*$", prefix, re.I):
                continue
            object_text = re.split(r";|\bbut\b|\band I\b", tail, maxsplit=1, flags=re.I)[0].strip()
            # Reporting can name the test directly or call it a service. The
            # same closed venue/purpose grammar used for orders preserves its
            # identity here; otherwise a routed order could evade exclusion.
            reported_object = re.sub(r"\s+(?:interpretation|service)(?=\s|$)", "", object_text, count=1, flags=re.I)
            passive = re.search(r"\s+(?:(?:interpretation|service)\s+)?(?:is|was|has been|will be)\s*$", prefix, re.I)
            passive_object = prefix[:passive.start()].strip() if passive else ""
            named = (_test_in(test, object_text) or test in _ordered_objects(reported_object) or
                     bool(passive) and (test in _ordered_objects(passive_object)))
            pronoun = action.end <= start <= action.end + 5 and re.match(
                r"(?:its|this|the)\s+(?:interpretation|service)\b", object_text, re.I)
            if not named and not pronoun:
                continue
            other_reporter = (re.search(r"\b(?:radiologist|another (?:clinician|physician|specialty))\b", prefix, re.I) or
                              re.search(r"\bby (?:the )?(?:radiologist|another (?:clinician|physician|specialty))\b", tail, re.I))
            if other_reporter and not re.search(r"\bI\b", prefix) and not re.search(r"\bby me\b", tail, re.I):
                continue
            return True
    return False


def _external_note_clause(text: str, bundle: VisitBundle) -> str | None:
    clauses = [c for _, _, sentence in _sentences(text)
               for c in re.split(r";|\bbut\b", sentence, flags=re.I)]
    for clause in clauses:
        if not _actor_bound(clause, "external_note_review", None):
            continue
        verb = _REVIEW.search(clause)
        tail = clause[verb.end():].strip() if verb else ""
        tail = re.sub(r"^the\s+", "", tail, flags=re.I)
        specialty = _SPECIALTIES.pattern
        direct = re.match(rf"(?:(?:outside|external)\s+)?(?:{specialty}\s+)?(?:notes?|records?)\b", tail, re.I)
        embedded = re.search(r"\bin\s+(?:the\s+)?(?:outside|external)\b.*\b(?:note|record)\b", tail, re.I)
        if (direct or embedded) and _external_identity(clause, bundle):
            return clause
    return None


def _safe_subject(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or re.search(r"[\x00-\x1f\x7f\ud800-\udfff]", value):
        return "Unclassified data"
    return value.strip()[:160]


def _historian_need_supported(text: str) -> bool:
    # Exact history words do not affirm that an independent historian was needed.
    # Past-tense patient inability is not a hypothetical clinician action.
    current_inability = re.sub(r"\bpatient\s+could not\s+(recall|remember|provide|give)\b",
                               r"patient was unable to \1", text, flags=re.I)
    if ("?" in text or _BAD_ASSERTION.search(text) or _UNFINISHED.search(current_inability)
            or _HISTORICAL.search(text) or re.search(r"\b(?:if|unless|false|untrue)\b", text, re.I)):
        return False
    if re.search(
        r"\b(?:no|without)\s+(?:(?:a|any|additional)\s+)?(?:need|requirement|"
        r"(?:confirmatory|conflicting|independent)\s+history)\b|"
        r"\bhistory\s+(?:(?:is|was)\s+)?(?:not|never)\s+(?:needed|required|necessary)\b|"
        r"\bhistory\s+(?:(?:is|was)\s+)?unnecessary\b", text, re.I):
        return False
    affirmative = (
        r"\bpatient\s+(?:(?:is|was)\s+)?(?:unable to|cannot|can't|can’t)\s+(?:provide|give)\s+"
        r"(?:a\s+)?(?:(?:complete|reliable|accurate|full|medical|and)\s+)*history\b|"
        r"\b(?:confirmatory|conflicting)\s+history\s+(?:(?:is|was)\s+)?(?:needed|required|necessary)\b|"
        r"\b(?:I|we)\s+(?:need|needed|require|required)\s+(?:confirmatory|conflicting)\s+history\b"
    )
    for _, _, sentence in _sentences(current_inability):
        for clause in re.split(r";|\bbut\b", sentence, flags=re.I):
            match = re.search(affirmative, clause, re.I)
            if match and not re.search(r"\b(?:no|not|never|without|denies|denied)\b", clause[:match.start()], re.I):
                return True
    return False


def _qualify(raw: dict, index: int, bundle: VisitBundle) -> _Event:
    action = raw.get("action") if raw.get("action") in ACTIONS else "unclassified"
    subject = _safe_subject(raw.get("subject"))
    event = _Event(f"mdm-data-{index + 1}", action, subject)
    source = raw.get("source")
    action_anchor = _anchor(raw.get("action_quote"), source, bundle)
    identity_anchor = _anchor(raw.get("identity_quote"), source, bundle)
    qualification_anchor = _anchor(raw.get("qualification_quote"), source, bundle)
    anchors = tuple(dict.fromkeys(a for a in (identity_anchor, action_anchor, qualification_anchor) if a))
    event = replace(event, evidence=anchors)
    if not action_anchor or not identity_anchor or action == "unclassified" or subject == "Unclassified data":
        return event
    if max(a.end for a in anchors) - min(a.start for a in anchors) > 1800:
        return replace(event, reason="Identity, action and eligibility evidence are too far apart to link automatically.")
    text = action_anchor.quote
    if not _action_supported(text, action):
        return replace(event, reason="A completed clinician action for this encounter could not be verified; review the source.")
    test = resolve_test(subject) if action.startswith("test_") or action == "independent_interpretation" else None
    if not (action.startswith("test_") or action == "independent_interpretation") and not _actor_bound(text, action, test):
        return replace(event, reason="The action could not be attributed to this encounter's clinician; review the complete assertion.")
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text) and not _CURRENT.search(text):
        return replace(event, reason="A dated data action needs explicit current-encounter relevance before automated credit.")
    # A following correction cannot be hidden by selecting only the initial
    # positive sentence. Explicit contradiction of this action holds it.
    source_text = getattr(bundle, source)
    following = source_text[action_anchor.end:action_anchor.end + 350].strip()
    if re.match(r"(?:Correction|Actually|This (?:order|review|interpretation|discussion) (?:did not|was not)|That (?:order|review) was (?:cancelled|canceled))\b", following, re.I):
        return replace(event, reason="A linked correction or cancellation requires review.")
    joined = " ".join(a.quote for a in anchors)
    if action.startswith("test_") or action == "independent_interpretation":
        event = replace(event, test=test)
        if test is None:
            return replace(event, reason="Test identity or variant is outside the reviewed registry; no extra unit inferred.")
        identity_bound = _test_in(test, identity_anchor.quote) or (
            action == "test_order" and test in _completed_order_objects(identity_anchor.quote))
        if not identity_bound or not _test_action_clause(test, text, action):
            return replace(event, reason="The named test is not explicitly linked to the documented action.")
        prior_context = joined + " " + following
        if action == "test_result_review" and re.search(r"\b(?:ordered|counted)\b.*\b(?:previous|prior)\s+encounter\b", prior_context, re.I):
            return replace(event, reason="A prior encounter may already include this ordered test; review its order/result allocation.")
        # Tests merely copied inside an external clinician note share its
        # source credit; viewing that note does not establish a separate review.
        if re.search(r"\b(?:in|within|from)\s+(?:the\s+)?(?:outside|external).*\bnote\b", text, re.I) and action == "test_result_review":
            return replace(event, reason="Test information embedded in an external note shares the note's source credit.")
        if test.has_professional_component:
            if _reporting_conflict(test, source_text, action_anchor):
                return replace(event, reason="Separate professional-component reporting or its attribution requires review; excluded from automated data credit.")
        if action == "independent_interpretation":
            if not test.has_professional_component:
                return replace(event, reason="Routine laboratory-result analysis does not establish independent professional interpretation.")
            if not _reporting_attested(action_anchor, qualification_anchor, bundle, "interpretation", test):
                return replace(event, reason="Confirm that this clinician does not separately report the interpretation.")
            if not _interpretation_findings(text):
                return replace(event, reason="An independent interpretation with actual findings requires review.")
            return replace(event, status="counted", category="interpretation", reason="Documented independent interpretation; not separately reported by this clinician.")
        return replace(event, status="counted", category="category_one", reason="One documented test action; repeated orders/reviews and overlapping components count once.")
    if action == "independent_historian":
        historian = _HISTORIAN.search(identity_anchor.quote)
        if not historian or not re.search(rf"\b{historian[0]}\b", text, re.I):
            return replace(event, reason="A distinct historian providing history could not be linked to the action.")
        if not qualification_anchor or not _historian_need_supported(qualification_anchor.quote):
            return replace(event, reason="Historian need or confirmatory history requires human review under CAB's evidence policy.")
        if re.search(r"\b(?:translator|interpreter|translation only|present only)\b", joined, re.I):
            return replace(event, reason="Presence or translation alone does not establish an independent historian.")
        return replace(event, status="counted", category="historian", reason="Independent history linked to documented need; all historians together contribute one unit.")
    # The source must be in the ACTION assertion itself, not borrowed from an
    # unrelated available record or nearby source-identity quote.
    external = _external_identity(text, bundle)
    if external is None:
        return replace(event, reason="External source organization or distinct specialty could not be established; clinician names alone do not add units.")
    if action == "external_note_review":
        if not _external_note_clause(text, bundle) or re.search(r"\b(?:my|our|own)\s+(?:own\s+)?(?:note|record)\b", text, re.I):
            return replace(event, reason="Review of an external note or record was not established.")
        return replace(event, status="counted", category="category_one", external_source=external,
                       reason="External note reviewed; records from the same source contribute one unit.")
    participant = re.search(r"\bwith\s+([^;.!?]+)", text, re.I)
    if not participant or _HISTORIAN.search(participant[1]) or re.search(r"\bpatient\b", participant[1], re.I) or not _external_identity(participant[1], bundle):
        return replace(event, reason="The external clinician must be the documented participant in the direct exchange.")
    if not _reporting_attested(action_anchor, qualification_anchor, bundle, "(?:discussion|consultation)") or _SEPARATE.search(_NOT_SEPARATE.sub("", text)):
        return replace(event, reason="Confirm that the interprofessional discussion is not separately reported before automated credit.")
    if _UNFINISHED.search(text):
        return replace(event, reason="The direct exchange must be complete and relevant to this encounter; review the source.")
    return replace(event, status="counted", category="discussion", external_source=external,
                   reason="Completed external management or interpretation discussion used for this encounter.")


def qualify_mdm_data(raw: object, bundle: VisitBundle, legacy: object = None) -> QualifiedMDMData:
    """Only source-qualified events can set the data element or its ceiling."""
    if not isinstance(raw, list):
        raw = []
    events = [_qualify(e, i, bundle) if isinstance(e, dict) else
              _Event(f"mdm-data-{i + 1}", "unclassified", "Unclassified data")
              for i, e in enumerate(raw[:_MAX_EVENTS])]
    if len(raw) > _MAX_EVENTS:
        events = [replace(e, status="held", category=None,
                         reason="Extraction exceeds the supported event limit; review the complete source.") for e in events]
    # Old free-form categories carry no counting authority. Keep the omission
    # visible instead of silently accepting labels as unique work.
    if not events and isinstance(legacy, list):
        for i, item in enumerate(legacy[:_MAX_EVENTS]):
            if isinstance(item, dict):
                quote = _anchor(item.get("quote"), "signed_note", bundle)
                events.append(_Event(f"mdm-data-{i + 1}", "unclassified", _safe_subject(item.get("item")),
                    reason="Legacy category labels do not establish unique work; structured source review is required.",
                    evidence=(quote,) if quote else ()))
    interpretations = [e.test for e in events if e.status == "counted" and e.category == "interpretation"]
    counted: list[_Event] = []
    result = []
    # Stable source order and broader panels first prevent extraction ordering
    # from changing counts or turning a panel into all of its components.
    priority = lambda e: (0 if e.category == "interpretation" else 1,
        -len(e.test.components) if e.test else 0,
        min((a.start for a in e.evidence), default=500_001), e.action, e.subject, e.id)
    decisions = {}
    for e in sorted(events, key=priority):
        duplicate = ""
        if e.status == "counted":
            if e.action == "test_result_review" and any(e.test.components & t.components for t in interpretations):
                duplicate = "The same test's review is already represented by independent interpretation; no second review credit."
            for other in counted:
                if e.category == other.category == "category_one":
                    if e.test and other.test and e.test.components & other.test.components:
                        duplicate = "Same test or overlapping panel components already counted; no additional unit."
                    if e.external_source and other.external_source and _same_source(e.external_source, other.external_source):
                        duplicate = "Records from the same or insufficiently distinguished external source count once."
                elif e.category == other.category and e.category in ("historian", "interpretation", "discussion"):
                    duplicate = "This pathway is already represented; additional events do not add a category or unit."
            if duplicate:
                e = replace(e, status="duplicate", reason=duplicate)
            else:
                counted.append(e)
        decisions[e.id] = e
    result = [decisions[e.id] for e in events]
    source_hash = hashlib.sha256((bundle.signed_note + "\0" + bundle.transcript).encode()).hexdigest()
    return QualifiedMDMData(tuple(result), _fingerprint(raw), source_hash)
