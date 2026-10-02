"""Coding agent: proposes codes, always with evidence and never above what's supported."""
from __future__ import annotations

from dataclasses import replace

from ..em_time import QualifiedEMTime, qualify_em_time
from ..encounter_care import qualify_encounter_care
from ..mdm_data import ACTIONS, QualifiedMDMData, qualify_mdm_data
from ..mdm_problems import QualifiedProblems, qualify_problems
from ..mdm_risk import QualifiedRisk, qualify_risk
from ..knowledge import em_ruleset
from ..knowledge.icd10_ccsr import ICD10CCSR
from ..knowledge.diagnosis_terms import VERSION, anchor_context
from ..primary_diagnosis import intent_notes
from ..llm import LLMClient
from ..schemas import CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle


# --------------------------------------------------------------------------- #
# Structured-output schemas for the two LLM calls. AzureOpenAILLM uses strict
# Structured Outputs when the schema is compatible and always re-validates
# locally; MockLLM ignores them.
# --------------------------------------------------------------------------- #
FACTS_SCHEMA = {
    "type": "object",
    "required": ["chief_complaint", "mdm_problem_events", "mdm_risk_events", "mdm_data_events"],
    "properties": {
        "chief_complaint": {"type": "string"},
        "mdm_problem_events": {"type": "array", "maxItems": 20, "description":
            "One factual event per distinct clinical problem addressed by this "
            "clinician. Do not classify a billing level. Preserve uncertainty, "
            "course, goals, control and prognosis separately in exact source text; "
            "new timing or unknown diagnosis does not establish severity. Do not "
            "split manifestations of one condition into extra problems.",
            "items": {"type": "object", "required": ["name", "source", "identity_quote", "addressed_quote", "status_quote"],
                "properties": {"name": {"type": "string", "maxLength": 160,
                        "description": "The named clinical problem only. Preserve diagnostic type, cause, body site and laterality. Put course, control, goals and uncertainty in status_quote rather than appending assessment or management prose to the name. Do not convert an uncertain diagnosis into a confirmed problem."},
                    "source": {"type": "string", "enum": ["signed_note", "transcript"]},
                    "identity_quote": {"type": "string", "maxLength": 1000,
                        "description": "Complete clinician assertion naming this current problem, preferably its assessment/plan entry. Preserve negation and uncertainty; an unrelated chief complaint or background-list mention is not evidence of current work."},
                    "addressed_quote": {"type": "string", "maxLength": 1000,
                        "description": "Complete assertion of this clinician's current evaluation, treatment or considered decision for this named problem. Prefer the shortest complete owned action statement, including all conditions/negation. A longer same-problem care block is acceptable within the bound; never concatenate noncontiguous quotes or omit a correction."},
                    "status_quote": {"type": "string", "maxLength": 1000,
                        "description": "Complete condition-bound clinician assessment of course, control against goals, severity or prognosis, with qualifications intact. Empty if absent."}}}},
        "mdm_risk_events": {"type": "array", "maxItems": 20, "description":
            "Actual current management decisions and their documented patient-specific "
            "consequences, including deliberate continuation/avoidance/refusal or an "
            "explicit conditional regimen. Never infer risk from illness severity, "
            "medication-list presence, route, procedure name or a referral alone.",
            "items": {"type": "object", "required": ["action", "subject", "source", "action_quote", "decision_quote", "qualification_quote"],
                "properties": {"action": {"type": "string", "enum": ["medication_management", "toxicity_monitoring",
                    "surgery_decision", "hospitalization_decision", "life_sustaining_decision", "social_constraint", "individualized_management"]},
                    "subject": {"type": "string", "maxLength": 120,
                        "description": "Exact treatment, medication product or procedure identity named in action_quote; not an action, indication or management summary. Keep dose, administration frequency and duration in the complete action_quote rather than the product subject. Preserve product combinations, concentration, salt, release formulation and route-defining details. Do not guess a drug from its class or shorten an unknown product to a familiar ingredient."},
                    "source": {"type": "string", "enum": ["signed_note", "transcript"]},
                    "action_quote": {"type": "string", "maxLength": 1000,
                        "description": "Complete assertion naming this subject and the current clinician's decision. Include the initial act and any conditional later steps without clipping qualifications."},
                    "decision_quote": {"type": "string", "maxLength": 1000,
                        "description": "Complete current decision or assessment for the same subject, including the assessment behind continuation. May repeat action_quote if it already supplies this. Not a patient request or another clinician's work."},
                    "qualification_quote": {"type": "string", "maxLength": 1000,
                        "description": "Complete same-subject evidence of product prescription/OTC status or distinct service-specific eligibility: intensive toxicity monitoring/method/frequency, surgery size/urgency/risks, hospitalization alternatives, poor prognosis, concrete SDOH limitation, or assessed patient-specific risk. For ordinary medication management leave empty when no such additional qualification exists. Generic side-effect counseling or disease history is not an advanced-risk qualification."}}}},
        "mdm_data_events": {"type": "array", "maxItems": 40, "description":
            "Completed clinician data work relevant to this encounter, one factual "
            "action per named test/source. Never supply a level, category, count "
            "or billing code. Distinguish genuine orders, result reviews, notes, "
            "independent interpretations and completed direct discussions. Include "
            "considered-but-declined testing only after documented shared decisions.",
            "items": {"type": "object", "required": ["action", "subject", "source",
                "identity_quote", "action_quote", "qualification_quote"], "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "subject": {"type": "string", "maxLength": 160,
                    "description": "Complete named clinical test identity/variant or external source identity. Preserve specimen, analyte, method, laterality and view distinctions. Keep provenance, dates, repeat/order wording and ordering venue in evidence instead of adding them to a test name. Do not infer a more specific test from a generic report."},
                "source": {"type": "string", "enum": ["signed_note", "transcript"]},
                "identity_quote": {"type": "string", "maxLength": 1000,
                    "description": "Exact complete assertion identifying the test or external source; never infer a test type."},
                "action_quote": {"type": "string", "maxLength": 1000,
                    "description": "Exact complete assertion linking that identity to this clinician's completed work."},
                "qualification_quote": {"type": "string", "maxLength": 1000,
                    "description": "Exact nearby eligibility evidence: historian need, independent findings/not separately reported, external source or completed exchange. Empty if absent."},
            }}},
        "total_time_minutes": {"type": ["integer", "null"], "description":
            "Explicit total physician/QHP E/M minutes on this service date. "
            "Never use patient, staff, waiting, session, procedure, or other-day "
            "time; never sum overlapping components or infer a total."},
        "total_time_quote": {"type": "string", "description":
            "Exact complete attestation stating the same total-minute number, "
            "clinician work and service-date context. Empty if not documented."},
        "vitals": {"type": "object"},
        "family_history_colon_cancer": {"type": ["boolean", "null"]},
        "family_history_quote": {"type": "string"},
    },
}

DIAGNOSES_SCHEMA = {
    "type": "object",
    "required": ["diagnoses"],
    "properties": {
        "diagnoses": {"type": "array", "items": {
            "type": "object", "required": ["concept"],
            "properties": {"concept": {"type": "string"},
                           "primary": {"type": "boolean", "description":
                               "True only for the best-supported encounter primary diagnosis intent. Preserve intent even when coding specificity is unresolved; do not choose solely by list order."},
                           "quote": {"type": "string"}}}},
    },
}


def sanitize_facts(facts: dict) -> dict:
    """Defensive normalization of the extraction output before any leveling
    decision reads it. Off-shape values become safe (conservative) defaults, so
    a degenerate extraction can only under-level, never inflate."""
    if not isinstance(facts, dict):
        facts = {}
    out = dict(facts)
    for key in ("problems", "data_reviewed"):
        values = out.get(key)
        out[key] = [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []
    for key in ("mdm_problem_events", "mdm_risk_events", "mdm_data_events"):
        out[key] = out[key] if isinstance(out.get(key), list) else []
    out["risk"] = out.get("risk") if isinstance(out.get("risk"), dict) else {}
    t = out.get("total_time_minutes")
    out["total_time_minutes"] = t if isinstance(t, int) and not isinstance(t, bool) and t > 0 else None
    q = out.get("total_time_quote")
    out["total_time_quote"] = q if isinstance(q, str) and q.strip() else ""
    # A model-provided boolean/dict is not an attestation. Only the private,
    # immutable result of the source qualifier can authorize the time pathway.
    attestation = out.get("_qualified_em_time")
    out["total_time_evidenced"] = (
        isinstance(attestation, QualifiedEMTime)
        and attestation.minutes == out["total_time_minutes"]
        and attestation.quote == out["total_time_quote"]
    )
    out["vitals"] = out.get("vitals") if isinstance(out.get("vitals"), dict) else {}
    return out


def _qualified_elements(facts: dict, evidence=None) -> dict:
    """Private source qualifications only; raw JSON labels have no authority."""
    retained = ({(e.source, e.quote, e.rationale) for e in evidence}
                if evidence is not None else None)
    qualified = {}
    for element, key, cls, raw_key in (
        ("problems", "_qualified_problems", QualifiedProblems, "mdm_problem_events"),
        ("risk", "_qualified_risk", QualifiedRisk, "mdm_risk_events"),
        ("data", "_qualified_mdm_data", QualifiedMDMData, "mdm_data_events"),
    ):
        item = facts.get(key)
        if not isinstance(item, cls) or not item.matches(facts[raw_key]):
            continue
        expected = {(e.source, e.quote, e.rationale) for e in item.evidence()}
        if retained is None or (expected and expected <= retained):
            qualified[element] = item
    return qualified


def qualified_em_support(facts: dict, evidence) -> tuple[set[str], bool]:
    facts = sanitize_facts(facts)
    elements = set(_qualified_elements(facts, evidence))
    time = facts.get("_qualified_em_time")
    time_supported = facts["total_time_evidenced"] and any(
        e.source == time.source and e.quote == time.quote for e in evidence)
    return elements, time_supported


def _facts_to_em(facts: dict, *, new_patient: bool, evidence=None) -> em_ruleset.EMResult:
    facts = sanitize_facts(facts)
    qualified = _qualified_elements(facts, evidence)
    levels = {element: item.level for element, item in qualified.items()}
    time_supported = facts["total_time_evidenced"]
    if evidence is not None:
        _, time_supported = qualified_em_support(facts, evidence)

    result = em_ruleset.level_office_visit(
        new_patient=new_patient, problems=levels.get("problems", "straightforward"),
        data=levels.get("data", "straightforward"), risk=levels.get("risk", "straightforward"),
        total_time_minutes=facts["total_time_minutes"], time_is_evidenced=time_supported,
    )
    # The grid's fallback straightforward values are not established elements.
    # Equal-code time is an independent valid basis when MDM itself is unproven.
    established = {name for name, item in qualified.items() if item.evidence()}
    if time_supported and len(established.intersection(result.met_by)) < 2:
        time_code = em_ruleset.level_by_time(new_patient=new_patient, total_time_minutes=facts["total_time_minutes"])
        if time_code == result.code:
            result = replace(result, pathway="total_time",
                label=f"Office visit supported by documented total time ({time_code})",
                explanation=f"Total time {facts['total_time_minutes']} min documented on the date of encounter supports {time_code}; MDM classification remains unestablished")
    if result.pathway == "total_time":
        result = replace(result, label=f"Office visit supported by documented total time ({result.code})")
    return result


class CodingAgent:
    def __init__(self, llm: LLMClient, icd10: ICD10CCSR):
        self.llm = llm
        self.icd10 = icd10

    def run(self, bundle: VisitBundle) -> tuple[dict, list[CodeSuggestion]]:
        combined = (
            f"Intake:\n{bundle.intake_text}\n\nTranscript:\n{bundle.transcript}\n\n"
            f"Signed note:\n{bundle.signed_note}"
        )
        facts = self.llm.complete_json(
            system="You are a clinical documentation extraction assistant. Extract only "
                   "facts present in the text; never infer services that were not described. "
                   "Every 'quote' field must be copied verbatim from the documentation — "
                   "never paraphrase a quote. Entity fields name the clinical object; "
                   "quote fields preserve the complete assertion of identity, current "
                   "work or qualification described by that field. Do not turn the "
                   "entity into a prose summary. Optional evidence is empty when its "
                   "specific role is absent; do not fill it with unrelated context.",
            user=f"TASK: extract_clinical_facts\n\n{combined}",
            schema=FACTS_SCHEMA,
        )
        facts = sanitize_facts(facts)
        problems = qualify_problems(facts["mdm_problem_events"], bundle, self.icd10)
        risk = qualify_risk(facts["mdm_risk_events"], bundle)
        facts["_qualified_problems"] = problems
        facts["_qualified_risk"] = risk
        data = qualify_mdm_data(facts["mdm_data_events"], bundle, facts["data_reviewed"])
        # Always overwrite any raw JSON claim to be a private qualification.
        facts["_qualified_mdm_data"] = data
        encounter_care = qualify_encounter_care(facts["mdm_problem_events"],
            facts["mdm_data_events"], bundle, problems, data)
        facts["_qualified_encounter_care"] = encounter_care

        # Exact numeric and eligibility checks precede BOTH level and ceiling.
        # General quote similarity cannot authorize a time-based billing fact.
        time = qualify_em_time(
            facts["total_time_minutes"], facts["total_time_quote"], bundle,
        )
        facts["_qualified_em_time"] = time.attestation
        if time.attestation:
            facts["total_time_quote"] = time.attestation.quote
        time_hold = time.reason if not time.attestation and (
            facts["total_time_minutes"] or facts["total_time_quote"]) else ""
        facts = sanitize_facts(facts)

        suggestions: list[CodeSuggestion] = []

        # --- E/M (rule-based level from extracted facts) -------------------- #
        new_patient = "new" in bundle.encounter_type
        em = _facts_to_em(facts, new_patient=new_patient)
        em_ev = problems.evidence() + risk.evidence() + data.evidence()
        # Current work can establish necessity without establishing an MDM
        # complexity category. Retain its own source proof, with a separate tag,
        # instead of allowing a private boolean to outlive stripped evidence.
        retained = {(e.source, e.quote, e.rationale) for e in em_ev}
        em_ev.extend(e for e in problems.addressed_evidence()
                     if (e.source, e.quote, e.rationale) not in retained)
        em_ev.extend(encounter_care.evidence())
        # When time drove the level, the sentence stating the minutes is the
        # evidence for it and has to be the first thing a reviewer sees.
        if time.attestation:
            time_evidence = Evidence(time.attestation.source, time.attestation.quote,
                                     f"documents {time.attestation.minutes} min total time")
            if em.pathway == "total_time":
                em_ev.insert(0, time_evidence)
            else:
                em_ev.append(time_evidence)  # retain an independently valid fallback
        suggestions.append(CodeSuggestion(
            kind=CodeKind.EM, code=em.code, label=em.label,
            disposition=Disposition.REVIEW, evidence=em_ev,
            data_review=data.public,
            clinical_review={"rule_version": "office-mdm-clinical.2026-09-30.v1",
                             "problems": problems.public, "risk": risk.public},
            rationale=f"{em.explanation}. Elements -> {em.elements}."
                      + (f" Time not used: {time_hold}" if time_hold else ""),
            notes=[
                f"em_pathway:{em.pathway}",
                f"em_mdm_code:{em.mdm_code}",
            ] + ([f"em_total_time_minutes:{em.total_time_minutes}"]
                 if em.total_time_minutes else [])
              + ([f"em_time_held:{time_hold}"] if time_hold else []),
        ))

        # --- diagnoses (LLM concepts -> authoritative ICD-10 lookup) -------- #
        dx = self.llm.complete_json(
            system="You map documented clinical concepts to diagnosis intent. "
                   "List one concise clinical diagnosis per item, preserving documented "
                   "type, cause, laterality, complications, negation and encounter phase. "
                   "Do not supply ICD codes. Do not infer a subtype or complication from "
                   "labs, medications, symptoms or risk factors. Unknown/not established "
                   "is not absent. Do not report differentials or family findings as "
                   "current patient diagnoses. The exact supporting quote must name the "
                   "diagnosis and its defining attributes, copied verbatim with its "
                   "assertion context intact — never paraphrased. Leave management "
                   "commentary out of the concept.",
            user=f"TASK: propose_diagnoses\n\n{combined}",
            schema=DIAGNOSES_SCHEMA,
        )
        sources = {"signed_note": bundle.signed_note, "transcript": bundle.transcript,
                   "intake_text": bundle.intake_text}
        for d in dx.get("diagnoses", []):
            primary_notes = intent_notes(d)
            quote = d.get("quote", "")
            anchor = None
            for name, text in sources.items():
                if quote.strip() and quote in text:
                    context = anchor_context(quote, text)
                    if context is not None:
                        anchor = (name, context)
                    # Ambiguity in the signed note must not be bypassed by
                    # choosing a more convenient transcript/intake occurrence.
                    break
            code, reason = self.icd10.resolve(d["concept"], quote=anchor[1] if anchor else "")
            ev = [Evidence(anchor[0], quote, "documented diagnosis/finding")] if anchor else []
            if code is None:
                suggestions.append(CodeSuggestion(
                    kind=CodeKind.ICD10, code="(unresolved)", label=d["concept"],
                    disposition=Disposition.REVIEW, evidence=ev,
                    rationale=reason,
                    notes=[f"terminology:{VERSION}"] + primary_notes,
                ))
                continue
            suggestions.append(CodeSuggestion(
                kind=CodeKind.ICD10, code=code.code, label=code.description,
                disposition=Disposition.REVIEW, evidence=ev,
                rationale=f"{reason} CCSR: {code.ccsr_category} {code.ccsr_description}. "
                          f"{'Extracted primary candidate; confirm in review.' if d.get('primary') is True else ''}",
                notes=[f"terminology:{VERSION}"] + primary_notes,
            ))

        return facts, suggestions
