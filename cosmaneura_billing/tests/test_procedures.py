"""Procedure / add-on code candidates — CPT 99058 (rapid access).

Requested by MCD's billing team on 2026-08-13: missing 99058 on rapid-access
visits is lost revenue. Before this the pipeline could not propose a
PROCEDURE-kind code at all.
"""
import pytest

from cosmaneura_billing.knowledge.procedures import ProcedureRules
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, Disposition, VisitBundle

WORKED_IN = ("Patient called at 0800 with acute symptoms and we worked her in at "
             "1030. HPI: as above. Plan: reassess in one week.")


def _bundle(note: str, *, pos: str = "11", **kw) -> VisitBundle:
    return VisitBundle(
        encounter_id="PROC-1", patient_age=44, patient_sex="female",
        encounter_type=kw.pop("encounter_type", "office_established"),
        place_of_service=pos, signed_note=note, **kw)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_proposes_99058_from_documented_work_in():
    out, audit = ProcedureRules().propose(_bundle(WORKED_IN))
    assert [s.code for s in out] == ["99058"]
    s = out[0]
    assert s.kind == CodeKind.PROCEDURE
    assert s.disposition == Disposition.REVIEW, "add-ons are never auto-captured"
    assert any("PROPOSE 99058" in line for line in audit)


def test_evidence_quote_is_verbatim_from_the_documentation():
    """A reconstructed quote would fail the evidence verifier and be stripped."""
    bundle = _bundle(WORKED_IN)
    out, _ = ProcedureRules().propose(bundle)
    quote = out[0].evidence[0].quote
    assert quote in bundle.signed_note
    assert "worked her in" in quote


def test_99058_carries_the_confirmation_hold():
    out, _ = ProcedureRules().propose(_bundle(WORKED_IN))
    assert "requires_provider_confirmation" in out[0].notes
    assert "cms_mue_0" in out[0].notes


@pytest.mark.parametrize("phrase", [
    "Seen on an emergency basis, disrupting the scheduled clinic visits.",
    "Rapid-access appointment created for today.",
    "Unscheduled same-day evaluation for worsening pain.",
])
def test_alternative_documented_phrasings_trigger(phrase):
    out, _ = ProcedureRules().propose(_bundle(phrase))
    assert [s.code for s in out] == ["99058"]


# --------------------------------------------------------------------------- #
# Conservatism — the billing team's constraint is that coder minutes go DOWN
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("note", [
    "Routine follow-up visit. Patient doing well.",
    "Discussed the urgency of starting therapy soon.",       # 'urgen*' alone
    "Urgent care referral discussed for after-hours needs.",  # unrelated 'urgent'
    "Patient works in construction and reports back pain.",   # 'works in'
])
def test_does_not_fire_on_ordinary_prose(note):
    out, _ = ProcedureRules().propose(_bundle(note))
    assert out == [], f"false positive on: {note!r}"


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def test_telehealth_place_of_service_blocks_99058():
    """99058 is defined as a service provided *in the office*. Proposing it on a
    telehealth encounter invites the denial the practice is already fighting."""
    out, audit = ProcedureRules().propose(
        _bundle(WORKED_IN, pos="10", encounter_type="telehealth_established"))
    assert out == []
    assert any("SKIP    99058" in line and "place of service 10" in line for line in audit)


def test_add_on_requires_an_em_service():
    out, audit = ProcedureRules().propose(_bundle(WORKED_IN), has_em=False)
    assert out == []
    assert any("requires an E/M service" in line for line in audit)


# --------------------------------------------------------------------------- #
# End to end through the real pipeline
# --------------------------------------------------------------------------- #
def test_pipeline_surfaces_99058_held_with_evidence():
    bundle = _bundle(
        "26 y/o established patient worked in on an emergency basis this morning. "
        "When I eat spicy food, I poop blood. Plan: refer you for a colonoscopy to be safe.",
        pos="11")
    result = BillingOrchestrator().process(bundle)

    procs = [s for s in result.provider_card.declined_codes
             if s.kind == CodeKind.PROCEDURE]
    assert [s.code for s in procs] == ["99058"], (
        "99058 must reach the reviewer as a held candidate")
    assert procs[0].evidence, "a held add-on must still show why it was proposed"
    # never silently on the claim
    assert not [s for s in result.biller_worksheet.procedures if s.code == "99058"]
    assert any("HOLD    99058" in line and "provider must confirm" in line
               for line in result.biller_worksheet.audit_trail)
