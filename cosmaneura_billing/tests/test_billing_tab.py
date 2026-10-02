"""BillingTabView (F1) — the provider billing surface.

The acceptance criteria are taken verbatim from the requirements synthesis §3 F1:

  * guardrail fuzz test green (see test_guardrail_fuzz.py)
  * no code renders without >= 1 exactly located evidence span
  * held/removed always carry a reason
  * dedupe verified (a multi-mapped finding renders once)
  * golden test on the demo encounter: unsupported E/M remains held;
    the patient-denied family-history code rejected;
    documented-but-unaddressed obesity held
  * zero mutation of the clinical note object
"""
import dataclasses

from fastapi.testclient import TestClient

from cosmaneura_billing.api import app
from cosmaneura_billing.billing_tab import build_billing_tab_view
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.corpus.mdm_cases import fixture_llm, qualified_case
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence

ENGINE_VERSION = "test-engine"


def _view(bundle=SAMPLE_VISIT):
    result = BillingOrchestrator().process(bundle)
    return build_billing_tab_view(
        bundle, result, engine_version=ENGINE_VERSION,
        generated_at="2026-08-17T00:00:00Z")


def _qualified_result():
    _, _, source = qualified_case(4)
    return source, BillingOrchestrator(llm=fixture_llm()).process(source)


def _qualified_view():
    source, result = _qualified_result()
    return build_billing_tab_view(source, result, engine_version=ENGINE_VERSION)


def _all_candidates(view):
    return view["icd_candidates"] + view["cpt_candidates"]


# --------------------------------------------------------------------------- #
# Acceptance criteria
# --------------------------------------------------------------------------- #
def test_no_candidate_renders_without_exactly_located_evidence():
    """Every visible code must point to the exact source text behind it."""
    view = _view()
    assert _all_candidates(view), "the demo encounter must produce candidates"
    sources = {
        "signed_note": SAMPLE_VISIT.signed_note,
        "transcript": SAMPLE_VISIT.transcript,
        "intake_text": SAMPLE_VISIT.intake_text,
    }
    for cand in _all_candidates(view):
        assert cand["evidence_spans"], f"{cand['code']} rendered with no evidence"
        for span in cand["evidence_spans"]:
            assert span["text"].strip()
            assert set(span["loc"]) == {"start", "end"}
            assert sources[span["source"]][
                span["loc"]["start"]:span["loc"]["end"]
            ] == span["text"]


def test_rejected_code_renders_only_with_exact_decision_evidence():
    """The denied concept retains exact decision evidence without a made-up code."""
    view = _view()
    candidate = next(
        row for row in _all_candidates(view) if row["description"].startswith("Family history")
    )
    quote = "I do not have a family history of colon cancer"
    start = SAMPLE_VISIT.intake_text.index(quote)
    assert candidate["status"] == "removed"
    assert candidate["code"] == "(unresolved)"
    assert "explicitly denied" in candidate["status_reason"]
    assert candidate["evidence_spans"] == [{
        "source": "intake_text",
        "text": quote,
        "loc": {"start": start, "end": start + len(quote)},
    }]
    assert view["withheld_without_evidence"] == 0  # care-only E/M evidence now permits held review
    assert view["withheld_without_reason"] == 0


def test_rejected_code_without_evidence_is_suppressed_not_rendered_from_reason():
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    result.provider_card.declined_codes.append(CodeSuggestion(
        kind=CodeKind.ICD10,
        code="Z99.89",
        label="Synthetic evidence-free rejection",
        disposition=Disposition.REJECTED,
        rationale="Synthetic rejection reason.",
    ))

    view = build_billing_tab_view(
        SAMPLE_VISIT, result, engine_version=ENGINE_VERSION,
        generated_at="2026-08-17T00:00:00Z",
    )

    assert "Z99.89" not in {candidate["code"] for candidate in _all_candidates(view)}
    assert view["withheld_without_evidence"] == 1
    assert view["withheld_without_reason"] == 0


def test_evidence_backed_rejection_without_reason_has_a_distinct_withheld_count():
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    result.provider_card.declined_codes.append(CodeSuggestion(
        kind=CodeKind.ICD10,
        code="Z99.89",
        label="Synthetic unexplained rejection",
        disposition=Disposition.REJECTED,
        evidence=[Evidence(
            "signed_note", "intermittent hematochezia", "synthetic test evidence",
        )],
    ))

    view = build_billing_tab_view(
        SAMPLE_VISIT, result, engine_version=ENGINE_VERSION,
        generated_at="2026-08-17T00:00:00Z",
    )

    assert "Z99.89" not in {candidate["code"] for candidate in _all_candidates(view)}
    assert view["withheld_without_evidence"] == 0
    assert view["withheld_without_reason"] == 1


def test_verified_but_unlocatable_evidence_is_not_a_rendering_anchor():
    """Normalized verification alone cannot produce a browser highlight."""
    from cosmaneura_billing.evidence import verify_suggestion_evidence

    result = BillingOrchestrator().process(SAMPLE_VISIT)
    suggestion = CodeSuggestion(
        kind=CodeKind.ICD10,
        code="Z99.89",
        label="Synthetic unlocatable evidence",
        disposition=Disposition.REJECTED,
        evidence=[Evidence(
            "signed_note", "INTERMITTENT HEMATOCHEZIA", "synthetic test evidence",
        )],
        rationale="Synthetic rejected candidate.",
    )
    assert verify_suggestion_evidence(SAMPLE_VISIT, [suggestion]) == []
    assert suggestion.has_evidence, "the normalized verifier should accept the quote"
    result.provider_card.declined_codes.append(suggestion)

    view = build_billing_tab_view(
        SAMPLE_VISIT, result, engine_version=ENGINE_VERSION,
        generated_at="2026-08-17T00:00:00Z",
    )

    assert "Z99.89" not in {candidate["code"] for candidate in _all_candidates(view)}


def test_em_code_without_an_exact_anchor_is_suppressed():
    source, result = _qualified_result()
    em = next(
        suggestion for suggestion in result.provider_card.captured_codes
        if suggestion.kind == CodeKind.EM
    )
    em.evidence = []

    view = build_billing_tab_view(
        source, result, engine_version=ENGINE_VERSION,
        generated_at="2026-08-17T00:00:00Z",
    )

    assert view["em"] is None
    assert "99214" not in {candidate["code"] for candidate in _all_candidates(view)}


def test_held_and_removed_always_carry_a_reason():
    for cand in _all_candidates(_view()):
        if cand["status"] != "suggested":
            assert cand["status_reason"], (
                f"{cand['code']} is {cand['status']} with no reason given")


def test_golden_demo_encounter():
    """The engine's answer on the demo visit, as specced."""
    view = _view()

    # Current-care evidence makes the candidate reviewable, but does not
    # establish two MDM elements or a time pathway in the unchanged demo note.
    em = view["em"]
    assert em["suggested_level"] == "99212"
    assert em["provider_selected_level"] == "99213"
    assert em["status"] == "held"
    assert em["copy_text"] is None
    assert any("fewer than two qualified MDM elements" in reason
               for reason in em["guardrail_actions"])
    assert em["mdm_facts"] == []
    assert "E/M level is unestablished" in em["status_reason"]
    assert "MDM = straightforward" not in em["status_reason"]
    assert "met by" not in em["status_reason"]

    # Patient explicitly denied the family history. The tempting code is
    # visibly removed, with that exact denial anchored as decision evidence.
    family_history = next(c for c in view["icd_candidates"] if c["description"].startswith("Family history"))
    assert family_history["status"] == "removed"
    assert "denied" in family_history["status_reason"].lower()
    assert family_history["evidence_spans"][0]["text"] == (
        "I do not have a family history of colon cancer"
    )

    # documented in vitals but not addressed in the plan -> held
    obesity = next(c for c in view["icd_candidates"] if c["description"] == "Obesity")
    assert obesity["status"] == "held"
    assert obesity["code"] == "(unresolved)"
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
    facts = {f["element"]: f for f in _qualified_view()["em"]["mdm_facts"]}
    assert set(facts) == {"problems", "data", "risk"}
    assert facts["problems"]["finding"] == "moderate"
    assert facts["risk"]["finding"] == "straightforward"


def test_mdm_element_evidence_is_not_borrowed_from_another_element():
    """The risk quote must not be presented as evidence for `problems`. Where an
    element has no span of its own, the span is null rather than wrong."""
    facts = {f["element"]: f for f in _qualified_view()["em"]["mdm_facts"]}
    risk_span = facts["risk"]["evidence_span"]
    problems_span = facts["problems"]["evidence_span"]
    assert risk_span is None
    assert problems_span is not None
    # the data element has no evidence span in the engine today; say so honestly
    assert facts["data"]["evidence_span"] is None


def test_filtered_evidence_does_not_shift_mdm_element_attribution():
    """Dropping one unlocatable quote must not re-label the next valid span."""
    from cosmaneura_billing.billing_tab import _element_span

    suggestion = CodeSuggestion(
        kind=CodeKind.EM,
        code="99214",
        label="Established patient office visit",
        disposition=Disposition.CAPTURED,
        evidence=[
            Evidence(
                "signed_note", "INTERMITTENT HEMATOCHEZIA",
                "MDM risk: mdm-risk-1",
            ),
            Evidence(
                "signed_note", "intermittent hematochezia",
                "MDM problems: mdm-problem-1",
            ),
        ],
    )

    assert _element_span(suggestion, SAMPLE_VISIT, "risk") is None
    problem = _element_span(suggestion, SAMPLE_VISIT, "problems")
    assert problem is not None
    assert problem["text"] == "intermittent hematochezia"
    loc = problem["loc"]
    assert SAMPLE_VISIT.signed_note[loc["start"]:loc["end"]] == problem["text"]


def test_guardrail_actions_are_surfaced_for_the_em_code():
    actions = _qualified_view()["em"]["guardrail_actions"]
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
    assert "99212" not in copy_all, "a held E/M must never reach the clipboard"


def test_only_suggested_candidates_have_copy_text():
    for cand in _all_candidates(_view()):
        assert cand["copy_text"] == (cand["code"] if cand["status"] == "suggested" else None)


def test_em_summary_copy_controls_follow_captured_held_and_removed_dispositions():
    """Exercise display state changes on a genuinely qualified pipeline E/M."""
    for disposition, status in ((Disposition.CAPTURED, "suggested"),
                                (Disposition.REVIEW, "held"),
                                (Disposition.REJECTED, "removed")):
        source, result = _qualified_result()
        candidate = next(c for c in result.provider_card.captured_codes if c.kind == CodeKind.EM)
        candidate.disposition = disposition
        if disposition != Disposition.CAPTURED:
            candidate.rationale = "Synthetic disposition check for display controls."
        view = build_billing_tab_view(source, result, engine_version=ENGINE_VERSION)
        expected_copy = candidate.code if disposition == Disposition.CAPTURED else None
        assert view["em"]["status"] == status
        assert view["em"]["copy_text"] == expected_copy
        assert (candidate.code in view["copy_all"].split()) == (expected_copy is not None)
        row = next(c for c in view["cpt_candidates"] if c["code"] == candidate.code)
        assert row["status"] == status
        assert row["copy_text"] == expected_copy
        assert row["evidence_spans"]
        for span in row["evidence_spans"]:
            document = getattr(source, span["source"])
            assert document[span["loc"]["start"]:span["loc"]["end"]] == span["text"]


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
    assert body["em"]["status"] == "held"
    assert body["em"]["copy_text"] is None
    assert body["em"]["mdm_facts"] == []
    assert body["inferenceMode"] == "mock"
    assert body["encounterVersion"] == 1
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
