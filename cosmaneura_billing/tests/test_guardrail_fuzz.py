"""The anti-inflation invariant, as a property rather than one scenario.

`prove_no_upcode.py` demonstrates the invariant on the demo encounter; this suite
attacks it. The three regression tests below are bugs this fuzzer actually found
on 2026-08-17, each of which the single-scenario proof passed straight through.
"""
import pytest

from cosmaneura_billing.agents import ComplianceGuardrailAgent
from cosmaneura_billing.guardrail_fuzz import em_level, run_fuzz
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence

# Documentation supporting at most a moderate (99214) visit.
FACTS_99214 = {
    "problems": [{"name": "Hematochezia", "status": "new", "uncertain_prognosis": True,
                  "quote": "blood per rectum"}],
    "data_reviewed": [{"category": "review_external_labs", "item": "CBC"}],
    "risk": {"level": "moderate", "quote": "refer for colonoscopy"},
}


def _em(code, disposition=Disposition.CAPTURED, evidence=True):
    return CodeSuggestion(
        kind=CodeKind.EM, code=code, label="Office visit", disposition=disposition,
        evidence=[Evidence("signed_note", "refer for colonoscopy", "risk")] if evidence else [])


# --------------------------------------------------------------------------- #
# The property
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seed", [20260817, 1, 42, 99999])
def test_fuzz_no_invariant_violations(seed):
    violations = run_fuzz(2000, seed=seed)
    assert not violations, (
        f"{len(violations)} invariant violation(s); first 5:\n"
        + "\n".join(violations[:5]))


# --------------------------------------------------------------------------- #
# Regressions — each of these upcoded before 2026-08-17
# --------------------------------------------------------------------------- #
def test_modifier_suffixed_code_is_not_upcoded():
    """`99212-95` parsed as level 5 via its last character, so clamping it to a
    99213 ceiling *raised* a level-2 visit to level 3."""
    out, audit = ComplianceGuardrailAgent().run(
        FACTS_99214, [_em("99212-95")], new_patient=False)
    assert out[0].code == "99212-95", "the guardrail rewrote a code it cannot parse"
    assert out[0].disposition == Disposition.REVIEW
    assert any("not a recognizable office-visit E/M code" in line for line in audit)


def test_annotated_code_cannot_bypass_the_ceiling():
    """`99214 (telehealth)` parsed as level 0, so it never tripped the ceiling
    check and was captured above what the documentation supported."""
    facts_99213 = {**FACTS_99214, "risk": {"level": "low", "quote": "reassurance"},
                   "problems": [{"name": "Constipation", "status": "established",
                                 "quote": "constipation"}]}
    out, _ = ComplianceGuardrailAgent().run(
        facts_99213, [_em("99214 (telehealth)")], new_patient=False)
    assert out[0].disposition != Disposition.CAPTURED, (
        "an unverifiable E/M code was captured without its level being checked")


def test_procedure_code_is_not_captured_without_evidence():
    """PROCEDURE and MODIFIER kinds fell through the guardrail loop untouched, so
    a pre-marked-CAPTURED procedure with no documentation span stayed captured."""
    proc = CodeSuggestion(kind=CodeKind.PROCEDURE, code="99058", label="Rapid access",
                          disposition=Disposition.CAPTURED, evidence=[])
    out, audit = ComplianceGuardrailAgent().run(FACTS_99214, [proc], new_patient=False)
    assert out[0].disposition == Disposition.REVIEW
    assert any("no documentation span" in line for line in audit)


def test_procedure_requiring_confirmation_is_held_even_with_evidence():
    proc = CodeSuggestion(
        kind=CodeKind.PROCEDURE, code="99058", label="Rapid access",
        disposition=Disposition.CAPTURED,
        evidence=[Evidence("signed_note", "refer for colonoscopy", "x")],
        notes=["requires_provider_confirmation"])
    out, audit = ComplianceGuardrailAgent().run(FACTS_99214, [proc], new_patient=False)
    assert out[0].disposition == Disposition.REVIEW
    assert any("provider must confirm" in line for line in audit)


# --------------------------------------------------------------------------- #
# Monotonic clamp: higher proposals never yield higher outputs than the ceiling
# --------------------------------------------------------------------------- #
def test_clamp_is_monotonic_and_bounded():
    agent = ComplianceGuardrailAgent()
    levels = []
    for code in ("99211", "99212", "99213", "99214", "99215"):
        out, _ = agent.run(FACTS_99214, [_em(code)], new_patient=False)
        levels.append(em_level(out[0].code))
    assert levels == sorted(levels), f"clamp is not monotonic in the input: {levels}"
    assert max(levels) <= 4, f"a proposal escaped the 99214 ceiling: {levels}"


def test_empty_facts_cannot_support_any_capture():
    """No documented problems means no medical necessity — nothing is billable."""
    out, _ = ComplianceGuardrailAgent().run(
        {"problems": [], "data_reviewed": [], "risk": {}},
        [_em("99215")], new_patient=False)
    assert out[0].disposition == Disposition.REVIEW
    assert em_level(out[0].code) <= 2
