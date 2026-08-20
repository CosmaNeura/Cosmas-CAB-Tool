"""The two-engine consensus invariant, as a property rather than one scenario.

`prove_no_upcode.py` demonstrates it on the demo encounter; this suite attacks
it. The two regression tests below are bugs this fuzzer actually found on
2026-08-19, both of which every hand-written consensus test passed straight
through — including the end-to-end two-engine pipeline test.
"""
import pytest

from cosmaneura_billing.agents import ComplianceGuardrailAgent
from cosmaneura_billing.agents.consensus import merge_suggestions
from cosmaneura_billing.consensus_fuzz import run_consensus_fuzz
from cosmaneura_billing.schemas import (
    CodeKind, CodeSuggestion, Disposition, Evidence,
)

FACTS_99214 = {
    "problems": [{"name": "Hematochezia", "status": "new", "uncertain_prognosis": True,
                  "quote": "blood per rectum"}],
    "data_reviewed": [{"category": "review_external_labs", "item": "CBC"}],
    "risk": {"level": "moderate", "quote": "refer for colonoscopy"},
}


# --------------------------------------------------------------------------- #
# The property
# --------------------------------------------------------------------------- #
def test_consensus_holds_across_adversarial_cases():
    violations = run_consensus_fuzz(400)
    assert not violations, "\n".join(violations[:10])


@pytest.mark.parametrize("seed", [1, 20260819, 999_983])
def test_consensus_holds_under_different_seeds(seed):
    violations = run_consensus_fuzz(150, seed=seed)
    assert not violations, "\n".join(violations[:10])


# --------------------------------------------------------------------------- #
# Regressions the fuzzer found
# --------------------------------------------------------------------------- #
def test_secondary_engine_em_code_is_dropped_not_held():
    """Found by the fuzzer: a non-diagnosis code from the secondary engine used
    to pass through with a consensus_hold note — but the guardrail's E/M branch
    does not consult those notes, so it was promoted to CAPTURED on its own
    evidence. The secondary engine's contract is diagnoses only, so anything else
    is dropped before the guardrail ever sees it."""
    rogue_em = CodeSuggestion(
        kind=CodeKind.EM, code="99215", label="Office visit, high MDM",
        disposition=Disposition.CAPTURED,
        evidence=[Evidence("signed_note", "refer for colonoscopy", "risk")],
        source_engine="ta4h",
    )
    merged, audit = merge_suggestions([], [rogue_em])
    assert merged == [], "an out-of-contract secondary code must not reach the guardrail"
    assert any("CONSENSUS-DROPPED" in line and "99215" in line for line in audit)

    out, _ = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out == []


def test_secondary_engine_procedure_code_is_dropped():
    rogue_proc = CodeSuggestion(
        kind=CodeKind.PROCEDURE, code="99058", label="Office emergency",
        disposition=Disposition.CAPTURED,
        evidence=[Evidence("signed_note", "worked in urgently", "")],
        source_engine="ta4h",
    )
    merged, audit = merge_suggestions([], [rogue_proc])
    assert merged == []
    assert any("CONSENSUS-DROPPED" in line for line in audit)


def test_secondary_evidence_never_supplies_the_first_span():
    """Found by the fuzzer: merging the second engine's evidence into a primary
    code that had none turned a HELD code into a CAPTURED one — evidence is the
    guardrail's necessity gate. The second engine may enrich a span, never
    create the first one."""
    unevidenced = CodeSuggestion(
        kind=CodeKind.ICD10, code="Z80.0", label="Family history of digestive cancer",
        disposition=Disposition.REVIEW, evidence=[],
    )
    twin = CodeSuggestion(
        kind=CodeKind.ICD10, code="Z80.0", label="Family history of digestive cancer",
        disposition=Disposition.REVIEW, source_engine="ta4h",
        evidence=[Evidence("signed_note", "family history of colon cancer", "ta4h")],
    )
    merged, audit = merge_suggestions([unevidenced], [twin])
    assert merged[0].evidence == [], "second engine supplied the first evidence span"
    assert any("ta4h_evidence_withheld" in n for n in merged[0].notes)
    assert any("held rather than captured" in line for line in audit)

    out, _ = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out[0].disposition != Disposition.CAPTURED


def test_secondary_evidence_still_enriches_an_evidenced_code():
    """The value case must survive the fix: an already-capturable code still ends
    up with two independent pointers into the chart."""
    evidenced = CodeSuggestion(
        kind=CodeKind.ICD10, code="K62.5", label="Hemorrhage of anus and rectum",
        disposition=Disposition.REVIEW,
        evidence=[Evidence("intake_text", "When I eat spicy food, I poop blood", "")],
    )
    twin = CodeSuggestion(
        kind=CodeKind.ICD10, code="K62.5", label="Hemorrhage of anus and rectum",
        disposition=Disposition.REVIEW, source_engine="ta4h",
        evidence=[Evidence("signed_note", "intermittent hematochezia", "ta4h")],
    )
    merged, _ = merge_suggestions([evidenced], [twin])
    assert len(merged[0].evidence) == 2
    assert merged[0].agreement == "both"
