"""
Core data structures for the CosmaNeura agentic billing layer.

Design principle baked into these types
---------------------------------------
A code is only ever "captured" when it can point to a span of the signed
documentation that supports it (``CodeSuggestion.evidence``). There is no field
anywhere in this module for "revenue target", "desired level", or anything an
agent could optimise toward at the expense of accuracy. Revenue is *reported*
(captured vs. defensibly-supported) but is never an input that can raise a code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass
class VisitBundle:
    """Everything the pipeline ingests about a single encounter.

    These map directly onto FluencyMed's existing outputs:
      * ``intake_text``  -> feature3 patient leading-Q&A / screening chatbot
      * ``transcript``   -> feature5 live-call transcription
      * ``signed_note``  -> the physician-signed assessment & plan (README
                            "Physician" feature #2: the signed note is what
                            billing is derived from)
    """
    encounter_id: str
    patient_age: int
    patient_sex: str
    encounter_type: str                  # "office_established" | "office_new" | "telehealth_established" ...
    place_of_service: str                # e.g. "11" office, "02"/"10" telehealth
    intake_text: str = ""
    transcript: str = ""
    signed_note: str = ""
    provider_specialty: str = "Family Medicine"
    # What the provider (or EHR) actually put on the encounter before our review.
    # The whole point is to compare *supported* against *submitted*.
    submitted_em_code: Optional[str] = None
    submitted_icd10: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Evidence + codes
# --------------------------------------------------------------------------- #
@dataclass
class Evidence:
    """A pointer back into the documentation. No span -> not billable."""
    source: str            # "signed_note" | "transcript" | "intake_text" | "vitals"
    quote: str             # the exact supporting text
    rationale: str = ""    # why this text supports the code


class Disposition(str, Enum):
    CAPTURED = "captured"          # supported by documentation, goes on the claim
    REVIEW = "needs_review"        # plausible but the provider must confirm/clarify
    REJECTED = "rejected"          # not supported / contradicted -> never billed


class CodeKind(str, Enum):
    EM = "evaluation_management"   # CPT E/M office visit level
    ICD10 = "icd10_diagnosis"
    PROCEDURE = "procedure"        # CPT procedure / HCPCS
    MODIFIER = "modifier"


@dataclass
class CodeSuggestion:
    kind: CodeKind
    code: str
    label: str                                  # short generic label (NOT the AMA long descriptor)
    disposition: Disposition = Disposition.REVIEW
    evidence: list[Evidence] = field(default_factory=list)
    rationale: str = ""
    # Filled in by the guardrail / denial agents:
    necessity_supported: bool = False
    denial_risk: str = "unknown"                # "low" | "medium" | "high"
    notes: list[str] = field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        return len(self.evidence) > 0


# --------------------------------------------------------------------------- #
# Feedback loop
# --------------------------------------------------------------------------- #
class FeedbackKind(str, Enum):
    """Allowed feedback categories.

    Note what is *absent*: there is deliberately no category that prompts a
    patient toward additional billable services, or that prompts a provider to
    perform/order/document services for revenue rather than for the patient.
    Feedback either improves documentation of care that happened, or surfaces
    clinically-indicated follow-up for the patient's benefit, or improves the
    completeness of next time's history-taking.
    """
    DOC_GAP = "documentation_gap"              # -> provider: note doesn't capture work that was done
    CARE_QUALITY = "care_quality"              # -> provider/patient: clinically indicated, patient-benefit
    INTAKE_COMPLETENESS = "intake_completeness"  # -> intake agent: capture X next time for a fuller history
    CODING_EDUCATION = "coding_education"       # -> provider: how documentation maps to coding, generally


class FeedbackTarget(str, Enum):
    PROVIDER = "provider"
    INTAKE_AGENT = "intake_agent"
    DOCUMENTATION_AGENT = "documentation_agent"
    BILLER = "biller"


@dataclass
class FeedbackEvent:
    kind: FeedbackKind
    target: FeedbackTarget
    message: str
    # "subtle nudge" delivery hint for the receiving surface
    tone: str = "subtle"                       # "subtle" | "informational"
    patient_benefit: str = ""                  # required-by-convention: state the clinical/patient benefit


# --------------------------------------------------------------------------- #
# Role-scoped outputs (the two end users)
# --------------------------------------------------------------------------- #
@dataclass
class ProviderCard:
    """What the *provider* sees after the visit is over.

    Optimised for: 30 seconds of attention, clinical framing, never a demand to
    'code higher'. Opportunities are documentation-completeness items.
    """
    encounter_id: str
    summary: str
    captured_codes: list[CodeSuggestion] = field(default_factory=list)
    documentation_opportunities: list[FeedbackEvent] = field(default_factory=list)
    care_follow_ups: list[FeedbackEvent] = field(default_factory=list)
    # transparency: what we did NOT bill and why
    declined_codes: list[CodeSuggestion] = field(default_factory=list)


@dataclass
class BillerWorksheet:
    """What the *back-office biller* sees. Claim-ready, fully auditable."""
    encounter_id: str
    primary_diagnosis: Optional[CodeSuggestion] = None
    diagnoses: list[CodeSuggestion] = field(default_factory=list)
    em_code: Optional[CodeSuggestion] = None
    procedures: list[CodeSuggestion] = field(default_factory=list)
    modifiers: list[CodeSuggestion] = field(default_factory=list)
    denial_flags: list[str] = field(default_factory=list)
    requires_provider_signoff: bool = False
    audit_trail: list[str] = field(default_factory=list)


@dataclass
class BillingResult:
    """The complete output of one pipeline run."""
    provider_card: ProviderCard
    biller_worksheet: BillerWorksheet
    feedback_events: list[FeedbackEvent] = field(default_factory=list)
    # captured vs. defensibly-supported, in code terms (revenue is downstream of this)
    revenue_integrity_note: str = ""
