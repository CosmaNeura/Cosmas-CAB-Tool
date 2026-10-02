"""Deterministic, deliberately bounded clinical terminology normalization.

This is a terminology matcher, not a clinical diagnosis engine or fuzzy search.
Every significant term must match. Unknown wording stays unresolved. Aliases
express terminology equivalence, never a diagnosis inferred from a lab value,
medication, demographic, or symptom. Extend with reviewed positive AND negative
examples. The CMS descriptors remain the source of code identity.
"""
from __future__ import annotations

import re
import unicodedata

VERSION = "clinical-terms.2026-10-02.v3"

# Phrase-local aliases. Order matters: expand abbreviations before phrases.
# These do not erase type, cause, laterality, phase, or named complications.
_ALIASES = (
    (r"\bfhx\b", "family history"),
    (r"\b(?:pmhx|pmh|h/o)\b", "history"),
    (r"\bt1dm\b", "type 1 diabetes mellitus"),
    (r"\bt2dm\b", "type 2 diabetes mellitus"),
    (r"\btype (one|i) diabetes(?: mellitus)?\b", "type 1 diabetes mellitus"),
    (r"\btype (two|ii) diabetes(?: mellitus)?\b", "type 2 diabetes mellitus"),
    (r"\btype ([12]) diabetes(?! mellitus)\b", r"type \1 diabetes mellitus"),
    (r"\bcopd\b", "chronic obstructive pulmonary disease"),
    (r"\bhtn\b", "hypertension"),
    (r"\bessential (?:primary )?hypertension\b", "hypertension"),
    (r"\bprimary hypertension\b", "hypertension"),
    (r"\bhypothyroidism unspecified\b", "hypothyroidism"),
    (r"\biud\b", "intrauterine contraceptive device"),
    (r"\bintrauterine device in place\b", "presence intrauterine contraceptive device"),
    (r"\bintrauterine contraceptive device in place\b", "presence intrauterine contraceptive device"),
    (r"\bpresence of intrauterine contraceptive device\b", "presence intrauterine contraceptive device"),
    (r"\bencounter for other general counseling and advice on procreation\b", "preconception counseling"),
    (r"\bencounter for preconception counseling\b", "preconception counseling"),
    (r"\bchronic obstructive bronchitis\b", "chronic obstructive pulmonary disease"),
    (r"\b(?:acute )?exacerbation of chronic obstructive pulmonary disease\b",
     "chronic obstructive pulmonary disease with acute exacerbation"),
    (r"\bchronic obstructive pulmonary disease (?:acute )?exacerbation\b",
     "chronic obstructive pulmonary disease with acute exacerbation"),
    (r"\bchronic obstructive pulmonary disease with exacerbation\b",
     "chronic obstructive pulmonary disease with acute exacerbation"),
    (r"\bunilateral primary osteoarthritis\b", "primary osteoarthritis"),
    (r"\bfertility restoration after (?:(?:previous|documented) )?(?:sterilization|tubal ligation)\b",
     "reversal previous sterilization"),
    (r"\bencounter for reversal of previous sterilization\b", "reversal previous sterilization"),
    (r"\bpostinfectious cough\b", "other specified cough"),
    (r"\bpost infectious cough\b", "other specified cough"),
    (r"\brectal bleeding\b", "hemorrhage anus rectum"),
    (r"\bhematochezia\b", "hemorrhage anus rectum"),
    (r"\bpoop (?:a little bit of )?blood\b", "hemorrhage anus rectum"),
    (r"\bhemorrhage of anus and rectum\b", "hemorrhage anus rectum"),
)
_RULES = tuple((re.compile(p), r) for p, r in _ALIASES)

# These states do not change the classification of these explicitly named
# diseases. Match the whole concept, never strip arbitrary words from another
# diagnosis or from a longer phrase with complications. In particular 'stable'
# is meaningful for angina, and 'uncontrolled' diabetes is not a synonym here.
_STATE_BASE = r"(?:hypertension|type [12] diabetes mellitus)"
_ROUTINE_STATE = r"(?:(?:clinically )?stable|(?:well )?controlled)"
_STATE_CONCEPTS = (
    re.compile(rf"^({_STATE_BASE})\s*,?\s+(?:is )?{_ROUTINE_STATE}[.]?$"),
    re.compile(rf"^{_ROUTINE_STATE}\s+({_STATE_BASE})[.]?$"),
    re.compile(rf"^newly (?:diagnosed|recognized|recognised)\s+({_STATE_BASE})[.]?$"),
)

# These are grammatical, not clinical, words. In particular 'other', 'primary',
# 'secondary', 'history', 'status', 'with', and all numbers are retained.
_GRAMMAR = {"an", "the", "of", "and", "for", "on"}
_SINGULAR = {"complications": "complication", "infections": "infection"}
_UNCERTAIN = re.compile(
    r"\b(?:possible|possibly|suspected|probable|rule out|ruled out|"
    r"differential|unconfirmed|not established|not confirmed|"
    r"uncertain|unknown|unclear|versus|vs|cannot exclude|concern for|question of)\b"
)
_FAMILY = re.compile(r"\b(?:family history|mother|father|sister|brother|parent|"
                     r"daughter|son|husband|wife|spouse|grandmother|grandfather|"
                     r"sibling|aunt|uncle)\b")
_DENIED = re.compile(r"\b(?:absent|denied|excluded)\b")
_NON_AFFIRMED = re.compile(r"\b(?:may have|might have|at risk for|at risk of|screen(?:ing)? for)\b")
_ABBREVIATIONS = {"hx", "fhx", "pmh", "dr", "mr", "mrs", "ms", "vs", "etc"}
_RELATION = re.compile(r"\b(?:due to|caused by|complicated by|secondary to|associated with)\b")
_NEGATIVE_PREDICATE = re.compile(
    r"\b(?:(?:is|was|has|have|had) (?:now )?(?:resolved|unlikely|not present|not active|not diagnosed)"
    r"|(?:(?:has|have|had) )?(?:never|not(?: ever)?) (?:been )?diagnosed)\b"
)
_HEADING = re.compile(
    r"(?im)^\s*(?:family history|past medical history|fhx|pmhx|pmh|history|differential(?: diagnosis)?|"
    r"assessment(?: and plan)?|plan|hpi|ros|social history)\s*:?\s*$"
)


def _boundaries(text: str) -> list[int]:
    boundaries = [0]
    for m in re.finditer(r"[;\n]+|(?<=[.!?])\s+", text):
        preceding = re.search(r"([a-z]+)\.$", text[:m.start()].lower())
        if preceding and preceding[1] in _ABBREVIATIONS:
            continue
        boundaries.append(m.end())
    return boundaries + [len(text)]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\bchronic obstructive pulmonary disease\s*\(copd\)",
                  "chronic obstructive pulmonary disease", text)
    text = re.sub(r"\b(?:doesn't|doesn’t|don't|don’t|isn't|isn’t|wasn't|wasn’t)\b", "not", text)
    text = re.sub(r"\bnegative for\b", "no", text)
    text = re.sub(r"[()\[\]\-–—]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    for pattern in _STATE_CONCEPTS:
        match = pattern.fullmatch(text)
        if match:
            text = match[1]
            break
    # Bind a numeric discriminator to its type. A bag of bare numbers loses
    # meaning for 'type 2 ... stage 3' vs 'type 3 ... stage 2'.
    return re.sub(r"\b(type|stage|grade|class) (\d+[a-z]?)\b", r"\1_\2", text)


def _terms(text: str) -> tuple[str, ...]:
    """Full clinical signature, including scoped negative terms.

    Commas reset negation scope; 'without aura, not intractable' therefore has
    two negative attributes, while 'intractable, without aura' has a positive
    intractability attribute. No prefix/substring/nearest-neighbor matching.
    """
    text = normalize(text)
    tokens = re.findall(r"[a-z0-9_]+|[,;.:]", text)
    out: list[str] = []
    negative = False
    list_negation = False
    for token in tokens:
        if token in {",", ";", ".", ":"}:
            if token == ";" or token == "," and not list_negation:
                negative = False
                list_negation = False
            continue
        if token in {"without", "not", "no", "denies"}:
            negative = True
            list_negation = token in {"no", "denies"}
            continue
        if token == "with":
            negative = False
            list_negation = False
            out.append("with")
            continue
        if token in _GRAMMAR or token == "unspecified":
            continue
        token = _SINGULAR.get(token, token)
        out.append(("absent:" if negative else "") + token)
    return tuple(out)


def signature(text: str) -> frozenset[str]:
    return frozenset(_terms(text))


def concept_signature(text: str) -> frozenset[str]:
    terms = signature(text)
    # ICD-10-CM's default for an explicitly typed diabetes with no documented
    # complication is the matching 'without complications' code. This never
    # supplies an unknown type, or discards a named complication. Evidence is
    # separately checked for conflicts before this default is usable.
    for diabetes_type in ("type_1", "type_2"):
        base = frozenset({diabetes_type, "diabetes", "mellitus"})
        if terms == base | {"absent:complication"}:
            return base
    return terms


def meaning_key(text: str) -> tuple[tuple[str, ...], frozenset[str]]:
    """Keep causal direction and disease/type/site adjacency in the index.

    Independently ordered negative modifiers ('without aura, not intractable')
    may trade places; positive clinical terms may not. Thus 'condition due to
    diabetes' cannot match 'diabetes due to condition'.
    """
    # A slash may repeat the same term and its explicit alias. Collapse only
    # identical ordered meanings, never alternatives such as 'sprain/fracture'.
    alternatives = text.split("/")
    if len(alternatives) > 1 and all(_terms(part) == _terms(alternatives[0]) for part in alternatives[1:]):
        text = alternatives[0]
    terms = concept_signature(text)
    positive = tuple(t for t in _terms(text) if not t.startswith("absent:") and t in terms)
    negative = frozenset(t for t in terms if t.startswith("absent:"))
    return positive, negative


def uncertain(text: str) -> bool:
    # Question punctuation is assertion context, not discardable terminology.
    # NFKC in normalize also makes full-width question marks visible here.
    normalized = normalize(text)
    return "?" in normalized or bool(_UNCERTAIN.search(normalized))


def supported_by_quote(description: str, quote: str, *, more_specific=()) -> bool:
    """Require the descriptor's full meaning in one affirmed patient sentence.

    This does not authenticate a quote's source; callers must do that first.
    A verified quote is necessary but insufficient. Extra descriptor attributes
    cannot be supplied by an unrelated sentence, denied disease, family member,
    or an uncertain diagnosis. Unknown clinical phrasing fails to review.
    """
    required = concept_signature(description)
    if not required or not quote.strip():
        return False
    # Sentence/semicolon boundaries prevent combining a type from one condition
    # or family member with a second condition. Do not split decimal numbers.
    boundaries = _boundaries(quote)
    segments = [part for start, end in zip(boundaries, boundaries[1:])
                for part in re.split(r"\bplus\b", normalize(quote[start:end]))]
    for segment in segments:
        if (uncertain(segment) or _DENIED.search(segment) or _NEGATIVE_PREDICATE.search(segment)
                or re.search(r":\s*(?:no|none)\b", segment)):
            continue
        if _NON_AFFIRMED.search(segment) and "screening" not in required:
            continue
        if _FAMILY.search(segment) and "family" not in required:
            continue
        if "history" in signature(segment) and not required & {"history", "status"}:
            continue
        present = signature(segment)
        if not required <= present:
            continue
        # Full ordered clinical phrase, with no unrelated condition supplying
        # a type or site across intervening words. A token set alone cannot
        # distinguish type 2 diabetes from type 2 endoleak + diabetes.
        phrase = meaning_key(description)[0]
        source_words = meaning_key(segment)[0]
        if not any(source_words[i:i + len(phrase)] == phrase
                   for i in range(len(source_words) - len(phrase) + 1)):
            continue
        if re.search(r"\b" + re.escape(phrase[-1])
                     + r"\b\s*[:,-]?\s*(?:now )?(?:negative|resolved|not present|not diagnosed|not active)\b", segment):
            continue
        # A comma in a negative list cannot affirm a later disease. In a code
        # with negative modifiers the disease itself precedes 'without'; this
        # check concerns the assertion BEFORE the clinical phrase.
        first = re.search(r"\b" + re.escape(phrase[0]) + r"\b", segment)
        prefix = segment[:first.start()] if first else ""
        if re.search(r"\b(?:no|not|without|denies|resolved|hx)\b", prefix):
            continue
        # Unknown causal or complication wording is not permission to drop the
        # relationship and capture a generic disease. Known code relationships
        # still require the full ordered descriptor in this same anchor.
        if _RELATION.search(segment) and not _RELATION.search(normalize(description)):
            continue
        # A generic concept cannot erase a more specific diagnosis that is
        # actually named in the same source (portal/secondary hypertension,
        # postprocedural hypothyroidism, etc.). The catalogue supplies these
        # competing meanings; this is not a condition-specific blacklist.
        if any(other <= present for other in more_specific):
            continue
        # The same qualifier cannot be both affirmed and denied in the anchor.
        if any((t[7:] if t.startswith("absent:") else "absent:" + t) in present
               for t in required):
            continue
        # A default 'without complication' cannot override an explicitly named
        # complication/relationship in the source diagnosis.
        if "absent:complication" in signature(description) and (
                "with" in present or re.search(r"\band\b|\bcomplicat\w*\b", segment)
                and "absent:complication" not in present):
            continue
        # Do not mingle type/laterality/phase from different diagnoses within
        # the same sentence. Contradictory or missing qualifiers stay in review.
        domains = (
            {"type_1", "type_2"}, {"right", "left", "bilateral"},
            {"initial", "subsequent", "sequela"}, {"primary", "secondary"},
        )
        if any(required & group and (present & group) != (required & group)
               for group in domains):
            continue
        return True
    return False


def anchor_context(quote: str, source: str) -> str | None:
    """Enclose an exact span so a model cannot omit a preceding 'no/history'.

    Multiple occurrences with different contexts are ambiguous and held. The
    original exact span, rather than this wider validation window, is rendered.
    """
    if not quote.strip() or quote not in source:
        return None
    start = source.find(quote)
    if source.find(quote, start + 1) >= 0:
        return None
    end = start + len(quote)
    boundaries = _boundaries(source)
    before = max(p for p in boundaries if p <= start)
    after = min(p for p in boundaries if p >= end)
    context = source[before:after]
    headings = list(_HEADING.finditer(source[:before]))
    if headings:
        # Section assertions survive newline/sentence clipping. The prefix is
        # validation context only; the rendered evidence remains the exact span.
        context = headings[-1].group().strip() + " " + context
    return context
