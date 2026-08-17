"""Coding agent: proposes codes, always with evidence and never above what's supported."""
from __future__ import annotations

from ..evidence import verify_quote
from ..knowledge import em_ruleset
from ..knowledge.icd10_ccsr import ICD10CCSR
from ..llm import LLMClient
from ..schemas import CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle


# --------------------------------------------------------------------------- #
# Structured-output schemas for the two LLM calls. With AnthropicLLM these are
# enforced via forced tool use and re-validated locally; MockLLM ignores them.
# --------------------------------------------------------------------------- #
FACTS_SCHEMA = {
    "type": "object",
    "required": ["chief_complaint", "problems", "data_reviewed", "risk"],
    "properties": {
        "chief_complaint": {"type": "string"},
        "problems": {"type": "array", "items": {
            "type": "object", "required": ["name"],
            "properties": {
                "name": {"type": "string"}, "status": {"type": "string"},
                "acuity": {"type": "string"},
                "uncertain_prognosis": {"type": "boolean"},
                "quote": {"type": "string"},
            }}},
        "data_reviewed": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "string"}, "category": {"type": "string"},
                           "quote": {"type": "string"}}}},
        "risk": {"type": "object", "properties": {
            "level": {"type": "string"},
            "drivers": {"type": "array", "items": {"type": "string"}},
            "quote": {"type": "string"}}},
        "total_time_minutes": {"type": ["integer", "null"]},
        # A bare integer is not documentation. Time can only raise an E/M level
        # when the chart says the minutes, so the extractor must return the
        # verbatim sentence that states them — it is verified before leveling.
        "total_time_quote": {"type": "string"},
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
                           "primary": {"type": "boolean"},
                           "quote": {"type": "string"}}}},
    },
}


def sanitize_facts(facts: dict) -> dict:
    """Defensive normalization of the extraction output before any leveling
    decision reads it. Off-shape values become safe (conservative) defaults, so
    a degenerate extraction can only under-level, never inflate."""
    if not isinstance(facts, dict):
        return {"problems": [], "data_reviewed": [], "risk": {}}
    out = dict(facts)
    out["problems"] = [p for p in out.get("problems") or [] if isinstance(p, dict)]
    out["data_reviewed"] = [d for d in out.get("data_reviewed") or [] if isinstance(d, dict)]
    out["risk"] = out.get("risk") if isinstance(out.get("risk"), dict) else {}
    t = out.get("total_time_minutes")
    out["total_time_minutes"] = t if isinstance(t, int) and not isinstance(t, bool) and t > 0 else None
    q = out.get("total_time_quote")
    out["total_time_quote"] = q if isinstance(q, str) and q.strip() else ""
    # Set only by CodingAgent.run after checking the quote against the chart.
    # Defaulting to False means any path that skips that check under-levels
    # rather than trusting an unverified number.
    out["total_time_evidenced"] = bool(out.get("total_time_evidenced"))
    out["vitals"] = out.get("vitals") if isinstance(out.get("vitals"), dict) else {}
    return out


# Map the LLM's element labels onto risk/data/problem levels for the ruleset.
def _facts_to_em(facts: dict, *, new_patient: bool) -> em_ruleset.EMResult:
    facts = sanitize_facts(facts)
    problems = facts["problems"]
    # number/complexity of problems
    has_uncertain = any(p.get("uncertain_prognosis") or str(p.get("status", "")).startswith("new")
                        for p in problems)
    if len(problems) >= 1 and has_uncertain:
        problem_level = "moderate"
    elif len(problems) >= 2:
        problem_level = "low"
    elif len(problems) == 1:
        problem_level = "low"
    else:
        problem_level = "straightforward"

    data = facts["data_reviewed"]
    unique = len({d.get("category", d.get("item")) for d in data})
    data_level = "moderate" if unique >= 3 else "low" if unique >= 1 else "straightforward"

    risk_level = facts["risk"].get("level", "low")
    if risk_level not in ("straightforward", "low", "moderate", "high"):
        risk_level = "low"

    return em_ruleset.level_office_visit(
        new_patient=new_patient, problems=problem_level, data=data_level,
        risk=risk_level, total_time_minutes=facts["total_time_minutes"],
        time_is_evidenced=facts["total_time_evidenced"],
    )


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
                   "never paraphrase a quote.",
            user=f"TASK: extract_clinical_facts\n\n{combined}",
            schema=FACTS_SCHEMA,
        )
        facts = sanitize_facts(facts)

        # Gate the time pathway before anything levels on it. The general
        # evidence verifier runs later, on suggestions — too late to inform a
        # leveling decision — so the time quote is checked against the chart
        # here. No verifiable quote means the number cannot raise a level.
        facts["total_time_evidenced"] = bool(
            facts["total_time_minutes"]
            and facts["total_time_quote"]
            and verify_quote(facts["total_time_quote"], {
                "signed_note": bundle.signed_note,
                "transcript": bundle.transcript,
                "intake_text": bundle.intake_text,
            })[0]
        )

        suggestions: list[CodeSuggestion] = []

        # --- E/M (rule-based level from extracted facts) -------------------- #
        new_patient = "new" in bundle.encounter_type
        em = _facts_to_em(facts, new_patient=new_patient)
        em_ev = []
        if facts.get("risk", {}).get("quote"):
            em_ev.append(Evidence("signed_note", facts["risk"]["quote"],
                                  "supports management risk level"))
        # When time drove the level, the sentence stating the minutes is the
        # evidence for it and has to be the first thing a reviewer sees.
        if em.pathway == "total_time" and facts["total_time_quote"]:
            em_ev.insert(0, Evidence("signed_note", facts["total_time_quote"],
                                     f"documents {em.total_time_minutes} min total time"))
        for p in facts.get("problems", [])[:2]:
            if p.get("quote"):
                em_ev.append(Evidence("intake_text", p["quote"], f"problem: {p['name']}"))
        suggestions.append(CodeSuggestion(
            kind=CodeKind.EM, code=em.code, label=em.label,
            disposition=Disposition.REVIEW, evidence=em_ev,
            rationale=f"{em.explanation}. Elements -> {em.elements}.",
            notes=[
                f"MDM ceiling for this documentation: "
                f"{em_ruleset.max_defensible_level(new_patient=new_patient, **em.elements)}",
                f"em_pathway:{em.pathway}",
                f"em_mdm_code:{em.mdm_code}",
            ] + ([f"em_total_time_minutes:{em.total_time_minutes}"]
                 if em.total_time_minutes else []),
        ))

        # --- diagnoses (LLM concepts -> authoritative ICD-10 lookup) -------- #
        dx = self.llm.complete_json(
            system="You map documented clinical concepts to diagnosis intent. "
                   "List the underlying clinical concept and the exact supporting quote, "
                   "copied verbatim from the documentation — never paraphrased.",
            user=f"TASK: propose_diagnoses\n\n{combined}",
            schema=DIAGNOSES_SCHEMA,
        )
        for d in dx.get("diagnoses", []):
            matches = self.icd10.find_by_description(d["concept"], limit=1)
            if not matches:
                suggestions.append(CodeSuggestion(
                    kind=CodeKind.ICD10, code="(unresolved)", label=d["concept"],
                    disposition=Disposition.REVIEW,
                    rationale=f"No ICD-10 match in active code set for '{d['concept']}'.",
                ))
                continue
            code = matches[0]
            ev = [Evidence("intake_text", d.get("quote", ""), "documented symptom/finding")] \
                if d.get("quote") and "(none" not in d.get("quote", "") else []
            suggestions.append(CodeSuggestion(
                kind=CodeKind.ICD10, code=code.code, label=code.description,
                disposition=Disposition.REVIEW, evidence=ev,
                rationale=f"CCSR: {code.ccsr_category} {code.ccsr_description}. "
                          f"{'Marked primary by coder.' if d.get('primary') else ''}",
                notes=["primary_candidate"] if d.get("primary") else [],
            ))

        return facts, suggestions
