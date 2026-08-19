"""End-to-end pipeline with the second extraction engine switched on.

The contract this file pins down: turning TA4H on must change what the reviewer
*sees* (provenance, extra held candidates, vetoes) without changing what the
engine is willing to *bill* above what one engine alone would bill.
"""
import pytest

from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition
from cosmaneura_billing.ta4h import MockTA4H
from demo_visit import SAMPLE_VISIT


@pytest.fixture(scope="module")
def single():
    return BillingOrchestrator(ta4h=None).process(SAMPLE_VISIT)


@pytest.fixture(scope="module")
def dual():
    return BillingOrchestrator(ta4h=MockTA4H()).process(SAMPLE_VISIT)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_second_engine_is_off_unless_asked_for():
    assert BillingOrchestrator(ta4h=None).multi_engine is False
    assert BillingOrchestrator(ta4h=MockTA4H()).multi_engine is True


def test_coding_agent_is_injectable():
    """The DI seam the multi-engine work needed: a substitute primary coder."""
    class StubCoder:
        def run(self, bundle):
            return {"problems": [], "data_reviewed": [], "risk": {}}, []

    result = BillingOrchestrator(coding=StubCoder(), ta4h=None).process(SAMPLE_VISIT)
    assert result.biller_worksheet.diagnoses == []


def test_secondary_engine_failure_degrades_to_single_engine():
    """A TA4H outage must never fail an encounter."""
    class BrokenTA4H:
        def analyze(self, documents):
            raise RuntimeError("service unavailable")

    result = BillingOrchestrator(ta4h=BrokenTA4H()).process(SAMPLE_VISIT)
    audit = "\n".join(result.biller_worksheet.audit_trail)
    assert "secondary extraction engine unavailable" in audit
    assert result.biller_worksheet.em_code.code == "99214"


# --------------------------------------------------------------------------- #
# The no-upcode invariant holds with two engines
# --------------------------------------------------------------------------- #
def test_em_level_identical_with_and_without_the_second_engine(single, dual):
    assert dual.biller_worksheet.em_code.code == single.biller_worksheet.em_code.code == "99214"


def test_second_engine_captures_no_code_the_first_did_not(single, dual):
    """Agreement may confirm a code; it may never introduce a billable one."""
    before = {s.code for s in single.provider_card.captured_codes}
    after = {s.code for s in dual.provider_card.captured_codes}
    assert after <= before, f"second engine introduced captured code(s): {after - before}"


def test_ta4h_only_codes_are_visible_but_never_billed(dual):
    ta4h_only = [s for s in dual.provider_card.declined_codes
                 if s.agreement == "ta4h_only"]
    captured = {s.code for s in dual.provider_card.captured_codes}
    for s in ta4h_only:
        assert s.disposition != Disposition.CAPTURED
        assert s.code not in captured


def test_contradicted_family_history_still_never_billed(dual):
    captured = {s.code for s in dual.provider_card.captured_codes}
    assert not any(c.startswith("Z80") for c in captured)


def test_every_captured_code_still_has_evidence(dual):
    for s in dual.provider_card.captured_codes:
        assert s.has_evidence, f"{s.code} captured without a documentation span"


# --------------------------------------------------------------------------- #
# What the reviewer gains
# --------------------------------------------------------------------------- #
def test_audit_trail_opens_with_the_engine_split(dual):
    first = dual.biller_worksheet.audit_trail[0]
    assert first.startswith("CONSENSUS two extraction engines ran")


def test_agreed_codes_are_tagged_for_the_reviewer(dual):
    all_codes = dual.provider_card.captured_codes + dual.provider_card.declined_codes
    dx = [s for s in all_codes if s.kind == CodeKind.ICD10]
    assert dx, "expected diagnosis suggestions"
    assert all(s.agreement for s in dx), "every diagnosis needs an agreement tag"
    assert "both" in {s.agreement for s in dx}


def test_agreed_codes_carry_two_independent_evidence_spans(dual):
    agreed = [s for s in dual.provider_card.captured_codes if s.agreement == "both"]
    assert agreed, "the demo encounter should produce at least one agreed code"
    assert any(len(s.evidence) >= 2 for s in agreed), \
        "an agreed code should carry both engines' pointers into the chart"


def test_single_engine_output_carries_no_agreement_noise(single):
    for s in single.provider_card.captured_codes + single.provider_card.declined_codes:
        assert s.agreement == ""
        assert not any(n.startswith("consensus_hold") for n in s.notes)
