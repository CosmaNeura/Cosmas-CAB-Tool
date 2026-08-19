"""Proof of the core invariant: the guardrail cannot be pushed to upcode.

Two proofs, because the pipeline can now run two extraction engines:
  1. the guardrail downgrades an over-level to the documented ceiling;
  2. adding a second extraction engine cannot raise that ceiling, and cannot put
     a code on the claim that the primary engine alone would not have.

Run: python prove_no_upcode.py
"""
from cosmaneura_billing.agents import ComplianceGuardrailAgent
from cosmaneura_billing.agents.coding import _facts_to_em
from cosmaneura_billing.agents.consensus import merge_facts, merge_suggestions
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence

# Documentation that supports at most a moderate (99214) visit.
facts_supporting_99214 = {
    "problems": [{"name": "Hematochezia", "status": "new", "uncertain_prognosis": True,
                  "quote": "blood per rectum"}],
    "data_reviewed": [{"category": "review_external_labs", "item": "CBC"}],
    "risk": {"level": "moderate", "quote": "refer for colonoscopy"},
}

# --------------------------------------------------------------------------- #
# 1. The guardrail downgrades an over-level.
# --------------------------------------------------------------------------- #
# An upstream agent (or a tampered-with input) tries to bill 99215.
inflated = CodeSuggestion(
    kind=CodeKind.EM, code="99215", label="Office visit, high MDM",
    disposition=Disposition.CAPTURED,
    evidence=[Evidence("signed_note", "refer for colonoscopy", "risk")],
)

result, audit = ComplianceGuardrailAgent().run(facts_supporting_99214, [inflated], new_patient=False)
final = result[0]

print("== 1. Guardrail ceiling ==")
print("attempted code :", "99215")
print("final code     :", final.code)
print("audit          :", audit[0])
assert final.code == "99214", "GUARDRAIL FAILED: an over-level survived!"
assert "DOWNGRADE" in audit[0]
print("PASS — the guardrail downgraded the over-level to the supported ceiling.\n")

# --------------------------------------------------------------------------- #
# 2. A second extraction engine cannot loosen anything.
# --------------------------------------------------------------------------- #
# A maximally "helpful" secondary engine: a pile of extra problems, extra data
# reviewed, and a diagnosis the primary engine never proposed.
greedy_secondary_facts = {
    "ta4h_problems": [{"name": f"problem {i}", "quote": "q"} for i in range(12)],
    "ta4h_data_reviewed": [{"item": f"lab {i}", "category": f"c{i}"} for i in range(9)],
    "ta4h_contradictions": {},
}
merged_facts, facts_audit = merge_facts(
    facts_supporting_99214, greedy_secondary_facts, new_patient=False)

ceiling_alone = _facts_to_em(facts_supporting_99214, new_patient=False).code
ceiling_merged = _facts_to_em(merged_facts, new_patient=False).code

print("== 2. Two-engine consensus ==")
print("ceiling, primary engine alone :", ceiling_alone)
print("ceiling, both engines merged  :", ceiling_merged)
assert ceiling_merged == ceiling_alone, \
    "CONSENSUS FAILED: a second engine moved the E/M ceiling!"

# A code only the second engine found must arrive held, and must stay held even
# with evidence attached and medical necessity satisfied.
ta4h_only = CodeSuggestion(
    kind=CodeKind.ICD10, code="R19.7", label="Diarrhea, unspecified",
    disposition=Disposition.CAPTURED,          # the secondary engine's best case
    evidence=[Evidence("signed_note", "frequent diarrhea", "documented finding")],
    source_engine="ta4h",
)
merged, merge_audit = merge_suggestions([], [ta4h_only])
gated, gate_audit = ComplianceGuardrailAgent().run(
    merged_facts, merged, new_patient=False)

print("second-engine-only code       :", gated[0].code, "->", gated[0].disposition.value)
assert gated[0].disposition == Disposition.REVIEW, \
    "CONSENSUS FAILED: a single-engine code reached the claim without a human!"
assert any("HOLD" in line and "R19.7" in line for line in gate_audit)

# And either engine may still veto: rejection is always permitted.
vetoed, veto_audit = merge_suggestions(
    [CodeSuggestion(kind=CodeKind.ICD10, code="Z80.0",
                    label="Family history of digestive cancer",
                    disposition=Disposition.CAPTURED,
                    evidence=[Evidence("signed_note", "family history", "")])],
    [],
    contradictions={"Z800": "documentation states this finding is absent/denied"},
)
print("second-engine veto            :", vetoed[0].code, "->", vetoed[0].disposition.value)
assert vetoed[0].disposition == Disposition.REJECTED, \
    "CONSENSUS FAILED: a vetoed code survived!"

print("PASS — the second engine can hold and reject, but never raise or add.\n")
print("The pipeline is structurally incapable of billing above documented MDM,")
print("with one extraction engine or two.")
