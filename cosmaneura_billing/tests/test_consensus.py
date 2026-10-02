"""Consensus policy: the ensemble can only ever be more conservative.

The three properties under test are the ones a compliance officer has to be able
to trust when a second engine is switched on:
  * the E/M ceiling never moves;
  * a code only one engine found cannot reach a claim without a human;
  * either engine can veto.
"""
import pytest

from cosmaneura_billing.agents import ComplianceGuardrailAgent, ConsensusAgent
from cosmaneura_billing.agents.coding import _facts_to_em
from cosmaneura_billing.agents.consensus import merge_facts, merge_suggestions
from cosmaneura_billing.corpus.mdm_cases import qualified_case
from cosmaneura_billing.schemas import (
    CodeKind, CodeSuggestion, Disposition, Evidence,
)

FACTS_99214, _, _ = qualified_case(4)


def dx(code, label="dx", disposition=Disposition.REVIEW, engine="", quote="blood per rectum"):
    return CodeSuggestion(
        kind=CodeKind.ICD10, code=code, label=label, disposition=disposition,
        evidence=[Evidence("signed_note", quote, "finding")] if quote else [],
        source_engine=engine,
    )


# --------------------------------------------------------------------------- #
# 1. The E/M ceiling never moves
# --------------------------------------------------------------------------- #
def test_secondary_facts_cannot_raise_the_ceiling():
    """Even a secondary engine reporting a pile of extra problems and high risk
    must not move the level — its keys are namespaced out of the grid."""
    greedy_secondary = {
        "ta4h_problems": [{"name": f"problem {i}", "quote": "q"} for i in range(12)],
        "ta4h_data_reviewed": [{"item": f"lab {i}", "category": f"c{i}"} for i in range(9)],
        "ta4h_contradictions": {},
    }
    merged, audit = merge_facts(FACTS_99214, greedy_secondary, new_patient=False)
    assert _facts_to_em(merged, new_patient=False).code == \
        _facts_to_em(FACTS_99214, new_patient=False).code == "99214"
    assert not audit


def test_non_namespaced_secondary_facts_are_dropped_and_audited():
    """Defence in depth: a leveling key smuggled in from the secondary engine is
    refused, not merged."""
    smuggled = {
        "problems": [{"name": "p", "uncertain_prognosis": True} for _ in range(5)],
        "risk": {"level": "high"},
        "ta4h_problems": [],
    }
    merged, audit = merge_facts(FACTS_99214, smuggled, new_patient=False)
    assert merged["problems"] == FACTS_99214["problems"]
    assert merged["risk"] == FACTS_99214["risk"]
    assert _facts_to_em(merged, new_patient=False).code == "99214"
    assert any("non-namespaced" in line for line in audit)
    assert sum("ignored non-namespaced" in line for line in audit) == 2


def test_ceiling_move_is_reverted_and_audited(monkeypatch):
    """If a future edit ever did let the ceiling move, the clamp catches it.

    The leveling call is faked rather than fed drifting facts, because by design
    no input *can* move the ceiling through ``merge_facts`` — this exercises the
    clamp itself, which exists to catch a future regression that broke that.
    """
    import cosmaneura_billing.agents.consensus as consensus

    class _Level:
        def __init__(self, code):
            self.code = code

    calls = {"n": 0}

    def drifting(facts, *, new_patient):
        calls["n"] += 1
        return _Level("99215" if calls["n"] == 2 else "99214")

    monkeypatch.setattr(consensus, "_facts_to_em", drifting)
    merged, audit = merge_facts(FACTS_99214, {"ta4h_problems": []}, new_patient=False)
    assert "ta4h_problems" not in merged, "clamp must fall back to primary facts only"
    assert merged == FACTS_99214
    assert any("99214 -> 99215" in line for line in audit)
    assert any("reverted to primary-engine facts" in line for line in audit)


# --------------------------------------------------------------------------- #
# 2. Single-engine codes are held, agreed codes are eligible
# --------------------------------------------------------------------------- #
def test_agreed_code_is_tagged_both_and_left_capturable():
    merged, audit = merge_suggestions([dx("K62.5")], [dx("K62.5", engine="ta4h")])
    assert len(merged) == 1
    assert merged[0].agreement == "both"
    assert merged[0].source_engine == "both"
    assert not any(n.startswith("consensus_hold") for n in merged[0].notes)
    assert any("both engines agree" in line for line in audit)


def test_primary_only_code_is_held():
    merged, _ = merge_suggestions([dx("K59.00")], [])
    assert merged[0].agreement == "primary_only"
    assert any(n.startswith("consensus_hold") for n in merged[0].notes)


def test_secondary_only_code_is_added_but_held():
    merged, _ = merge_suggestions([], [dx("R19.7", engine="ta4h")])
    assert len(merged) == 1
    assert merged[0].agreement == "ta4h_only"
    assert merged[0].disposition == Disposition.REVIEW
    assert any(n.startswith("consensus_hold") for n in merged[0].notes)


def test_single_engine_hold_survives_the_guardrail():
    """The end-to-end guarantee: a held code cannot be promoted to CAPTURED even
    with perfect evidence and necessity."""
    merged, _ = merge_suggestions([], [dx("R19.7", engine="ta4h")])
    out, audit = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out[0].disposition == Disposition.REVIEW
    assert any("HOLD" in line and "R19.7" in line for line in audit)


def test_agreed_code_can_still_be_captured_by_the_guardrail():
    """Agreement doesn't capture a code — but it doesn't block one either."""
    merged, _ = merge_suggestions([dx("K62.5")], [dx("K62.5", engine="ta4h")])
    out, _ = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out[0].disposition == Disposition.CAPTURED


def test_clinical_hold_reason_outranks_the_engine_split():
    """A code held for a clinical reason must say so — the engine split is the
    less useful explanation for a coder, so it must not shadow the real one."""
    unaddressed = dx("E66.8", label="Other obesity")
    unaddressed.notes.append("not_addressed_in_plan")
    merged, _ = merge_suggestions([unaddressed], [])
    out, audit = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out[0].disposition == Disposition.REVIEW
    hold = next(line for line in audit if line.startswith("HOLD"))
    assert "not addressed in plan" in hold
    assert "primary engine only" not in hold


def test_evidence_free_code_reports_necessity_not_the_split():
    no_evidence = dx("R19.7", quote="")
    merged, _ = merge_suggestions([no_evidence], [])
    out, audit = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out[0].disposition == Disposition.REVIEW
    hold = next(line for line in audit if line.startswith("HOLD"))
    assert "no documented finding" in hold


def test_preference_hold_still_outranks_everything():
    held = dx("K59.00")
    held.notes.append("preference_hold: you've declined this 3x")
    merged, _ = merge_suggestions([held], [dx("K59.00", engine="ta4h")])
    out, audit = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out[0].disposition == Disposition.REVIEW
    assert any("declined this 3x" in line for line in audit)


def test_codes_match_across_decimal_formatting():
    merged, _ = merge_suggestions([dx("K62.5")], [dx("K625", engine="ta4h")])
    assert len(merged) == 1 and merged[0].agreement == "both"


def test_agreement_merges_both_engines_evidence():
    merged, _ = merge_suggestions(
        [dx("K62.5", quote="bright red blood per rectum")],
        [dx("K62.5", engine="ta4h", quote="intermittent hematochezia")])
    quotes = {e.quote for e in merged[0].evidence}
    assert quotes == {"bright red blood per rectum", "intermittent hematochezia"}


def test_more_conservative_disposition_wins_with_its_reason():
    secondary = dx("E66.8", engine="ta4h", disposition=Disposition.REJECTED)
    secondary.rationale = "Azure Text Analytics for health: finding is hypothetical."
    merged, _ = merge_suggestions([dx("E66.8", disposition=Disposition.CAPTURED)], [secondary])
    assert merged[0].disposition == Disposition.REJECTED
    # a rejection has to carry its reason into the audit trail
    assert "hypothetical" in merged[0].rationale


# --------------------------------------------------------------------------- #
# 3. Either engine may veto
# --------------------------------------------------------------------------- #
def test_secondary_veto_rejects_a_primary_code():
    merged, audit = merge_suggestions(
        [dx("Z80.0", disposition=Disposition.CAPTURED)], [],
        contradictions={"Z800": "documentation states this finding is absent/denied"})
    assert merged[0].disposition == Disposition.REJECTED
    assert merged[0].agreement == "contradicted_by_ta4h"
    assert any("CONSENSUS-VETO" in line for line in audit)


def test_vetoed_code_stays_rejected_through_the_guardrail():
    merged, _ = merge_suggestions(
        [dx("Z80.0", disposition=Disposition.CAPTURED)], [],
        contradictions={"Z800": "attributed to a family member"})
    out, _ = ComplianceGuardrailAgent().run(FACTS_99214, merged, new_patient=False)
    assert out[0].disposition == Disposition.REJECTED


# --------------------------------------------------------------------------- #
# E/M passthrough + summary line
# --------------------------------------------------------------------------- #
def test_em_suggestion_passes_through_untouched():
    em = CodeSuggestion(kind=CodeKind.EM, code="99214", label="office visit",
                        evidence=[Evidence("signed_note", "refer for colonoscopy", "risk")])
    merged, _ = merge_suggestions([em], [dx("K62.5", engine="ta4h")])
    passed = next(s for s in merged if s.kind == CodeKind.EM)
    assert passed.code == "99214"
    assert passed.agreement == ""                 # no agreement claim on E/M
    assert passed.source_engine == "primary"
    assert not any(n.startswith("consensus_hold") for n in passed.notes)


def test_consensus_agent_summarizes_the_split():
    facts, suggestions, audit = ConsensusAgent().run(
        (FACTS_99214, [dx("K62.5"), dx("K59.00")]),
        ({"ta4h_contradictions": {}}, [dx("K62.5", engine="ta4h"), dx("R19.7", engine="ta4h")]),
        new_patient=False,
    )
    assert audit[0].startswith("CONSENSUS two extraction engines ran")
    assert "1 code(s) agreed" in audit[0]
    assert {s.agreement for s in suggestions} == {"both", "primary_only", "ta4h_only"}
    assert _facts_to_em(facts, new_patient=False).code == "99214"
