"""Orchestrator: runs the agents in order and assembles the two role-scoped views."""
from __future__ import annotations

from .agents import (
    CodingAgent, ComplianceGuardrailAgent, DenialRiskAgent,
    DocumentationIntegrityAgent, FeedbackAgent,
)
from .knowledge.icd10_ccsr import ICD10CCSR
from .llm import LLMClient, get_default_client
from .schemas import (
    BillerWorksheet, BillingResult, CodeKind, CodeSuggestion, Disposition,
    FeedbackKind, ProviderCard, VisitBundle,
)


class BillingOrchestrator:
    def __init__(self, llm: LLMClient | None = None, icd10: ICD10CCSR | None = None, ncci=None, coverage=None):
        self.llm = llm or get_default_client()
        self.icd10 = icd10 or ICD10CCSR()
        self.ncci = ncci         # optional knowledge.ncci.NCCIEdits
        self.coverage = coverage  # optional knowledge.coverage.CoverageDB
        self.coding = CodingAgent(self.llm, self.icd10)
        self.integrity = DocumentationIntegrityAgent()
        self.guardrail = ComplianceGuardrailAgent()
        self.denial = DenialRiskAgent()
        self.feedback = FeedbackAgent()

    def process(self, bundle: VisitBundle) -> BillingResult:
        new_patient = "new" in bundle.encounter_type

        facts, suggestions = self.coding.run(bundle)
        suggestions, gaps = self.integrity.run(facts, suggestions)
        suggestions, audit = self.guardrail.run(facts, suggestions, new_patient=new_patient)
        suggestions, denial_flags = self.denial.run(suggestions, ncci=self.ncci, coverage=self.coverage)
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
        )
