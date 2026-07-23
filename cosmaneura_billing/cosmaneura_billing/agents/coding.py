"""Coding agent: proposes codes, always with evidence and never above what's supported."""
from __future__ import annotations

from ..knowledge import em_ruleset
from ..knowledge.icd10_ccsr import ICD10CCSR
from ..llm import LLMClient
from ..schemas import CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle


# Map the LLM's element labels onto risk/data/problem levels for the ruleset.
def _facts_to_em(facts: dict, *, new_patient: bool) -> em_ruleset.EMResult:
    problems = facts.get("problems", [])
    # number/complexity of problems
    has_uncertain = any(p.get("uncertain_prognosis") or p.get("status", "").startswith("new") for p in problems)
    if len(problems) >= 1 and has_uncertain:
        problem_level = "moderate"
    elif len(problems) >= 2:
        problem_level = "low"
    elif len(problems) == 1:
        problem_level = "low"
    else:
        problem_level = "straightforward"

    data = facts.get("data_reviewed", [])
    unique = len({d.get("category", d.get("item")) for d in data})
    data_level = "moderate" if unique >= 3 else "low" if unique >= 1 else "straightforward"

    risk_level = facts.get("risk", {}).get("level", "low")
    if risk_level not in ("straightforward", "low", "moderate", "high"):
        risk_level = "low"

    total_time = facts.get("total_time_minutes")
    return em_ruleset.level_office_visit(
        new_patient=new_patient, problems=problem_level, data=data_level,
        risk=risk_level, total_time_minutes=total_time,
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
                   "facts present in the text; never infer services that were not described.",
            user=f"TASK: extract_clinical_facts\n\n{combined}",
        )

        suggestions: list[CodeSuggestion] = []

        # --- E/M (rule-based level from extracted facts) -------------------- #
        new_patient = "new" in bundle.encounter_type
        em = _facts_to_em(facts, new_patient=new_patient)
        em_ev = []
        if facts.get("risk", {}).get("quote"):
            em_ev.append(Evidence("signed_note", facts["risk"]["quote"],
                                  "supports management risk level"))
        for p in facts.get("problems", [])[:2]:
            if p.get("quote"):
                em_ev.append(Evidence("intake_text", p["quote"], f"problem: {p['name']}"))
        suggestions.append(CodeSuggestion(
            kind=CodeKind.EM, code=em.code, label=em.label,
            disposition=Disposition.REVIEW, evidence=em_ev,
            rationale=f"{em.explanation}. Elements -> {em.elements}.",
            notes=[f"MDM ceiling for this documentation: "
                   f"{em_ruleset.max_defensible_level(new_patient=new_patient, **em.elements)}"],
        ))

        # --- diagnoses (LLM concepts -> authoritative ICD-10 lookup) -------- #
        dx = self.llm.complete_json(
            system="You map documented clinical concepts to diagnosis intent. "
                   "List the underlying clinical concept and the exact supporting quote.",
            user=f"TASK: propose_diagnoses\n\n{combined}",
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
