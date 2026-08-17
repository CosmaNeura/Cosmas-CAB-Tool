"""Orchestrator: runs the agents in order and assembles the two role-scoped views."""
from __future__ import annotations

from .agents import (
    CodingAgent, ComplianceGuardrailAgent, DenialRiskAgent,
    DocumentationIntegrityAgent, FeedbackAgent,
)
from .evidence import verify_suggestion_evidence
from .knowledge.coverage import CoverageDB
from .knowledge.cpt import CPTDescriptors
from .knowledge.denial_stats import DenialPriors
from .knowledge.icd10_ccsr import ICD10CCSR
from .knowledge.ncci import NCCIEdits
from .knowledge.pos import PlaceOfService
from .knowledge.procedures import ProcedureRules
from .preferences import PreferenceStore, apply_preferences
from .llm import LLMClient, describe_client, get_default_client
from .schemas import (
    BillerWorksheet, BillingResult, CodeKind, CodeSuggestion, Degradation,
    Disposition, FeedbackKind, ProviderCard, VisitBundle,
)


class BillingOrchestrator:
    def __init__(self, llm: LLMClient | None = None, icd10: ICD10CCSR | None = None,
                 ncci=None, coverage=None, pos=None, cpt=None, preferences=None):
        self.llm = llm or get_default_client()
        self.icd10 = icd10 or ICD10CCSR()
        # NCCI / MCD coverage: explicit instance wins; otherwise auto-load from
        # data/ncci/ and data/mcd/ if the practice has dropped the CMS files there.
        self.ncci = ncci if ncci is not None else NCCIEdits.from_bundled()
        self.coverage = coverage if coverage is not None else CoverageDB.from_bundled()
        self.pos = pos or PlaceOfService()       # bundled CMS POS codes
        self.cpt = cpt or CPTDescriptors()       # loads only from licensed data (CPT_DATA_DIR)
        # learned reviewer preferences (hold/refine only, never inflate)
        self.preferences = preferences or PreferenceStore()
        # national per-code denial priors (CMS PSPS, bundled)
        self.denial_priors = DenialPriors()
        # deterministic procedure / add-on candidates (99058 and friends)
        self.procedures = ProcedureRules()
        self.coding = CodingAgent(self.llm, self.icd10)
        self.integrity = DocumentationIntegrityAgent()
        self.guardrail = ComplianceGuardrailAgent()
        self.denial = DenialRiskAgent()
        self.feedback = FeedbackAgent()

    def process(self, bundle: VisitBundle, user_id: str | None = None) -> BillingResult:
        new_patient = "new" in bundle.encounter_type

        facts, suggestions = self.coding.run(bundle)
        # Procedure / add-on candidates come from documented language rather than
        # clinical inference, so they are proposed deterministically here — and
        # then go through exactly the same evidence, integrity, guardrail and
        # denial gates as everything else.
        proc, proc_audit = self.procedures.propose(
            bundle, has_em=any(s.kind == CodeKind.EM for s in suggestions))
        suggestions += proc
        # Authenticate every evidence quote against the actual documentation
        # BEFORE any agent may rely on it; a code whose quotes don't verify
        # loses them and cannot pass the guardrail's evidence gate.
        ver_audit = verify_suggestion_evidence(bundle, suggestions)
        suggestions, gaps = self.integrity.run(facts, suggestions)
        # Learned reviewer preferences (per user + practice-wide): may hold a
        # code for review or refine it within its ICD-10 family — never add,
        # promote, or re-level. Runs before the guardrail, which enforces that.
        pref_audit = apply_preferences(
            suggestions, self.preferences.for_user(user_id), icd10=self.icd10)
        suggestions, audit = self.guardrail.run(facts, suggestions, new_patient=new_patient)
        audit = proc_audit + ver_audit + pref_audit + audit
        suggestions, denial_flags = self.denial.run(
            suggestions, ncci=self.ncci, coverage=self.coverage,
            icd10=self.icd10, pos=self.pos, denial_priors=self.denial_priors,
            place_of_service=bundle.place_of_service,
            encounter_type=bundle.encounter_type,
        )
        # Official CPT descriptors, when the practice's licensed data is loaded.
        relabeled = self.cpt.relabel([s for s in suggestions
                                      if s.kind in (CodeKind.EM, CodeKind.PROCEDURE)])
        if relabeled:
            audit.append(f"CPT     official descriptors applied to {relabeled} code(s) "
                         "from licensed CPT data.")
        events = self.feedback.run(facts, gaps, suggestions, submitted_em_code=bundle.submitted_em_code)

        captured = [s for s in suggestions if s.disposition == Disposition.CAPTURED]
        review = [s for s in suggestions if s.disposition == Disposition.REVIEW]
        rejected = [s for s in suggestions if s.disposition == Disposition.REJECTED]

        # ---- biller worksheet (claim-ready) ------------------------------- #
        dx = [s for s in captured if s.kind == CodeKind.ICD10]
        primary = next((s for s in dx if "primary_candidate" in s.notes), dx[0] if dx else None)
        ordered_dx = ([primary] + [d for d in dx if d is not primary]) if primary else dx
        em = next((s for s in captured if s.kind == CodeKind.EM), None)

        worksheet = BillerWorksheet(
            encounter_id=bundle.encounter_id,
            primary_diagnosis=primary,
            diagnoses=ordered_dx,
            em_code=em,
            procedures=[s for s in captured if s.kind == CodeKind.PROCEDURE],
            modifiers=[s for s in captured if s.kind == CodeKind.MODIFIER],
            denial_flags=denial_flags,
            requires_provider_signoff=bool(review),
            audit_trail=audit,
        )

        # ---- provider card (post-visit, clinical framing) ----------------- #
        card = ProviderCard(
            encounter_id=bundle.encounter_id,
            summary=facts.get("chief_complaint", ""),
            captured_codes=captured,
            documentation_opportunities=[e for e in events if e.kind in
                                         (FeedbackKind.DOC_GAP, FeedbackKind.CODING_EDUCATION)],
            care_follow_ups=[e for e in events if e.kind == FeedbackKind.CARE_QUALITY],
            declined_codes=rejected + review,
        )

        n_cap, n_dx = len([c for c in captured]), len(dx)
        integrity_note = (
            f"{n_cap} codes captured ({n_dx} dx + E/M), each linked to documentation. "
            f"{len(rejected)} rejected as unsupported, {len(review)} held for provider confirmation. "
            "Captured set reflects documented, medically-necessary care only; no code exceeds its MDM ceiling."
        )

        return BillingResult(
            provider_card=card, biller_worksheet=worksheet,
            feedback_events=events, revenue_integrity_note=integrity_note,
            inference=describe_client(self.llm),
            degraded=self._degradation(facts),
        )

    def _degradation(self, facts: dict) -> Degradation | None:
        """Say when a run wasn't a real coding attempt.

        The offline model only covers the demo encounter, so on anything else it
        extracts nothing and the pipeline correctly captures nothing. That is the
        right output for the wrong reason, and without this a reviewer sees an
        empty claim with no explanation.
        """
        if getattr(self.llm, "mode", None) != "mock":
            return None
        if facts.get("problems"):
            return None
        return Degradation(
            reason="offline_model_unsupported_encounter",
            detail=("Running on the offline demo model, which only codes the bundled "
                    "demo encounter. No codes were proposed for this documentation — "
                    "set ANTHROPIC_API_KEY for real extraction."),
        )
