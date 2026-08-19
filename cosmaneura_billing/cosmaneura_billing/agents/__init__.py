"""The agents that make up the billing pipeline.

Flow:
    CodingAgent              -> proposes E/M level + ICD-10 candidates (with evidence)
    TA4HCodingAgent          -> optional second extractor (Azure Text Analytics
                                for health): ICD-10 candidates + assertion vetoes
    ConsensusAgent           -> reconciles the two extractors; can only be more
                                conservative than the primary engine alone
    DocumentationIntegrityAgent -> rejects unsupported/contradicted codes, flags gaps
    ComplianceGuardrailAgent -> the hard gate: necessity + can only downgrade/remove
    DenialRiskAgent          -> specificity, primary-dx linkage, modifier checks
    FeedbackAgent            -> emits typed nudges back into the platform

The two multi-engine agents are inert unless a secondary engine is configured;
with none, the flow is exactly the single-engine pipeline it has always been.
"""
from .coding import CodingAgent
from .consensus import ConsensusAgent
from .documentation_integrity import DocumentationIntegrityAgent
from .compliance_guardrail import ComplianceGuardrailAgent
from .denial_risk import DenialRiskAgent
from .feedback import FeedbackAgent
from .ta4h_coding import TA4HCodingAgent

__all__ = [
    "CodingAgent",
    "TA4HCodingAgent",
    "ConsensusAgent",
    "DocumentationIntegrityAgent",
    "ComplianceGuardrailAgent",
    "DenialRiskAgent",
    "FeedbackAgent",
]
