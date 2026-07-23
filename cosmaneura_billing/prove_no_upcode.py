"""Proof of the core invariant: the guardrail cannot be pushed to upcode.

Run: python prove_no_upcode.py
"""
from cosmaneura_billing.agents import ComplianceGuardrailAgent
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence

# Documentation that supports at most a moderate (99214) visit.
facts_supporting_99214 = {
    "problems": [{"name": "Hematochezia", "status": "new", "uncertain_prognosis": True,
                  "quote": "blood per rectum"}],
    "data_reviewed": [{"category": "review_external_labs", "item": "CBC"}],
    "risk": {"level": "moderate", "quote": "refer for colonoscopy"},
}

# An upstream agent (or a tampered-with input) tries to bill 99215.
inflated = CodeSuggestion(
    kind=CodeKind.EM, code="99215", label="Office visit, high MDM",
    disposition=Disposition.CAPTURED,
    evidence=[Evidence("signed_note", "refer for colonoscopy", "risk")],
)

result, audit = ComplianceGuardrailAgent().run(facts_supporting_99214, [inflated], new_patient=False)
final = result[0]

print("attempted code :", "99215")
print("final code     :", final.code)
print("audit          :", audit[0])
assert final.code == "99214", "GUARDRAIL FAILED: an over-level survived!"
assert "DOWNGRADE" in audit[0]
print("\nPASS — the guardrail downgraded the over-level to the supported ceiling.")
print("The pipeline is structurally incapable of billing above documented MDM.")
