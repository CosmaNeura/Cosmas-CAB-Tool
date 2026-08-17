"""Proof of the core invariant: the guardrail cannot be pushed to upcode.

Run: python prove_no_upcode.py
"""
from cosmaneura_billing.agents import ComplianceGuardrailAgent
from cosmaneura_billing.guardrail_fuzz import run_fuzz
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

# ---------------------------------------------------------------------------
# One scenario proves the guardrail handles the case its author imagined. The
# property below is what actually backs the promise made to customers: across
# 2,000 adversarial inputs — inflated levels, missing evidence, codes
# pre-marked billable, modifier-suffixed and malformed code strings, empty
# fact sets — no code is ever added, no E/M level ever rises, and nothing is
# captured that the documentation does not support.
# ---------------------------------------------------------------------------
CASES = 2000
violations = run_fuzz(CASES)
if violations:
    print(f"\nFAIL — {len(violations)} invariant violation(s) in {CASES} cases:")
    for line in violations[:10]:
        print("   ", line)
    raise SystemExit(1)

print(f"PASS — {CASES} adversarial cases, zero invariant violations.")
print("       (I1 no codes added · I2 no level raised · I3 nothing captured above")
print("        the ceiling · I4 no evidence invented · I5 no capture without")
print("        evidence · I6 rejections stick · I7 non-E/M codes untouched ·")
print("        I8 unverifiable codes are held, never billed)")
print("\nThe pipeline is structurally incapable of billing above documented MDM.")
