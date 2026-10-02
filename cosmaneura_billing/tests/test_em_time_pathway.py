"""The 2021+ total-time E/M pathway, end to end.

Requested because family-medicine televisits are frequently time-coded (Maura
Buete, 2026-07-24) and Sherin George referenced 99215 (2026-08-10).

The safety property is the interesting part. `total_time_minutes` arrives from an
extraction model; on its own it is a number, not documentation. Before this the
ruleset raised the level on that number alone, so a hallucinated time could
produce a 99215 with nothing in the chart supporting it — an upcode the guardrail
could not catch, because the guardrail's own ceiling was computed from the same
unverified number. The pathway now requires a quote that verifies against the
chart, and fails closed to MDM when it doesn't.
"""
import pytest

from cosmaneura_billing.agents.coding import CodingAgent, sanitize_facts
from cosmaneura_billing.knowledge.em_ruleset import (
    level_by_time, level_office_visit, max_defensible_level,
)
from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
from cosmaneura_billing.schemas import CodeKind, VisitBundle

NOTE_WITH_TIME = (
    "Established patient seen for medication review. Straightforward decision "
    "making. Total time spent on the date of the encounter: 42 minutes."
)


class _FactsLLM:
    """Returns a fixed facts payload so the leveling path can be tested directly."""
    mode = "mock"
    model_label = "test"

    def __init__(self, facts):
        self.facts = facts

    def complete_json(self, system, user, *, max_tokens=8000, schema=None):
        if "extract_clinical_facts" in user:
            return dict(self.facts)
        return {"diagnoses": []}


def _bundle(note: str) -> VisitBundle:
    return VisitBundle(
        encounter_id="TIME-1", patient_age=55, patient_sex="female",
        encounter_type="office_established", place_of_service="11", signed_note=note)


def _em(bundle, facts):
    _, suggestions = CodingAgent(_FactsLLM(facts), ICD10CCSR()).run(bundle)
    return next(s for s in suggestions if s.kind == CodeKind.EM)


LOW_MDM_FACTS = {
    "chief_complaint": "medication review",
    "problems": [{"name": "hypertension", "status": "stable", "quote": ""}],
    "data_reviewed": [],
    "risk": {"level": "straightforward", "quote": ""},
}


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("minutes,expected", [
    (5, None), (10, "99212"), (19, "99212"), (20, "99213"),
    (30, "99214"), (39, "99214"), (40, "99215"), (75, "99215"),
])
def test_established_patient_time_thresholds(minutes, expected):
    assert level_by_time(new_patient=False, total_time_minutes=minutes) == expected


@pytest.mark.parametrize("minutes,expected", [
    (10, None), (15, "99202"), (30, "99203"), (45, "99204"), (60, "99205"),
])
def test_new_patient_time_thresholds(minutes, expected):
    assert level_by_time(new_patient=True, total_time_minutes=minutes) == expected


@pytest.mark.parametrize("bad", [0, -5, None, True, 12.5, "30"])
def test_degenerate_times_are_rejected(bad):
    assert level_by_time(new_patient=False, total_time_minutes=bad) is None


# --------------------------------------------------------------------------- #
# The evidence gate, through the coding agent
# --------------------------------------------------------------------------- #
def test_verified_time_quote_raises_the_level_and_carries_its_evidence():
    facts = {**LOW_MDM_FACTS, "total_time_minutes": 42,
             "total_time_quote": "Total time spent on the date of the encounter: 42 minutes."}
    em = _em(_bundle(NOTE_WITH_TIME), facts)
    assert em.code == "99215"
    assert "em_pathway:total_time" in em.notes
    # derived MDM for these facts is the straightforward floor
    assert "em_mdm_code:99212" in em.notes
    assert "em_total_time_minutes:42" in em.notes
    assert "42 minutes" in em.evidence[0].quote, (
        "the sentence stating the minutes must lead the evidence")


def test_time_with_no_quote_falls_back_to_mdm():
    facts = {**LOW_MDM_FACTS, "total_time_minutes": 42, "total_time_quote": ""}
    em = _em(_bundle(NOTE_WITH_TIME), facts)
    assert em.code == "99212"
    assert "em_pathway:mdm_2of3" in em.notes


def test_hallucinated_time_quote_cannot_raise_the_level():
    """The quote does not appear in the note, so it does not verify."""
    facts = {**LOW_MDM_FACTS, "total_time_minutes": 42,
             "total_time_quote": "Total time: 42 minutes of face-to-face care."}
    em = _em(_bundle("Established patient seen for medication review."), facts)
    assert em.code == "99212", "an unverifiable time quote must not level the visit"
    assert "em_pathway:mdm_2of3" in em.notes


def test_time_below_the_mdm_level_never_lowers_it():
    from dataclasses import replace
    from cosmaneura_billing.corpus.mdm_cases import qualified_case
    facts, _, source = qualified_case(4)
    time = "Total physician time on the date of service: 11 minutes."
    raw = {k: v for k, v in facts.items() if not k.startswith("_")}
    raw.update(total_time_minutes=11, total_time_quote=time)
    em = _em(replace(source, signed_note=source.signed_note + "\n\n" + time), raw)
    assert em.code == "99214"   # independently source-qualified problems/data
    assert "em_pathway:mdm_2of3" in em.notes


# --------------------------------------------------------------------------- #
# The guardrail ceiling moves with the pathway
# --------------------------------------------------------------------------- #
def test_ceiling_admits_evidenced_time():
    kw = dict(new_patient=False, problems="low", data="low", risk="straightforward")
    assert max_defensible_level(**kw) == "99213"
    assert max_defensible_level(**kw, total_time_minutes=42,
                                time_is_evidenced=True) == "99215"
    assert max_defensible_level(**kw, total_time_minutes=42,
                                time_is_evidenced=False) == "99213"


def test_sanitize_defaults_time_evidence_to_false():
    """Any path that skips the verification step must under-level, not trust the
    number."""
    out = sanitize_facts({"total_time_minutes": 90})
    assert out["total_time_evidenced"] is False
    assert level_office_visit(
        new_patient=False, problems="low", data="low", risk="straightforward",
        total_time_minutes=out["total_time_minutes"],
        time_is_evidenced=out["total_time_evidenced"]).code == "99213"
