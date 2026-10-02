"""The core compliance invariant: the guardrail can keep, downgrade, or remove —
never raise a level, add a code, or capture a code without evidence."""
from cosmaneura_billing.agents import ComplianceGuardrailAgent
from cosmaneura_billing.corpus.mdm_cases import qualified_case
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence

FACTS_99214, QUALIFIED_EVIDENCE, _ = qualified_case(4)


def _em(code, evidence=True, disposition=Disposition.CAPTURED):
    return CodeSuggestion(
        kind=CodeKind.EM, code=code, label=f"Office visit {code}",
        disposition=disposition,
        evidence=list(QUALIFIED_EVIDENCE) if evidence else [],
    )


def test_inflated_em_is_downgraded_to_ceiling():
    result, audit = ComplianceGuardrailAgent().run(FACTS_99214, [_em("99215")], new_patient=False)
    assert result[0].code == "99214"
    assert any("DOWNGRADE" in line for line in audit)


def test_em_at_or_below_ceiling_is_never_raised():
    for code in ("99212", "99213", "99214"):
        result, _ = ComplianceGuardrailAgent().run(FACTS_99214, [_em(code)], new_patient=False)
        assert result[0].code == code, "guardrail must never raise a level"
        assert result[0].disposition == Disposition.CAPTURED


def test_em_without_evidence_is_held_not_captured():
    result, audit = ComplianceGuardrailAgent().run(FACTS_99214, [_em("99214", evidence=False)],
                                                   new_patient=False)
    assert result[0].disposition == Disposition.REVIEW
    assert any("HOLD" in line for line in audit)


def test_rejected_codes_stay_rejected():
    s = _em("99214", disposition=Disposition.REJECTED)
    result, audit = ComplianceGuardrailAgent().run(FACTS_99214, [s], new_patient=False)
    assert result[0].disposition == Disposition.REJECTED
    assert any("REJECT" in line for line in audit)


def test_unresolved_dx_is_held():
    s = CodeSuggestion(kind=CodeKind.ICD10, code="(unresolved)", label="mystery concept")
    result, audit = ComplianceGuardrailAgent().run(FACTS_99214, [s], new_patient=False)
    assert result[0].disposition == Disposition.REVIEW


def test_dx_without_evidence_is_held():
    s = CodeSuggestion(kind=CodeKind.ICD10, code="K62.5", label="Hemorrhage of anus and rectum")
    result, _ = ComplianceGuardrailAgent().run(FACTS_99214, [s], new_patient=False)
    assert result[0].disposition == Disposition.REVIEW


def test_dx_with_evidence_is_captured_with_audit():
    s = CodeSuggestion(kind=CodeKind.ICD10, code="K62.5", label="Hemorrhage of anus and rectum",
                       evidence=[Evidence("intake_text", "I poop blood", "documented symptom")])
    result, audit = ComplianceGuardrailAgent().run(FACTS_99214, [s], new_patient=False)
    assert result[0].disposition == Disposition.CAPTURED
    assert result[0].necessity_supported
    assert any("CAPTURE K62.5" in line for line in audit)


def test_every_decision_is_audited():
    suggestions = [_em("99215"), _em("99213"),
                   CodeSuggestion(kind=CodeKind.ICD10, code="(unresolved)", label="x")]
    _, audit = ComplianceGuardrailAgent().run(FACTS_99214, suggestions, new_patient=False)
    # one DOWNGRADE plus one line per suggestion disposition
    assert len(audit) >= len(suggestions)
