"""Denial-risk agent.

Heuristics that approximate common payer edits. These lower the chance of a
clean claim being denied -- they never raise reimbursement. Each captured code
gets a denial_risk rating and, where relevant, a concrete fix.

If real CMS NCCI edits are loaded (see knowledge/ncci.py) they are consulted for
procedure-to-procedure bundling and per-day unit limits; otherwise the agent
falls back to the modifier-25 heuristic below. If the current-FY ICD-10-CM set
is available (knowledge/icd10_ccsr.py overlay) every captured diagnosis is
checked for current validity; if the POS table is available (knowledge/pos.py)
telehealth encounters are checked for modifier/POS consistency.
"""
from __future__ import annotations

from ..schemas import CodeKind, CodeSuggestion, Disposition

_UNSPECIFIED_HINTS = ("unspecified", "other ", "not specified")


class DenialRiskAgent:
    def run(
        self,
        suggestions: list[CodeSuggestion],
        *,
        ncci=None,
        coverage=None,
        icd10=None,                 # knowledge.icd10_ccsr.ICD10CCSR (current-FY check)
        pos=None,                   # knowledge.pos.PlaceOfService
        place_of_service: str = "",
        encounter_type: str = "",
    ) -> tuple[list[CodeSuggestion], list[str]]:
        flags: list[str] = []
        captured = [s for s in suggestions if s.disposition == Disposition.CAPTURED]

        has_em = any(s.kind == CodeKind.EM for s in captured)
        has_dx = any(s.kind == CodeKind.ICD10 for s in captured)
        has_procedure_same_day = any(s.kind == CodeKind.PROCEDURE for s in captured)

        for s in captured:
            if s.kind == CodeKind.ICD10:
                if any(h in s.label.lower() for h in _UNSPECIFIED_HINTS):
                    s.denial_risk = "medium"
                    s.notes.append("low specificity - a more specific code reduces denial risk")
                    flags.append(f"{s.code} ({s.label}) is low-specificity; document additional detail "
                                 "(type, acuity, cause, or site) where known to support a more specific code.")
                else:
                    s.denial_risk = "low"
                # current-FY validity: a deleted or header-level code is a
                # near-certain claim rejection.
                if icd10 is not None and getattr(icd10, "has_current_fy", False):
                    if icd10.is_active(s.code) is False:
                        s.denial_risk = "high"
                        s.notes.append("not billable in current FY ICD-10-CM code set")
                        flags.append(f"{s.code} ({s.label}) is not a billable code in the current "
                                     "FY ICD-10-CM set (deleted, replaced, or a non-billable "
                                     "category code) - recode before submitting.")
            if s.kind == CodeKind.EM:
                s.denial_risk = "low" if has_dx else "high"

        # Telehealth POS / modifier consistency.
        if pos is not None and place_of_service and has_em:
            if pos.is_telehealth(place_of_service):
                desc = pos.get(place_of_service) or "telehealth"
                flags.append(
                    f"Telehealth place of service {place_of_service} ({desc}): confirm the E/M "
                    "carries modifier 95 (or 93 for audio-only) and that the payer covers "
                    "telehealth for this service; payment differs from in-office POS 11.")
            elif "telehealth" in encounter_type:
                flags.append(
                    f"Encounter is typed telehealth but place of service is {place_of_service} "
                    "(not POS 02/10) - fix the POS or the encounter type before submitting.")

        # Real NCCI edits, if the practice has loaded the CMS files.
        proc_codes = [s for s in captured if s.kind in (CodeKind.EM, CodeKind.PROCEDURE)]
        if ncci is not None and getattr(ncci, "loaded", False):
            for i in range(len(proc_codes)):
                for j in range(i + 1, len(proc_codes)):
                    hit = ncci.ptp_check(proc_codes[i].code, proc_codes[j].code)
                    if hit and hit.modifier_indicator == "0":
                        flags.append(f"NCCI PTP: {hit.column_two} is bundled into {hit.column_one} and "
                                     "cannot be unbundled (no modifier permitted). Remove or reconsider.")
                    elif hit and hit.modifier_indicator == "1":
                        flags.append(f"NCCI PTP: {hit.column_two} with {hit.column_one} requires a bypass "
                                     "modifier (e.g. 59/X{EPSU}) only if the services were distinct.")
            for s in proc_codes:
                limit = ncci.mue_limit(s.code)
                if limit is not None and limit <= 1:
                    s.notes.append(f"NCCI MUE: max {limit} unit/day for {s.code}")
        elif has_em and has_procedure_same_day:
            # fallback heuristic when NCCI files aren't loaded
            flags.append("E/M billed with a same-day procedure: confirm modifier 25 is appropriate "
                         "(separately identifiable E/M). Load NCCI edits for exact bundling rules.")

        # LCD/NCD medical-necessity check, if the practice has loaded coverage
        # data. Applies to PROCEDURES only: office-visit E/M codes are not
        # governed by LCD diagnosis lists, so matching them against articles
        # would produce false medical-necessity flags.
        if coverage is not None and getattr(coverage, "loaded", False) and has_dx:
            dx = [s for s in captured if s.kind == CodeKind.ICD10]
            primary = next((s for s in dx if "primary_candidate" in s.notes), dx[0])
            for s in [c for c in captured if c.kind == CodeKind.PROCEDURE]:
                res = coverage.supports(s.code, primary.code)
                if res.status in ("not_supported", "not_covered"):
                    s.denial_risk = "high"
                    s.notes.append(f"coverage: {res.status} for primary dx {primary.code}")
                    flags.append(f"Medical necessity ({res.message})")

        if has_em and not has_dx:
            flags.append("E/M has no captured diagnosis to support medical necessity - resolve before submit.")

        return suggestions, flags
