"""The five agents that make up the billing pipeline.

Flow:
    CodingAgent              -> proposes E/M level + ICD-10 candidates (with evidence)
    DocumentationIntegrityAgent -> rejects unsupported/contradicted codes, flags gaps
    ComplianceGuardrailAgent -> the hard gate: necessity + can only downgrade/remove
    DenialRiskAgent          -> specificity, primary-dx linkage, modifier checks
    FeedbackAgent            -> emits typed nudges back into the platform
"""
from .coding import CodingAgent
from .documentation_integrity import DocumentationIntegrityAgent
from .compliance_guardrail import ComplianceGuardrailAgent
from .denial_risk import DenialRiskAgent
from .feedback import FeedbackAgent

__all__ = [
    "CodingAgent",
    "DocumentationIntegrityAgent",
    "ComplianceGuardrailAgent",
    "DenialRiskAgent",
    "FeedbackAgent",
]
