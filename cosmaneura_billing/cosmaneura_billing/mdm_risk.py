"""Source-qualified office MDM management risk, never a model-selected level.

This bounded attestation reader is an automation policy, not an exhaustive
clinical language parser or a payer rule. Unrecognized decisions stay visible
for review. Public AMA office E/M guidance (2023 pp. 6–19, Jan. 2026 FAQs),
the AMA webinar FAQ p. 3, and CMS MLN006764 (May 2026) inform the pathways.
Generic authored names below are neither licensed descriptors nor billing
codes. There is no bundled prescription-status catalogue: the provisional US
vocabulary resolves only whole, unambiguous generic names; unfamiliar products
need an exact prescription-only attestation. An order for an OTC product does
not make it prescription-only. Drug names never establish a management act.

Extensions require source-rule review and positive/negative actor, object,
temporality, formulation, and causal-evidence tests. In particular, the example
risk pathways are not exhaustive: individualized OTC decisions may qualify for
greater risk when the source establishes the patient's concrete morbidity risk.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re

from .knowledge.mdm_tests import resolve_test
from .schemas import Evidence, VisitBundle

VERSION = "office-mdm-risk.2026-09-30.v4"
ACTIONS = ("medication_management", "toxicity_monitoring", "surgery_decision",
           "hospitalization_decision", "life_sustaining_decision",
           "social_constraint", "individualized_management")
_MAX_EVENTS = 20
_LEVELS = ("straightforward", "low", "moderate", "high")
_RX = frozenset("""metformin lisinopril losartan olmesartan valsartan amlodipine
hydrochlorothiazide chlorthalidone furosemide spironolactone metoprolol atenolol
carvedilol diltiazem verapamil atorvastatin rosuvastatin pravastatin ezetimibe
levothyroxine methimazole prednisone prednisolone methylprednisolone albuterol
tiotropium montelukast amoxicillin doxycycline nitrofurantoin cephalexin
azithromycin ciprofloxacin levofloxacin clindamycin metronidazole fluconazole
valacyclovir acyclovir oseltamivir sertraline escitalopram fluoxetine paroxetine
duloxetine venlafaxine bupropion buspirone trazodone mirtazapine quetiapine
aripiprazole risperidone lithium lamotrigine levetiracetam gabapentin pregabalin
topiramate sumatriptan rizatriptan allopurinol colchicine apixaban rivaroxaban
dabigatran warfarin clopidogrel digoxin amiodarone sotalol methotrexate
azathioprine mycophenolate tacrolimus cyclosporine hydroxychloroquine
adalimumab etanercept infliximab secukinumab rituximab letrozole clomiphene
cabergoline bromocriptine follitropin menotropins leuprolide cetrorelix
ganirelix finasteride tamsulosin sildenafil tadalafil empagliflozin dapagliflozin
semaglutide tirzepatide dulaglutide glipizide pioglitazone sitagliptin morphine
fentanyl hydromorphone midazolam lorazepam diazepam ketamine buprenorphine
oxycodone hydrocodone tramadol methylphenidate amphetamine""".split())
_OTC = frozenset(("acetaminophen", "ibuprofen", "naproxen", "aspirin",
                  "cetirizine", "loratadine", "saline nasal spray",
                  "oral rehydration solution", "dextromethorphan", "guaifenesin"))
_CONTROLLED = frozenset(("morphine", "fentanyl", "hydromorphone", "midazolam",
                         "lorazepam", "diazepam", "ketamine", "buprenorphine"))
_MEASURES = frozenset(("rest", "ice", "hydration", "exercise", "physical therapy",
                       "dietary changes", "saline irrigation", "watchful waiting",
                       "reassurance", "observation"))
_MANAGEMENT_VERBS = (
    r"begin(?:ning)?|began|begun|start(?:ed|ing)?|initiat(?:e|ed|ing)|restart(?:ed|ing)?|prescrib(?:e|ed|ing)|"
    r"increas(?:e|ed|ing)|decreas(?:e|ed|ing)|reduc(?:e|ed|ing)|chang(?:e|ed|ing)|"
    r"taper(?:ed|ing)?|stop(?:ped|ping)?|discontinu(?:e|ed|ing)|avoid(?:ed|ing)?|"
    r"withhold(?:ing)?|withheld|continu(?:e|ed|ing)|renew(?:ed|ing)?|refill(?:ed|ing)?")
_CONTINUATIONS = frozenset(("continue", "continued", "continuing", "renew", "renewed",
                             "renewing", "refill", "refilled", "refilling"))
_BOUNDARY = re.compile(r"(?<!Dr\.)(?<!Mr\.)(?<!Ms\.)(?<=[.!?])\s+|\n")
_SPEAKER = re.compile(r"^\s*(?:physician|qhp|provider|doctor)\s*:\s*", re.I)
_BAD = re.compile(
    r"\b(?:hypothetical|template|example|retracted|incorrect|correction|"
    r"(?:patient|pt|mother|father|spouse|caregiver)\s*(?::|says?\b|said\b|reports?\b|reported\b|states?\b|stated\b|requests?\b)|"
    r"per\s+(?:the\s+)?patient|patient[- ]reported|"
    r"(?:did|do|does|have|has)\s+not\s+(?:actually\s+)?(?:begin|start|stop|change|continue|renew|prescribe|decide|consider|recommend|order|monitor|manage)|"
    r"never\s+(?:began|begun|started|stopped|changed|continued|prescribed|decided|considered|recommended|ordered)|"
    r"not\s+(?:beginning|begun|started|stopped|changed|continued|renewed|prescribed|decided|considered|recommended|ordered|managed)|"
    r"no\s+(?:current\s+)?(?:medication\s+)?(?:decision|management)|"
    r"yesterday|last\s+(?:week|month|year)|previously|historically|prior\s+visit|"
    r"(?:might|may|could|would)\s+(?:begin|start|stop|change|continue|prescribe|admit|consider|recommend)|"
    r"(?:will|plan\s+to)\s+(?:consider|discuss)|not\s+yet|undecided)\b", re.I)
_OTHER_ACTOR = re.compile(r"\b(?:nurse|staff|assistant|patient|mother|father|spouse|"
    r"endocrinologist|cardiologist|surgeon|Dr\.?\s+[A-Z][a-z]+)\s+"
    r"(?:has\s+|had\s+)?(?:began|begun|started|stopped|changed|continued|renewed|prescribed|decided|recommended|ordered)\b", re.I)
_OTHER_PATIENT = re.compile(r"\b(?:another patient|family member|"
    r"(?:my|his|her|their|the patient's)\s+(?:mother|father|spouse|husband|wife|son|daughter|sister|brother)|"
    r"(?:mother|father|spouse|husband|wife|son|daughter|sister|brother)'s)\b", re.I)
_CORRECTION = re.compile(r"\b(?:correction|retract(?:ed|ion)?|incorrect|canceled|cancelled|"
    r"that\s+(?:plan|decision|statement)\s+(?:is|was)\s+wrong)\b", re.I)
_ASSESSMENT = re.compile(r"\b(?:review(?:ed|ing)?|assess(?:ed|ing)?|evaluat(?:ed|ing)|discuss(?:ed|ing))\b", re.I)
_CLINICAL = re.compile(r"\b(?:response|tolerability|adherence|administration|efficacy|"
    r"effectiveness|side effects|adverse effects|benefits|risks|blood pressure|"
    r"glycemic control|symptoms|A1c|TSH|renal function|potassium)\b", re.I)
_HARM = re.compile(r"\b(?:bleeding|hemorrhage|hypoglycemia|arrhythmia|sedation|falls|"
    r"dehydration|hepatotoxicity|nephrotoxicity|myelosuppression|agranulocytosis|"
    r"neutropenia|thrombocytopenia|renal injury|kidney injury|liver injury|"
    r"respiratory depression|QT prolongation|organ failure|death|permanent disability|"
    r"worsening hypertension|heart failure exacerbation|infection)\b", re.I)
_FACTOR = re.compile(r"\b(?:renal impairment|kidney disease|renal failure|hepatic impairment|"
    r"liver disease|cirrhosis|heart failure|COPD|sleep apnea|anticoagulation|apixaban|"
    r"warfarin|rivaroxaban|thrombocytopenia|coagulopathy|prior\s+(?:GI\s+)?bleed(?:ing)?|"
    r"ulcer disease|immunosuppression|neutropenia|pregnancy|diabetes|hypoglycemia|"
    r"frailty|malnutrition|severe obesity|anesthetic reaction|anaphylaxis|"
    r"prolonged QT|bradycardia|hypotension|hypertension|concurrent opioids|"
    r"concurrent benzodiazepines|suicidality|osteoporosis)\b", re.I)


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
    action: str = ""
    status: str = "held"
    category: str | None = None
    reason: str = "A current management decision and its qualifying context require review."
    evidence: tuple[_Anchor, ...] = ()
    level: str = "straightforward"


@dataclass(frozen=True)
class _Act:
    verb: str
    text: str
    tail: str
    prefix: str = ""


def _fingerprint(raw: object) -> str:
    try:
        value = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class QualifiedRisk:
    events: tuple[_Event, ...]
    extraction_sha256: str
    source_sha256: str

    def matches(self, raw: object) -> bool:
        return bool(self.extraction_sha256) and self.extraction_sha256 == _fingerprint(raw)

    @property
    def established(self) -> bool:
        return any(e.status == "counted" for e in self.events)

    @property
    def level(self) -> str:
        return max((e.level for e in self.events if e.status == "counted"),
                   key=_LEVELS.index, default="straightforward")

    @property
    def public(self) -> dict:
        return {"rule_version": VERSION, "level": self.level, "events": [
            {"id": e.id, "subject": e.subject, "status": e.status,
             "category": e.category, "reason": e.reason,
             "evidence": [{"source": a.source, "quote": a.quote} for a in e.evidence]}
            for e in self.events]}

    def evidence(self) -> list[Evidence]:
        return [Evidence(a.source, a.quote, f"MDM risk: {e.id}")
                for e in self.events if e.status == "counted" for a in e.evidence]


def _sentences(text: str):
    edges = [0, *(m.end() for m in _BOUNDARY.finditer(text)), len(text)]
    return [(a, b) for a, b in zip(edges, edges[1:]) if text[a:b].strip()]


def _anchor(quote: object, source: str, bundle: VisitBundle) -> _Anchor | None:
    if source not in ("signed_note", "transcript") or not isinstance(quote, str) or not quote.strip() or len(quote) > 1000:
        return None
    text = getattr(bundle, source, "")
    if not isinstance(text, str) or len(text) > 500_000 or text.count(quote) != 1:
        return None
    pos = text.index(quote)
    headings = list(re.finditer(r"(?m)^\s*([A-Za-z][A-Za-z /'-]{0,60}):", text[:pos]))
    if headings and re.search(r"\b(?:history|medications|patient|family|caregiver|reported|quotation|example|template)\b", headings[-1][1], re.I):
        return None
    contexts = [(a, b) for a, b in _sentences(text) if max(a, pos) < min(b, pos + len(quote))]
    if not contexts:
        return None
    a, b = contexts[0][0], contexts[-1][1]
    context = text[a:b].strip()
    if len(context) > 1000 or (source == "transcript" and not _SPEAKER.match(context)):
        return None
    start = a + len(text[a:b]) - len(text[a:b].lstrip())
    return _Anchor(source, context, start, start + len(context))


def _subject(subject: str) -> str:
    return r"(?<![\w-])" + re.escape(subject).replace(r"\ ", r"\s+") + r"(?![\w-])"


def _mentions(subject: str, text: str) -> bool:
    return bool(re.search(_subject(subject), text, re.I) or re.search(
        r"(?<![\w-])" + re.escape(subject) + r"(?=-(?:induced|related|associated)\b)", text, re.I))


def _body(text: str) -> str:
    text = _SPEAKER.sub("", text).strip()
    return re.sub(r"^(?:Assessment and plan|Plan|Management|Decision today)\s*:\s*", "", text, flags=re.I)


def _safe(text: str) -> bool:
    return not (_BAD.search(text) or _OTHER_ACTOR.search(text) or _OTHER_PATIENT.search(text) or "?" in text)


def _bound_context(anchor: _Anchor, subject: str, bundle: VisitBundle) -> bool:
    if not _safe(anchor.quote):
        return False
    text = getattr(bundle, anchor.source)
    # A clipped exact quote may omit an immediately following correction.
    following = text[anchor.end:anchor.end + 1000].lstrip()
    first = next(iter(_sentences(following)), None)
    if first and _CORRECTION.search(following[first[0]:first[1]]):
        return False
    # Later explicit corrections to this same treatment cannot be bypassed by
    # selecting the earlier statement. Unrelated clinical acts do not bind it.
    return not any(
        _mentions(subject, piece) and _CORRECTION.search(piece)
        for piece in re.split(r"(?<=[.!?])\s+|\n", text[anchor.end:]))


def _intro(prefix: str) -> bool:
    """Admit clinician/plan clauses, not a verb found in somebody else's story."""
    prefix = prefix.lstrip()
    if re.fullmatch(r"(?:today[, :]?\s*)?(?:(?:I|we)\s+)?(?:(?:have|am|are|will)\s+)?"
                    r"(?:(?:decided|elected|agreed|choose|chose)\s+to\s+)?", prefix, re.I):
        return True
    if re.fullmatch(r"after\s+.{1,180},\s*(?:(?:I|we)\s+)?(?:(?:decided|elected|agreed)\s+to\s+)?", prefix, re.I):
        return bool(_ASSESSMENT.search(prefix))
    return bool(re.fullmatch(r"(?:I|we)\s+.{1,180}\s+and\s+(?:will\s+)?(?:(?:decided|elected|agreed)\s+to\s+)?", prefix, re.I)
                and _ASSESSMENT.search(prefix) and _CLINICAL.search(prefix))


_AMOUNT = r"(?:\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_DOSE = _AMOUNT + r"\s*(?:mg|mcg|micrograms?|g|units?|mL|tablets?|capsules?|puffs?|drops?)\b"
_ROUTE = r"(?:orally|by mouth|intravenously|intramuscularly|subcutaneously|IV|IM|SC|SQ)"
_FREQUENCY = (r"(?:daily|weekly|monthly|nightly|once|twice|three times|four times|"
              r"every\s+" + _AMOUNT + r"(?:\s+to\s+" + _AMOUNT + r")?\s+(?:hours?|days?))")


def _product_subject(subject: str, action: str) -> str:
    """Separate only a closed administration suffix on the exact source object.

    The raw/public subject and evidence stay unchanged. Salt, combination,
    concentration, release and product-form words are never dropped to obtain a
    known ingredient. Unknown suffixes keep the whole unknown product identity.
    This parser establishes neither prescription status nor a management act.
    """
    if not _mentions(subject, action):
        return subject
    split = re.fullmatch(r"(.+?)\s+(" + _DOSE + r")"
        r"(?:\s+" + _ROUTE + r")?(?:\s+" + _FREQUENCY + r")*"
        r"(?:\s+as needed)?(?:\s+for\s+" + _AMOUNT + r"\s+days?)?", subject, re.I)
    return split[1] if split else subject


def _object_tail(tail: str) -> bool:
    return not tail or bool(re.match(
        r"^(?:\s*(?:[,;:.]|$)|\s+(?:" + _DOSE + r"|\d|today\b|now\b|because\b|due\b|for\b|at\b|from\b|to\b|by\b|after\b|with\b|without\b|orally\b|intravenously\b|intramuscularly\b|subcutaneously\b|IV\b|IM\b|SC\b|SQ\b))", tail, re.I))


def _dose_boundary(tail: str) -> bool:
    """Consume the source regimen, not just its first post-strength word.

    Unknown product words after a dose/frequency/route cannot disappear when
    the extracted subject ends early. A separate clinical clause remains in
    the original source evidence and still passes decision/context checks.
    """
    remainder = re.split(r";|[.!?](?:\s|$)", tail, maxsplit=1)[0]
    # A route/preposition can precede the dose. Consume only closed regimen
    # tokens in any supported order; never let an unknown tail bypass parsing.
    administration = re.compile(r"^\s+(?:(?:(?:at|from|to|by)\s+)?" + _DOSE
        + "|" + _ROUTE + "|" + _FREQUENCY
        + r"|as needed|with (?:the )?(?:morning|evening) meal|with (?:food|meals)|"
        r"at bedtime|for\s+" + _AMOUNT + r"\s+(?:days?|weeks?|months?)|today|now)\b", re.I)
    while match := administration.match(remainder):
        remainder = remainder[match.end():]
    if not remainder.strip(" ,:."):
        return True
    # Do not accept generic 'with <product>' or an arbitrary comma suffix.
    # Conditional titration is an additional complete regimen, not a product.
    following = re.fullmatch(r"\s*,?\s*then\s+(" + _DOSE + r".*)", remainder, re.I)
    if following:
        return _dose_boundary(" " + following[1]) if len(following[1]) < len(tail) else False
    # A purpose phrase such as 'for topical use' is product/route information,
    # not an indication that may be left outside the product identity. These
    # are structural formulation words, not drug identities or Rx status data.
    if re.search(r"\b(?:use|application|administration|delivery|release|formulation|"
        r"preparation|compound|combination|blend|product|medicine|drug|"
        r"oral|topical|ophthalmic|otic|nasal|intranasal|vaginal|rectal|transdermal|"
        r"inhaled|inhalation|injection|intravenous|intramuscular|subcutaneous|"
        r"tablet|capsule|gel|cream|ointment|solution|suspension|patch|granules|"
        r"ER|XR|SR|DR)\b|%", remainder, re.I):
        return False
    return bool(re.fullmatch(r"\s+(?:if tolerated|"
        r"after (?:the )?(?:specimen collection|collecting (?:a |the )?specimen)|"
        r"(?:after|following) (?:a )?shared decision(?:-making)?|"
        r"rather than (?:increasing|decreasing|changing) (?:the )?dose today|"
        r"(?:for|during|because of|due to) [a-z][a-z -]{1,100})(?:,? with instructions to [^;.!?]{1,160})?",
        remainder, re.I))


def _management(text: str, subject: str) -> _Act | None:
    body = _body(text)
    # A definite regimen conditional on a clinical result can be today's own
    # decision. Waiting for somebody else's prescribing authority is not.
    if re.search(r"\b(?:if|when|once|after|until|pending)\b[^.;]{0,100}\b"
        r"(?:endocrinologist|cardiologist|surgeon|specialist|doctor|physician|provider|prescriber|Dr\.?\s+[A-Z][a-z]+)\s+"
        r"(?:(?:will|can|must)\s+)?(?:prescrib(?:e|es)|authoriz(?:e|es)|approv(?:e|es)|start(?:s)?|initiat(?:e|es)|begin(?:s)?)\b", body, re.I):
        return None
    # Conditional but definite instructions with a regimen are a decision;
    # mere future consideration is not. Preserve the whole condition in evidence.
    conditional = re.match(r"^if\s+[^,;.!?]{3,120},\s*", body, re.I)
    if conditional:
        if not re.search(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|micrograms?|g|units?|mL)\b", body, re.I):
            return None
        body = body[conditional.end():]
    for m in re.finditer(r"\b(" + _MANAGEMENT_VERBS + r")\s+(?:the\s+)?(?:prescription(?:-only)?\s+)?" + _subject(subject), body, re.I):
        if not _intro(body[:m.start()]):
            continue
        tail = body[m.end():]
        # A subject prefix must not silently swallow another product/formulation.
        if not _object_tail(tail) or not _dose_boundary(tail):
            continue
        end = re.search(r";|[.!?](?:\s|$)", tail)
        tail = tail[:end.start()] if end else tail
        if re.search(r"\b(?:(?:in|on|since|during)\s+(?:19|20)\d{2}|\d+\s+(?:days?|weeks?|months?|years?)\s+ago)\b", tail, re.I):
            continue
        return _Act(m[1].casefold(), body[:m.end()] + tail, tail, body[:m.start()])
    return None


def _decision(text: str, subject: str) -> bool:
    if _management(text, subject):
        return True
    body = _body(text)
    verbs = r"recommend(?:ed)?|arrang(?:e|ed)|select(?:ed)?|cho(?:ose|se)|forgo|forwent|withdraw|withdrew|proceed\s+with|decid(?:e|ed)\s+(?:on|against|for)"
    for m in re.finditer(r"\b(?:" + verbs + r")\s+(?:the\s+)?" + _subject(subject), body, re.I):
        if _intro(body[:m.start()]) and _object_tail(body[m.end():]):
            return True
    return _considered(text, subject)


def _considered(text: str, subject: str) -> bool:
    body = _body(text)
    m = re.search(r"\bconsidered\s+(?:the\s+)?" + _subject(subject), body, re.I)
    return bool(m and _intro(body[:m.start()]) and _object_tail(body[m.end():])
        and _dose_boundary(body[m.end():]) and re.search(
        r"\b(?:after|following)\s+(?:a\s+)?shared\s+decision(?:-making)?\b", body, re.I)
        and re.search(r"\b(?:patient\s+declined|we\s+(?:decided|agreed)|I\s+decided)\b", body, re.I))


def _assessed_continuation(text: str, subject: str) -> bool:
    for a, b in _sentences(text):
        for clause in text[a:b].split(";"):
            clause = _body(clause.strip())
            if not (_ASSESSMENT.search(clause) and _CLINICAL.search(clause)):
                continue
            if re.search(r"\b(?:not|never|without)\b[^.;]{0,40}\b(?:review|reviewed|assess|assessed|evaluate|evaluated|discuss|discussed)\b", clause, re.I):
                continue
            act = _management(clause, subject)
            if act and _ASSESSMENT.search(act.text) and _CLINICAL.search(act.text):
                # A coordinated assessment may omit the repeated drug name,
                # but then its prefix must contain only clinical assessment
                # terms, not an assessment of another named treatment.
                prefix = _ASSESSMENT.sub("", _CLINICAL.sub("", act.prefix))
                prefix = re.sub(_subject(subject), "", prefix, flags=re.I)
                prefix = re.sub(r"\b(?:I|we|after|and|will|have|his|her|their|the|patient's|"
                    r"current|today|of|to|for|with|at|goal|controlled|improved|stable|"
                    r"decided|elected|agreed|normal|abnormal|well|tolerated)\b", "", prefix, flags=re.I)
                if not re.search(r"[A-Za-z]", prefix):
                    return True
            if re.match(r"^(?:(?:I|we)\s+)?(?:reviewed|assessed|evaluated|discussed)\s+", clause, re.I) and re.search(
                _CLINICAL.pattern + r"(?:\s+and\s+" + _CLINICAL.pattern + r")?\s+(?:of|to|for|with)\s+" + _subject(subject), clause, re.I):
                return True
    return False


def _rx_status(subject: str, text: str, *, source_subject: str | None = None) -> str | None:
    # Explicit product-specific status can qualify an unfamiliar formulation;
    # the verb 'prescribe' or a mention of prescription-only alternatives is
    # insufficient. Require an affirmative whole-product status assertion or
    # the prescription-only modifier on the actual management object.
    # The full source object can include the regimen that was separated above.
    # Its explicit status assertion must not disappear behind a registry default.
    token = "(?:" + "|".join(_subject(s) for s in dict.fromkeys(
        (subject, source_subject or subject))) + ")"
    statuses = set()
    for a, b in _sentences(text):
        for clause in text[a:b].split(";"):
            clause = _body(clause.strip())
            if not _safe(clause):
                continue
            status = re.fullmatch(token + r"\s+(?:is|remains)\s+(?:an?\s+)?"
                r"(prescription-only|OTC|over-the-counter)(?:\s+(?:product|medicine|medication))?[.]?", clause, re.I)
            if status:
                statuses.add("rx" if status[1].casefold() == "prescription-only" else "otc")
            if _management(clause, subject) and re.search(
                r"\b(?:" + _MANAGEMENT_VERBS + r")\s+(?:the\s+)?prescription-only\s+" + token, clause, re.I):
                statuses.add("rx")
    if len(statuses) > 1:
        return None
    if statuses:
        return next(iter(statuses))
    key = subject.casefold()
    return "rx" if key in _RX else "otc" if key in _OTC else None


def _risk_statement(text: str, subject: str) -> str | None:
    for a, b in _sentences(text):
        statement = _body(text[a:b].strip())
        if not re.match(r"^(?:for this patient,?\s+)?" + _subject(subject) +
                        r"\s+(?:has|poses|carries|presents|is)\s+", statement, re.I):
            continue
        if not (_safe(statement) and _HARM.search(statement)) or re.search(
            r"\b(?:not|no|without|denies|absent|uncertain|possible)\b", statement, re.I):
            continue
        cause = re.fullmatch(r"(?:for this patient,?\s+)?" + _subject(subject) +
            r"\s+(?:(?:has|poses|carries|presents)\s+(?:a\s+)?|is\s+(?:an?\s+)?"
            r"(?:(?:elective|emergency)\s+)?(?:minor|major)\s+(?:surgery|procedure)\s+with\s+)"
            r"(?:(?:patient-specific|moderate|high|substantial|increased)\s+){1,3}"
            r"risk\s+of\s+([^.;]{3,100})\s+(?:because of|due to|given)\s+([^.;]{4,180})[.]?", statement, re.I)
        if cause and _HARM.search(cause[1]) and _FACTOR.search(cause[2]):
            return statement
    return None


def _risk_context(text: str, subject: str) -> bool:
    return _risk_statement(text, subject) is not None


def _individual_level(text: str, subject: str) -> str | None:
    text = _risk_statement(text, subject)
    if text is None:
        return None
    if not re.search(r"\b(?:for this patient|patient-specific|this patient's)\b", text, re.I):
        return None
    if re.search(r"\b(?:high|substantial)\s+risk\b", text, re.I) and re.search(
        r"\b(?:life-threatening|death|organ failure|permanent disability)\b", text, re.I):
        return "high"
    if re.search(r"\bmoderate\s+risk\b", text, re.I):
        return "moderate"
    return None


def _toxicity(text: str, subject: str) -> bool:
    if not (_safe(text) and _mentions(subject, text) and _HARM.search(text)):
        return False
    # Read the order's object and purpose together. A separate test elsewhere
    # in the quote cannot supply a method for this medication's toxicity.
    m = re.fullmatch(r"(?:(?:I|we)\s+(?:have\s+)?)?(?:ordered|order)\s+"
        r"(?P<method>[^.;]{2,180})\s+to\s+(?:monitor for|detect)\s+" +
        re.escape(subject) + r"(?:[- ](?:induced|related|associated))?\s+"
        r"(?P<harm>[^.;]{3,120})[.]?", _body(text), re.I)
    if not m or not _HARM.fullmatch(m["harm"]):
        return False
    interval = r"\b(?:daily|weekly|monthly|quarterly|every\s+(?:[1-3]\s+months?|(?:[1-9]|1[0-2])\s+weeks?))\b"
    frequencies = list(re.finditer(interval, m["method"], re.I))
    if len(frequencies) != 1:
        return False
    method = re.sub(interval, "", m["method"], flags=re.I).strip()
    components = set()
    for name in re.split(r",\s*|\s+and\s+", method, flags=re.I):
        test = resolve_test(name.strip())
        if test:
            components.update(test.components)
    harm = m["harm"].casefold()
    if harm in ("myelosuppression", "agranulocytosis", "neutropenia", "thrombocytopenia"):
        return bool(components & {"lab:wbc", "lab:platelets"})
    if harm in ("hepatotoxicity", "liver injury"):
        return bool(components & {"lab:alt", "lab:ast"})
    if harm in ("nephrotoxicity", "renal injury", "kidney injury"):
        return "lab:creatinine" in components
    if harm in ("arrhythmia", "qt prolongation"):
        return any(c.startswith("cardiac:ecg:") for c in components)
    if harm in ("bleeding", "hemorrhage"):
        return method.casefold() in ("inr", "prothrombin time", "pt/inr")
    if harm == "hypoglycemia":
        return "lab:glucose" in components
    return False


def _act_family(verb: str) -> str:
    if re.fullmatch(r"begin(?:ning)?|began|begun|start(?:ed|ing)?|initiat(?:e|ed|ing)|restart(?:ed|ing)?|prescrib(?:e|ed|ing)", verb):
        return "initiation"
    if verb in _CONTINUATIONS:
        return "continuation"
    for stem in ("increas", "decreas", "reduc", "chang", "taper", "stop", "discontinu", "avoid", "withh"):
        if verb.startswith(stem):
            return stem
    return verb


def _conflicting_medication_context(anchor: _Anchor, subject: str, active: _Act) -> bool:
    """Optional roles cannot retract or reassign the asserted management act.

    An unrelated assessment, a negated higher-risk qualifier, or an unselected
    dose increase is not a contradiction of a separately proven current start.
    Keep direct negation/actor binding to this product and this action family.
    Whole-source correction checks still apply to the mandatory action anchor.
    """
    object_pattern = r"\s+(?:the\s+)?(?:prescription(?:-only)?\s+)?" + _subject(subject)
    for a, b in _sentences(anchor.quote):
        for clause in anchor.quote[a:b].split(";"):
            negative = re.search(r"\b(?:not|never|declined to|refused to)\s+(?:actually\s+)?(" +
                                 _MANAGEMENT_VERBS + r")" + object_pattern, clause, re.I)
            if negative and _act_family(negative[1].casefold()) == _act_family(active.verb) and _object_tail(clause[negative.end():]):
                return True
            passive = re.search(_subject(subject) + r"\s+(?:is|was|has been)\s+(?:not|never)\s+(" +
                                _MANAGEMENT_VERBS + r")\b", clause, re.I)
            if passive and _act_family(passive[1].casefold()) == _act_family(active.verb):
                return True
            decision = re.search(r"\b(?:decision|plan|order)\s+to\s+(" + _MANAGEMENT_VERBS + r")" +
                object_pattern + r"\s+(?:is|was|has been)\s+(?:not|never)\s+(?:made|issued|authorized|established)\b", clause, re.I)
            if decision and _act_family(decision[1].casefold()) == _act_family(active.verb):
                return True
            actor = _OTHER_ACTOR.search(clause)
            if actor:
                direct = re.match(object_pattern, clause[actor.end():], re.I)
                verb = re.search(r"(" + _MANAGEMENT_VERBS + r")$", actor[0], re.I)
                if direct and verb and _act_family(verb[1].casefold()) == _act_family(active.verb) and _object_tail(clause[actor.end() + direct.end():]):
                    return True
            if active.verb in _CONTINUATIONS and _mentions(subject, clause) and _CLINICAL.search(clause) and re.search(
                r"\b(?:not|never|without)\b[^.;]{0,40}\b(?:review|reviewed|assess|assessed|evaluate|evaluated|discuss|discussed)\b", clause, re.I):
                return True
    return False


def _medication_event(e: _Event, mapped: dict[str, _Anchor], bundle: VisitBundle) -> _Event:
    """Select proof by role; extra context never becomes implicit risk support.

    Every submitted quote has already passed exact-source/shape validation.
    The action must independently bind the clinician and whole product. An
    optional decision can establish an otherwise missing continuation assessment;
    only the qualification role can establish the extra morbidity/toxicity path.
    Unused anchors remain in a held event but never in counted code evidence.
    """
    action_anchor = mapped["action_quote"]
    action_text = action_anchor.quote
    subject = _product_subject(e.subject, action_text)
    if not _bound_context(action_anchor, subject, bundle):
        return replace(e, reason="Attribution, negation, timing, or a source correction prevents a current clinician decision.")
    active = _management(action_text, subject)
    if not active and _considered(action_text, subject):
        active = _Act("considered", action_text, "")
    if not active:
        return replace(e, reason="A list, refill mention, or another person's action does not establish this medication decision.")
    if any(_conflicting_medication_context(a, subject, active)
           for a in e.evidence if a != action_anchor):
        return replace(e, reason="Additional source context negates or reassigns this medication decision or its required assessment.")
    usable = tuple(a for a in e.evidence if _bound_context(a, subject, bundle) and _mentions(subject, a.quote))
    selected = [action_anchor]

    def use(anchor):
        if anchor not in selected:
            selected.append(anchor)

    if active.verb in _CONTINUATIONS:
        decision = mapped.get("decision_quote")
        assessed = next((a for a in (action_anchor, decision)
                         if a is not None and a in usable and _assessed_continuation(a.quote, subject)), None)
        if assessed is None:
            return replace(e, reason="Continuation or renewal needs a documented clinical assessment and decision, not list reconciliation alone.")
        use(assessed)
    status = _rx_status(subject, "\n".join(a.quote for a in usable), source_subject=e.subject)
    if status is None:
        return replace(e, reason="This product's prescription/OTC status or exact formulation requires review.")
    # The provisional whole-name registry needs no additional source anchor.
    # A source-supplied product status does, including unfamiliar formulations.
    if _rx_status(subject, action_text, source_subject=e.subject) != status:
        status_anchor = next((a for a in usable if _rx_status(subject, a.quote, source_subject=e.subject) == status), None)
        if status_anchor is None:
            return replace(e, reason="The exact product status cannot be bound to an affirmative source assertion.")
        use(status_anchor)
    qualification = mapped.get("qualification_quote", action_anchor)
    qualified_text = qualification.quote if qualification in usable else ""

    def counted(category, level, reason):
        if any(a not in selected for a in e.evidence):
            reason += " Optional context did not establish additional risk."
        return replace(e, status="counted", category=category, level=level, reason=reason,
                       evidence=tuple(selected))

    if e.action == "toxicity_monitoring":
        if _toxicity(qualified_text, subject):
            use(qualification)
            return counted("intensive_toxicity_monitoring", "high", "Current therapy has agent-specific serious-toxicity monitoring with an objective method and qualifying interval.")
        return replace(e, reason="Intensive monitoring needs this agent's serious toxicity, an objective monitoring method, and a current qualifying interval; efficacy or routine tests are insufficient.")
    personal = _individual_level(qualified_text, subject)
    if personal:
        use(qualification)
        return counted("patient_specific_management", personal, "The current decision has documented patient-specific morbidity risk and a causal clinical factor.")
    if subject.casefold() in _CONTROLLED and re.match(
        r"\s+(?:\d+(?:\.\d+)?\s*(?:mg|mcg|micrograms?|mL)\s+)?(?:IV|IM|SC|SQ|intravenously|intramuscularly|subcutaneously)\b", active.tail, re.I):
        return counted("parenteral_controlled_medication", "high", "The current controlled-medication decision explicitly specifies a parenteral route.")
    return counted("prescription_management" if status == "rx" else "nonprescription_management",
                   "moderate" if status == "rx" else "low", "A source-bound current medication decision establishes this management pathway; product name alone does not.")


def _qualify(raw: object, index: int, bundle: VisitBundle) -> _Event:
    eid = f"mdm-risk-{index + 1:02d}"
    if not isinstance(raw, dict):
        return _Event(eid, "Unrecognized management event")
    subject = raw.get("subject", "")
    if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 120 or any(ord(c) < 32 for c in subject):
        return _Event(eid, "Unrecognized management subject")
    subject = subject.strip()
    action = raw.get("action")
    e = _Event(eid, subject, action if isinstance(action, str) else "")
    fields = {"action", "subject", "source", "action_quote", "decision_quote", "qualification_quote"}
    if set(raw) - fields or action not in ACTIONS:
        return replace(e, reason="Unrecognized event fields or factual action; raw risk labels are not evidence.")
    source = raw.get("source", "")
    anchors = []
    mapped = {}
    for field in ("action_quote", "decision_quote", "qualification_quote"):
        quote = raw.get(field, "")
        if field != "action_quote" and quote == "":
            continue
        a = _anchor(quote, source, bundle)
        if a is None:
            return replace(e, evidence=tuple(anchors), reason="Each supplied quote must identify one complete assertion in its declared clinical source.")
        mapped[field] = a
        if a not in anchors:
            anchors.append(a)
    e = replace(e, evidence=tuple(anchors))
    if not anchors or not _bound_context(mapped["action_quote"], subject, bundle):
        return replace(e, reason="Attribution, negation, timing, or a source correction prevents a current clinician decision.")
    if action in ("medication_management", "toxicity_monitoring"):
        return _medication_event(e, mapped, bundle)
    if not all(_bound_context(a, subject, bundle) for a in anchors):
        return replace(e, reason="Attribution, negation, timing, or a source correction prevents a current clinician decision.")
    action_text = mapped["action_quote"].quote
    qualification = mapped.get("qualification_quote", mapped["action_quote"]).quote
    # Additional anchors cannot borrow an unrelated patient's/treatment's risk.
    if not all(_mentions(subject, a.quote) for a in anchors):
        return replace(e, reason="Each decision and eligibility assertion must name the same management subject.")
    chosen = _decision(action_text, subject)

    def counted(category, level, reason):
        return replace(e, status="counted", category=category, level=level, reason=reason)

    if action == "social_constraint":
        limited = re.match(r"^(?:(?:treatment with|diagnostic evaluation for|treatment of)\s+)?" + _subject(subject) + r"\s+(?:is|was|remains|has been)\s+significantly\s+limited\s+(?:by|because of|due to)\s+([^.;]{4,180})[.]?$", _body(action_text), re.I)
        factor = r"\b(?:inability to afford|cannot afford|financial insecurity|food insecurity|homelessness|unstable housing|lack of transportation|transportation barrier|low health literacy|lack of insurance)\b"
        if limited and re.search(factor, limited[1], re.I) and not re.search(r"\b(?:no|not|without)\b", limited[1], re.I):
            return counted("socially_limited_care", "moderate", "An identified social condition significantly limits this patient's current diagnosis or treatment.")
        return replace(e, reason="A social condition alone is insufficient; the source must causally establish a significant limitation of the named current care.")

    if not chosen:
        return replace(e, reason="The named management choice requires a current clinician decision, including substantive considered/declined choices when documented.")
    if action == "individualized_management":
        personal = _individual_level(qualification, subject)
        if personal:
            return counted("patient_specific_management", personal, "Concrete patient-specific morbidity risk and its causal clinical factor qualify the documented choice.")
        if subject.casefold() in _MEASURES:
            minimal = subject.casefold() in ("watchful waiting", "reassurance", "observation")
            return counted("minimal_management" if minimal else "conservative_management", "straightforward" if minimal else "low", "The source establishes a current conservative management decision.")
        return replace(e, reason="Individualized risk requires concrete morbidity and a causal patient-specific factor; a risk adjective alone does not qualify.")
    if action == "surgery_decision":
        # Type and risk qualifiers belong to this operation's own assertion.
        statements = [_body(qualification[a:b].strip()) for a, b in _sentences(qualification)]
        pattern = r"^" + _subject(subject) + r"\s+is\s+(?:an?\s+)?(?:(elective|emergency)\s+)?(minor|major)\s+(?:surgery|procedure)\b(.*)"
        qualification = next((s for s in statements if re.fullmatch(pattern, s, re.I)), "")
        typed = re.fullmatch(pattern, qualification, re.I)
        minor = bool(typed and typed[2].casefold() == "minor")
        major = bool(typed and typed[2].casefold() == "major")
        emergency = bool(typed and (typed[1] or "").casefold() == "emergency")
        elective = bool(typed and (typed[1] or "").casefold() == "elective")
        if re.search(r"\b(?:not|never)\s+(?:an?\s+)?(?:emergency|elective|minor|major)\b|\bif\b", qualification, re.I):
            return replace(e, reason="The surgery characterization is conditional, negated, or internally inconsistent.")
        risks = _risk_context(qualification, subject)
        none = bool(typed and re.fullmatch(r"\s+(?:with no|without)\s+identified\s+(?:(?:patient|procedure)(?:\s+(?:or|and)\s+(?:patient|procedure))?\s+)?risk factors[.]?", typed[3], re.I))
        if minor == major or (emergency and elective) or (risks and none):
            return replace(e, reason="Surgery type and individualized risk context are absent, inconsistent, or ambiguous.")
        if major and emergency:
            return counted("emergency_major_surgery", "high", "The clinician documents a current decision for an emergency major surgery.")
        if minor and (risks or none):
            return counted("minor_surgery_with_risks" if risks else "minor_surgery", "moderate" if risks else "low", "The clinician characterizes a minor surgery and its identified risk context.")
        if major and elective and (risks or none):
            return counted("elective_major_surgery_with_risks" if risks else "elective_major_surgery", "high" if risks else "moderate", "The clinician characterizes an elective major surgery and its identified risk context.")
        return replace(e, reason="Procedure name, invasiveness, age, or referral alone cannot establish the surgery pathway.")
    if action == "hospitalization_decision":
        if re.search(r"\b(?:hospital admission|hospitalization|inpatient admission|intensive care|ICU transfer|escalation to hospital care)\b", subject, re.I):
            contextual = any(_decision(_body(action_text[a:b].strip()), subject) and re.search(
                _subject(subject) + r"\s+(?:today\s+)?(?:because of|due to|for)\s+[^.;]{0,60}\b(?:hypoxemia|hypoxia|respiratory failure|sepsis|hemodynamic instability|suicidality|severe dehydration|acute kidney injury|chest pain|stroke|hemorrhage|heart failure|inpatient treatment|urgent monitoring)\b", action_text[a:b], re.I)
                for a, b in _sentences(action_text))
            if contextual:
                return counted("hospitalization", "high", "A substantive current hospitalization/escalation decision is tied to this patient's clinical need, including a documented shared decision against admission.")
        return replace(e, reason="A current substantive hospitalization decision needs its clinical context; an ED contingency or routine referral is insufficient.")
    if action == "life_sustaining_decision":
        if re.search(r"\b(?:resuscitation|mechanical ventilation|life-sustaining treatment|dialysis|artificial nutrition)\b", subject, re.I):
            # The withdrawal action, intervention and prognosis must belong to
            # one clinical clause; stopping another medicine is not withdrawal.
            for a, b in _sentences(action_text):
                for clause in action_text[a:b].split(";"):
                    body = _body(clause.strip())
                    match = re.search(r"\b(?:forgo|forwent|withdraw|withdrew|withhold|withheld|stop|stopped|discontinue|discontinued|decided against)\s+(?:the\s+)?"
                        + _subject(subject) + r"\s+(?:today\s+)?(?:because of|due to|given)\s+(?:poor prognosis|terminal illness|terminal cancer|end-stage disease|irreversible organ failure)\b", body, re.I)
                    if match and _intro(body[:match.start()]) and _safe(body):
                        return counted("life_sustaining_deescalation", "high", "A current decision to limit life-sustaining treatment is explicitly caused by poor prognosis.")
        return replace(e, reason="A code-status label alone is insufficient; a current life-sustaining treatment decision must be causally tied to poor prognosis.")
    return e


def qualify_risk(raw: object, bundle: VisitBundle) -> QualifiedRisk:
    fingerprint = _fingerprint(raw)
    source_hash = hashlib.sha256((bundle.signed_note + "\0" + bundle.transcript).encode()).hexdigest()
    if not isinstance(raw, list):
        events = () if raw is None else (_Event("mdm-risk-01", "Unstructured management risk", reason="Only bounded factual management events can qualify risk; model level labels are ignored."),)
    elif len(raw) > _MAX_EVENTS:
        events = (_Event("mdm-risk-01", "Management event limit exceeded", reason="More than 20 candidate events requires review; no partial risk calculation was performed."),)
    else:
        events = tuple(_qualify(item, i, bundle) for i, item in enumerate(raw))
        # The highest supported pathway for the same named management decision
        # represents it once; repeated extraction cannot manufacture more risk.
        seen = set()
        selected = {}
        for e in sorted(events, key=lambda e: (-_LEVELS.index(e.level), e.id)):
            if e.status != "counted":
                continue
            key = e.subject.casefold()
            if key in seen:
                selected[e.id] = replace(e, status="duplicate", reason="This same management subject already has an equal or stronger source-qualified pathway.")
            else:
                seen.add(key)
        events = tuple(selected.get(e.id, e) for e in events)
    return QualifiedRisk(events, fingerprint, source_hash)
