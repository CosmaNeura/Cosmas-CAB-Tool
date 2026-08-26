"""BillingTabView (F1) — the provider billing surface.

The acceptance criteria are taken verbatim from the requirements synthesis §3 F1:

  * guardrail fuzz test green (see test_guardrail_fuzz.py)
  * no candidate renders without >= 1 evidence span
  * held/removed always carry a reason
  * dedupe verified (a multi-mapped finding renders once)
  * golden test on the demo encounter: 99214 surfaced with evidence over the
    selected 99213; the patient-denied family-history code rejected;
    documented-but-unaddressed obesity held
  * zero mutation of the clinical note object
"""
import dataclasses

from fastapi.testclient import TestClient

from cosmaneura_billing.api import app
from cosmaneura_billing.billing_tab import build_billing_tab_view
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.orchestrator import BillingOrchestrator

ENGINE_VERSION = "test-engine"


def _view(bundle=SAMPLE_VISIT):
    result = BillingOrchestrator().process(bundle)
    return build_billing_tab_view(
        bundle, result, engine_version=ENGINE_VERSION,
        generated_at="2026-08-17T00:00:00Z")


def _all_candidates(view):
    return view["icd_candidates"] + view["cpt_candidates"]


# --------------------------------------------------------------------------- #
# Acceptance criteria
# --------------------------------------------------------------------------- #
def test_no_actionable_candidate_renders_without_an_evidence_span():
    """Suggested and held codes are things a reviewer can act on, so each must
    show the words behind it."""
    view = _view()
    assert _all_candidates(view), "the demo encounter must produce candidates"
    actionable = [c for c in _all_candidates(view) if c["status"] != "removed"]
    assert actionable
    for cand in actionable:
        assert cand["evidence_spans"], f"{cand['code']} rendered with no evidence"
        assert all(s["text"].strip() for s in cand["evidence_spans"])


def test_removed_codes_are_shown_on_their_reason_not_their_evidence():
    """A code rejected as contradicted has no supporting span by construction.
    Requiring one would delete the most useful line in the view — 'we considered
    this and here is why not' — so removed codes are admitted on their reason."""
    removed = [c for c in _all_candidates(_view()) if c["status"] == "removed"]
    assert removed, "the demo encounter rejects the family-history code"
    for cand in removed:
        assert cand["status_reason"], f"{cand['code']} removed with no reason"


def test_held_and_removed_always_carry_a_reason():
    for cand in _all_candidates(_view()):
        if cand["status"] != "suggested":
            assert cand["status_reason"], (
                f"{cand['code']} is {cand['status']} with no reason given")


def test_golden_demo_encounter():
    """The engine's answer on the demo visit, as specced."""
    view = _view()

    # 99214 surfaced over the provider's selected 99213, with evidence
    em = view["em"]
    assert em["suggested_level"] == "99214"
    assert em["provider_selected_level"] == "99213"
    assert em["status"] == "suggested"
    assert em["evidence_spans"]
    assert em["pathway"] == "mdm_2of3"

    by_code = {c["code"]: c for c in view["icd_candidates"]}

    # patient explicitly denied the family history -> removed
    assert by_code["Z80.0"]["status"] == "removed"
    assert "denied" in by_code["Z80.0"]["status_reason"].lower()

    # documented in vitals but not addressed in the plan -> held
    obesity = next(c for c in view["icd_candidates"] if c["code"].startswith("E66"))
    assert obesity["status"] == "held"
    assert "not addressed" in obesity["status_reason"].lower()

    # the primary diagnosis leads
    assert view["icd_candidates"][0]["code"] == "K62.5"
    assert view["icd_candidates"][0]["primary"] is True
    assert all(c["primary"] is False for c in view["icd_candidates"][1:])


def test_the_clinical_note_is_never_modified():
    """Sherin's constraint: clinical tool first. The billing tab is a view."""
    before = dataclasses.asdict(SAMPLE_VISIT)
    view = _view()
    assert dataclasses.asdict(SAMPLE_VISIT) == before
    assert view["clinical_note_modified"] is False


def test_dedupe_renders_a_multi_mapped_finding_once():
    """Two codes citing the same documented span are one finding with alternates
    (Sherin's progesterone-deficiency case)."""
    from cosmaneura_billing.billing_tab import _dedupe

    span = [{"source": "signed_note", "text": "progesterone deficiency", "loc": None}]
    rows = _dedupe([
        {"code": "E28.39", "evidence_spans": span, "alternates": [], "status": "suggested"},
        {"code": "N91.2", "evidence_spans": list(span), "alternates": [], "status": "suggested"},
    ])
    assert len(rows) == 1, "a finding mapping to two codes must render once"
    assert rows[0]["code"] == "E28.39"
    assert rows[0]["alternates"] == ["N91.2"]


def test_distinct_findings_are_not_merged():
    from cosmaneura_billing.billing_tab import _dedupe

    rows = _dedupe([
        {"code": "K62.5", "alternates": [], "status": "suggested",
         "evidence_spans": [{"source": "s", "text": "poop blood", "loc": None}]},
        {"code": "K59.00", "alternates": [], "status": "suggested",
         "evidence_spans": [{"source": "s", "text": "constipation", "loc": None}]},
    ])
    assert [r["code"] for r in rows] == ["K62.5", "K59.00"]


def test_dedupe_keeps_the_server_selected_primary_as_the_visible_mapping():
    from cosmaneura_billing.billing_tab import _dedupe

    span = [{"source": "signed_note", "text": "shared finding", "loc": None}]
    rows = _dedupe([
        {"code": "A00.0", "evidence_spans": span, "alternates": [], "status": "suggested"},
        {"code": "B00.0", "evidence_spans": list(span), "alternates": [], "status": "suggested"},
    ], preferred_code="B00.0")
    assert len(rows) == 1
    assert rows[0]["code"] == "B00.0"
    assert rows[0]["alternates"] == ["A00.0"]


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #
def test_suggested_codes_lead_held_ones():
    statuses = [c["status"] for c in _view()["icd_candidates"]]
    first_held = statuses.index("held") if "held" in statuses else len(statuses)
    assert "suggested" not in statuses[first_held:], (
        f"a held code outranked a suggested one: {statuses}")


def test_coverage_ordering_never_drops_a_documented_code():
    """Ordering ranks and labels; it must not remove an evidence-backed code."""
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    rendered = {c["code"] for c in _all_candidates(_view())}
    considered = {s.code for s in
                  list(result.provider_card.captured_codes)
                  + list(result.provider_card.declined_codes)
                  if s.has_evidence}
    # every evidence-backed code is either rendered or folded in as an alternate
    alternates = {a for c in _all_candidates(_view()) for a in c["alternates"]}
    assert considered <= (rendered | alternates)


# --------------------------------------------------------------------------- #
# MDM facts and evidence attribution
# --------------------------------------------------------------------------- #
def test_mdm_facts_cover_the_three_elements():
    facts = {f["element"]: f for f in _view()["em"]["mdm_facts"]}
    assert set(facts) == {"problems", "data", "risk"}
    assert facts["risk"]["finding"] == "moderate"


def test_mdm_element_evidence_is_not_borrowed_from_another_element():
    """The risk quote must not be presented as evidence for `problems`. Where an
    element has no span of its own, the span is null rather than wrong."""
    facts = {f["element"]: f for f in _view()["em"]["mdm_facts"]}
    risk_span = facts["risk"]["evidence_span"]
    problems_span = facts["problems"]["evidence_span"]
    assert risk_span is not None
    assert problems_span is None or problems_span["text"] != risk_span["text"]
    # the data element has no evidence span in the engine today; say so honestly
    assert facts["data"]["evidence_span"] is None


def test_guardrail_actions_are_surfaced_for_the_em_code():
    actions = _view()["em"]["guardrail_actions"]
    assert actions and any("99214" in a for a in actions)


# --------------------------------------------------------------------------- #
# A/P decomposition and copy affordances
# --------------------------------------------------------------------------- #
def test_plan_items_link_to_the_code_that_justifies_them():
    from cosmaneura_billing.billing_tab import _plan_items
    from cosmaneura_billing.schemas import (
        CodeKind, CodeSuggestion, Disposition, Evidence, VisitBundle,
    )
    from cosmaneura_billing.billing_tab import _link_plan_items

    bundle = VisitBundle(
        encounter_id="AP-1", patient_age=41, patient_sex="female",
        encounter_type="office_established", place_of_service="11",
        signed_note=("Assessment: obesity; insulin resistance. "
                     "Plan: start semaglutide 0.25 mg weekly for obesity; "
                     "order fasting insulin for insulin resistance."))
    items = _plan_items(bundle)
    assert len(items) == 2

    obesity = CodeSuggestion(
        kind=CodeKind.ICD10, code="E66.9", label="Obesity, unspecified",
        disposition=Disposition.CAPTURED,
        evidence=[Evidence("signed_note", "obesity", "")])
    linked = _link_plan_items(obesity, items)
    assert any("semaglutide" in i for i in linked)
    assert not any("fasting insulin" in i for i in linked), (
        "a plan item must not attach to a code it does not justify")


def test_copy_all_contains_only_suggested_codes():
    view = _view()
    copy_all = view["copy_all"].split()
    suggested = {c["code"] for c in _all_candidates(view) if c["status"] == "suggested"}
    assert set(copy_all) == suggested
    assert "Z80.0" not in copy_all, "a removed code must never reach the clipboard"


def test_every_candidate_has_copy_text():
    for cand in _all_candidates(_view()):
        assert cand["copy_text"] == cand["code"]


# --------------------------------------------------------------------------- #
# Completeness is embedded per the F1 contract
# --------------------------------------------------------------------------- #
def test_office_encounter_has_no_rulepack_yet():
    assert _view()["completeness"]["rulepack"] is None


def test_telehealth_encounter_gets_the_telehealth_pack():
    import dataclasses as dc

    bundle = dc.replace(SAMPLE_VISIT, place_of_service="10",
                        encounter_type="telehealth_established")
    completeness = _view(bundle)["completeness"]
    assert completeness["rulepack"] == "telehealth.v1"
    assert completeness["complete"] is False, (
        "the demo note documents no telehealth metadata")
    assert completeness["attestation_block"] is None


# --------------------------------------------------------------------------- #
# API surface
# --------------------------------------------------------------------------- #
def test_billing_tab_endpoint():
    body = TestClient(app).post("/api/encounters/billing-tab", json={
        "encounterId": "ENC-100237",
        "generatedAt": "2026-08-17T00:00:00Z",
    }).json()
    assert body["visit_id"] == "ENC-100237"
    assert body["generated_at"] == "2026-08-17T00:00:00Z"
    assert body["em"]["suggested_level"] == "99214"
    assert body["inferenceMode"] == "mock"
    assert body["clinical_note_modified"] is False


def test_completeness_endpoint_and_override_validation():
    client = TestClient(app)
    ok = client.post("/api/completeness", json={
        "note": ("This visit was conducted via two-way audio-video telehealth. "
                 "Patient verbally consented to this telehealth visit. "
                 "Patient was at home in Nashua, NH. "
                 "Provider was at the office in Bow, NH. "
                 "Total time spent on the date of the encounter: 25 minutes."),
        "placeOfService": "10"}).json()
    assert ok["complete"] is True
    assert ok["attestation_block"]

    gaps = client.post("/api/completeness", json={
        "note": "Telehealth visit.", "placeOfService": "10"}).json()
    assert gaps["complete"] is False
    assert gaps["attestation_block"] is None

    rejected = client.post("/api/completeness", json={
        "note": "Telehealth visit.", "placeOfService": "10",
        "overrides": {"telehealth.duration": ""}})
    assert rejected.status_code == 422

    no_pack = client.post("/api/completeness", json={
        "note": "Office visit.", "placeOfService": "11",
        "encounterType": "office_established"}).json()
    assert no_pack["rulepack"] is None
