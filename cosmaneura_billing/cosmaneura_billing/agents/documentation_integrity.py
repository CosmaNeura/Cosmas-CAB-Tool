"""Documentation-integrity agent.

Three jobs:
  1. No evidence  -> downgrade to REVIEW (provider must point at the documentation).
  2. Contradicted -> REJECT outright (e.g. a family-history code when the patient
     explicitly denied family history). This is the anti-upcode reflex.
  3. Documented-but-unaddressed -> keep at REVIEW and raise a DOC_GAP. A finding
     sitting in the vitals (e.g. an elevated BMI) is not billable until the
     provider actually addresses it; we surface it as a *documentation* gap, not
     as "bill it anyway".
"""
from __future__ import annotations

from ..schemas import CodeSuggestion, Disposition


class DocumentationIntegrityAgent:
    def run(self, facts: dict, suggestions: list[CodeSuggestion]) -> tuple[list[CodeSuggestion], list[str]]:
        gaps: list[str] = []

        fam_hx_present = facts.get("family_history_colon_cancer", None)

        for s in suggestions:
            label = (s.label or "").lower()

            # (2) explicit contradiction: family-history code with denied history
            if "family history" in label and fam_hx_present is False:
                s.disposition = Disposition.REJECTED
                s.rationale = ("Patient explicitly denied this family history "
                               f"(\"{facts.get('family_history_quote', '')}\"). "
                               "Coding it would be unsupported.")
                continue

            # (3) documented-but-unaddressed finding (obesity from BMI in vitals)
            if "obesity" in label or s.code.startswith("E66"):
                vitals = facts.get("vitals", {})
                if vitals.get("documented") and not vitals.get("addressed_in_plan"):
                    s.disposition = Disposition.REVIEW
                    s.notes.append("not_addressed_in_plan")
                    s.rationale = (f"BMI {vitals.get('bmi')} is documented in vitals but obesity "
                                   "is not addressed in the assessment/plan. Not billable until addressed.")
                    gaps.append(
                        f"BMI {vitals.get('bmi')} documented but not addressed in the plan; "
                        "obesity (E66.x) is reportable only if you assess/manage it.")
                    continue

            # (1) no evidence -> needs the provider to confirm
            if not s.has_evidence and s.disposition != Disposition.REJECTED:
                if s.code != "(unresolved)":
                    s.disposition = Disposition.REVIEW
                    s.notes.append("no documentation span attached - confirm before billing")

        return suggestions, gaps
