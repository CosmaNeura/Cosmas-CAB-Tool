"""Denial-risk agent.

Heuristics that approximate common payer edits. These lower the chance of a
clean claim being denied -- they never raise reimbursement. Each captured code
gets a denial_risk rating and, where relevant, a concrete fix.

If real CMS NCCI edits are loaded (see knowledge/ncci.py) they are consulted for
procedure-to-procedure bundling and per-day unit limits; otherwise the agent
falls back to the modifier-25 heuristic below. Every captured diagnosis must
be affirmatively billable in the current-FY ICD-10-CM overlay; missing current
data holds the diagnosis for review. If the POS table is available (knowledge/pos.py)
telehealth encounters are checked for modifier/POS consistency.
"""
from __future__ import annotations

from ..schemas import CodeKind, CodeSuggestion, Disposition
from ..icd10_validity import billability
from ..primary_diagnosis import resolve_primary

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
        denial_priors=None,         # knowledge.denial_stats.DenialPriors (PSPS)
        place_of_service: str = "",
        encounter_type: str = "",
    ) -> tuple[list[CodeSuggestion], list[str]]:
        flags: list[str] = []
        # Quote verification cannot make a retired/category/unknown code
        # billable. Apply this final hold before computing diagnosis support or
        # coverage so a held diagnosis cannot still justify another service.
        for s in suggestions:
            if s.kind != CodeKind.ICD10 or s.disposition != Disposition.CAPTURED:
                continue
            active = billability(s.code, icd10)
            if active is True:
                continue
            if active is False:
                reason = ("Not a billable code in the current FY ICD-10-CM set "
                          "(absent or a non-billable category). Held for coding review; "
                          "select a supported current code before use.")
                marker = "icd10_current_fy_hold: invalid_or_nonbillable"
            else:
                reason = ("Current FY ICD-10-CM billability could not be verified. "
                          "Held for coding review; restore a verified current catalogue "
                          "and rerun before use.")
                marker = "icd10_current_fy_hold: catalogue_unavailable"
            s.disposition = Disposition.REVIEW
            s.necessity_supported = False
            s.denial_risk = "high"
            s.rationale = reason
            s.notes.append(marker)
            flags.append(f"{s.code} ({s.label}): {reason}")

        captured = [s for s in suggestions if s.disposition == Disposition.CAPTURED]
        sequencing = resolve_primary(suggestions)
        if sequencing.public["requires_review"]:
            flags.append(sequencing.public["message"])

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
            if s.kind == CodeKind.EM:
                s.denial_risk = "low" if has_dx else "high"

        # Real per-code denial priors (CMS PSPS: national Medicare denied/
        # submitted per HCPCS). Attach the rate to every captured E/M and
        # procedure code; flag codes Medicare denies unusually often.
        if denial_priors is not None and getattr(denial_priors, "loaded", False):
            from ..knowledge.denial_stats import FLAG_THRESHOLD, HIGH_THRESHOLD
            for s in captured:
                if s.kind not in (CodeKind.EM, CodeKind.PROCEDURE):
                    continue
                prior = denial_priors.get(s.code)
                if prior is None:
                    continue
                s.notes.append(f"denial_prior:{prior.rate:.4f}")
                state_prior = denial_priors.get_state(s.code)
                if state_prior is not None:
                    s.notes.append(
                        f"denial_prior_state:{denial_priors.state}:{state_prior.rate:.4f}")
                if prior.rate >= HIGH_THRESHOLD:
                    s.denial_risk = "high"
                elif prior.rate >= FLAG_THRESHOLD and s.denial_risk in ("low", "unknown"):
                    s.denial_risk = "medium"
                if prior.rate >= FLAG_THRESHOLD:
                    local = (f" ({denial_priors.state}: {state_prior.rate:.0%})"
                             if state_prior is not None else "")
                    flags.append(
                        f"{s.code}: Medicare denied {prior.rate:.0%} of "
                        f"{prior.submitted:,} submissions of this code nationally"
                        f"{local} (PSPS {denial_priors.source_year}) - verify frequency "
                        "limits, coverage criteria, and modifiers before submitting.")

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
        if coverage is not None and getattr(coverage, "loaded", False):
            primary = sequencing.primary
            for s in [c for c in captured if c.kind == CodeKind.PROCEDURE]:
                if primary is None:
                    note = "coverage: not evaluated because primary diagnosis sequencing is unresolved"
                    s.notes.append(note)
                    flags.append("Procedure coverage was not evaluated: resolve primary diagnosis sequencing in review.")
                    continue
                res = coverage.supports(s.code, primary.code)
                if res.status in ("not_supported", "not_covered"):
                    s.denial_risk = "high"
                    s.notes.append(f"coverage: {res.status} for primary dx {primary.code}")
                    flags.append(f"Medical necessity ({res.message})")

        if has_em and not has_dx:
            flags.append("E/M has no captured diagnosis to support medical necessity - resolve before submit.")

        return suggestions, flags
