"""Orchestrator: runs the agents in order and assembles the two role-scoped views."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .agents import (
    CodingAgent, ComplianceGuardrailAgent, ConsensusAgent, DenialRiskAgent,
    DocumentationIntegrityAgent, FeedbackAgent, TA4HCodingAgent,
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
from .persistence import DEPLOYABLE_SYNTHETIC_INFERENCE_SOURCES
from .schemas import (
    BillerWorksheet, BillingResult, CodeKind, CodeSuggestion, Degradation,
    Disposition, FeedbackKind, ProviderCard, VisitBundle,
)
from .settings import Settings
from .ta4h import TA4HClient, get_default_ta4h_client
from .ta4h_shadow import TA4HShadowMetrics, TA4HShadowRunner


class InferenceInputPolicyError(PermissionError):
    """The configured model-input boundary rejected an untrusted bundle."""


class BillingOrchestrator:
    """Runs the agents in order and assembles the two role-scoped views.

    Every collaborator is injectable. ``coding`` and ``secondary_coding`` are
    the multi-engine seam: with no secondary engine configured (the default)
    the pipeline is exactly the single-engine one, byte for byte.
    """

    def __init__(self, llm: LLMClient | None = None, icd10: ICD10CCSR | None = None,
                 ncci=None, coverage=None, pos=None, cpt=None, preferences=None,
                 settings: Settings | None = None,
                 coding=None, secondary_coding=None, ta4h: TA4HClient | None = None,
                 shadow_metrics: TA4HShadowMetrics | None = None):
        config = settings or Settings.from_env()
        self.settings = config
        self.llm = llm or get_default_client(config)
        self.icd10 = icd10 or ICD10CCSR(settings=config)
        # NCCI / MCD coverage: explicit instance wins; otherwise auto-load from
        # data/ncci/ and data/mcd/ if the practice has dropped the CMS files there.
        self.ncci = ncci if ncci is not None else NCCIEdits.from_bundled()
        self.coverage = coverage if coverage is not None else CoverageDB.from_bundled()
        self.pos = pos or PlaceOfService()       # bundled CMS POS codes
        self.cpt = cpt or CPTDescriptors(settings=config)  # licensed data only
        # learned reviewer preferences (hold/refine only, never inflate)
        self.preferences = preferences or PreferenceStore(settings=config)
        # national per-code denial priors (CMS PSPS, bundled)
        self.denial_priors = DenialPriors()
        # deterministic procedure / add-on candidates (99058 and friends)
        self.procedures = ProcedureRules()
        self.coding = coding or CodingAgent(self.llm, self.icd10)
        # Optional second extraction engine (Azure Text Analytics for health).
        # Disabled unless the azure_language settings are enabled, exactly like
        # Azure OpenAI inference, or a client is injected for tests.
        if ta4h is not None:
            self.ta4h = ta4h
        elif secondary_coding is not None:
            self.ta4h = None
        else:
            self.ta4h = get_default_ta4h_client(config)
        if secondary_coding is not None:
            self.secondary_coding = secondary_coding
        elif self.ta4h is not None:
            self.secondary_coding = TA4HCodingAgent(self.ta4h, self.icd10)
        else:
            self.secondary_coding = None
        if config.azure_language_shadow_enabled:
            if self.secondary_coding is None:
                raise RuntimeError("TA4H shadow mode has no secondary extractor")
            self.ta4h_mode = "shadow"
        elif self.secondary_coding is not None:
            # Explicit injection retains the pre-shadow active-consensus test
            # seam. The deployed active lane remains separately gated by
            # COSMANEURA_AZURE_LANGUAGE_ENABLED.
            self.ta4h_mode = "active_consensus"
        else:
            self.ta4h_mode = "off"
        self.shadow_metrics = shadow_metrics or TA4HShadowMetrics(self.ta4h_mode)
        self.shadow_runner = (
            TA4HShadowRunner(self.secondary_coding, self.shadow_metrics)
            if self.ta4h_mode == "shadow"
            else None
        )
        self.consensus = ConsensusAgent()
        self.integrity = DocumentationIntegrityAgent()
        self.guardrail = ComplianceGuardrailAgent()
        self.denial = DenialRiskAgent()
        self.feedback = FeedbackAgent()

    @property
    def multi_engine(self) -> bool:
        return self.secondary_coding is not None

    def ta4h_observability(self) -> dict[str, str | int]:
        return self.shadow_metrics.snapshot()

    def wait_for_shadow(self, timeout: float) -> bool:
        """Acceptance/test synchronization; reviewer requests never call this."""

        return (
            True
            if self.shadow_runner is None
            else self.shadow_runner.wait_for_idle(timeout)
        )

    def shutdown(self) -> None:
        if self.shadow_runner is not None:
            self.shadow_runner.shutdown()

    def _extract(self, bundle: VisitBundle, new_patient: bool
                 ) -> tuple[dict, list[CodeSuggestion], list[str]]:
        """Run the extraction engine(s) and return merged facts + candidates.

        With a secondary engine configured both run concurrently, so the added
        latency is the slower of the two rather than their sum. A secondary-engine
        failure degrades to single-engine operation and is recorded in the audit
        trail — it never fails the encounter.
        """
        if self.secondary_coding is None:
            facts, suggestions = self.coding.run(bundle)
            return facts, suggestions, []

        if self.ta4h_mode == "shadow":
            facts, suggestions = self.coding.run(bundle)
            # Non-blocking and bounded: a slow, failed, or saturated TA4H lane
            # cannot change response availability or latency.
            self.shadow_runner.observe(bundle, suggestions)
            return facts, suggestions, []

        with ThreadPoolExecutor(max_workers=2) as pool:
            primary_future = pool.submit(self.coding.run, bundle)
            secondary_future = pool.submit(self.secondary_coding.run, bundle)
            facts, suggestions = primary_future.result()
            try:
                secondary = secondary_future.result()
            except Exception:                             # noqa: BLE001 - degrade, don't fail
                return facts, suggestions, [
                    "CONSENSUS secondary extraction engine unavailable "
                    "[secondary_error] — single-engine result.",
                ]

        # C2 depends on this ordering: the primary engine's evidence must be
        # authenticated before consensus decides whether a secondary span is
        # merely additive. Otherwise a fabricated primary quote could make the
        # secondary engine supply the first valid span and grow the captured set.
        primary_evidence_audit = verify_suggestion_evidence(bundle, suggestions)
        secondary_facts, secondary_suggestions = secondary
        secondary_evidence_audit = verify_suggestion_evidence(
            bundle, secondary_suggestions,
        )
        merged_facts, merged_suggestions, consensus_audit = self.consensus.run(
            (facts, suggestions),
            (secondary_facts, secondary_suggestions),
            new_patient=new_patient,
        )
        return (
            merged_facts,
            merged_suggestions,
            consensus_audit + primary_evidence_audit + secondary_evidence_audit,
        )

    def process(
        self,
        bundle: VisitBundle,
        user_id: str | None = None,
        *,
        input_provenance: str | None = None,
    ) -> BillingResult:
        if (
            self.settings.azure_openai_synthetic_only
            and input_provenance not in DEPLOYABLE_SYNTHETIC_INFERENCE_SOURCES
        ):
            raise InferenceInputPolicyError(
                "model inference requires reserved stored synthetic provenance"
            )
        new_patient = "new" in bundle.encounter_type

        facts, suggestions, consensus_audit = self._extract(bundle, new_patient)
        # Procedure / add-on candidates come from documented language rather than
        # clinical inference, so they are proposed deterministically here — and
        # then go through exactly the same evidence, integrity, guardrail and
        # denial gates as everything else. They are not part of the consensus
        # merge: TA4H proposes no CPT, so there is nothing to reconcile.
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
        audit = consensus_audit + proc_audit + ver_audit + pref_audit + audit
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
                    "configure the Azure OpenAI managed-identity inference settings "
                    "for real extraction."),
        )
