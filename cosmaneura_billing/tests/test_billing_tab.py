"""F1 golden test on the demo encounter (synthesis §F1 acceptance):

26-y/o rectal bleeding — 99214 surfaced with evidence over the selected 99213;
the patient-denied family-history code REMOVED with the contradiction shown;
documented-but-unaddressed obesity HELD with a reason; every rendered
candidate carries ≥1 evidence span; dedupe holds; the clinical note object is
never mutated."""
from __future__ import annotations

import copy
import json

import pytest

from cosmaneura_billing.billing_tab import build_billing_tab_view
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence

GENERATED_AT = "2026-08-12T21:00:00+00:00"


@pytest.fixture(scope="module")
def view_and_bundle():
    bundle = copy.deepcopy(SAMPLE_VISIT)
    note_before = bundle.signed_note
    result = BillingOrchestrator().process(bundle)
    view = build_billing_tab_view(bundle, result, generated_at=GENERATED_AT)
    return view, bundle, note_before


def test_em_undercoding_recovery_with_evidence(view_and_bundle):
    view, _, _ = view_and_bundle
    em = view["em"]
    assert em["suggested_level"] == "99214"
    assert em["provider_selected_level"] == "99213"
    assert em["status"] == "suggested"
    assert em["pathway"] == "mdm_2of3"           # reserved seam for total_time
    elements = {f["element"] for f in em["mdm_facts"]}
    assert {"problems", "data", "risk"} <= elements
    assert any(f["evidence_span"] for f in em["mdm_facts"]), \
        "MDM facts must carry documentation evidence"


def test_every_rendered_candidate_has_evidence_span(view_and_bundle):
    view, _, _ = view_and_bundle
    for c in view["icd_candidates"] + view["cpt_candidates"]:
        assert len(c["evidence_spans"]) >= 1, f"{c['code']} rendered without a span"
        for span in c["evidence_spans"]:
            assert span["text"].strip()


def test_held_and_removed_always_carry_reasons(view_and_bundle):
    view, _, _ = view_and_bundle
    for c in view["icd_candidates"] + view["cpt_candidates"]:
        if c["status"] in ("held", "removed"):
            assert c["status_reason"], f"{c['code']} lacks a status_reason"
        else:
            assert c["status_reason"] is None


def test_denied_family_history_removed_with_contradiction_shown(view_and_bundle):
    view, _, _ = view_and_bundle
    z80 = [c for c in view["icd_candidates"] if c["code"].startswith("Z80")]
    assert z80, "family-history candidate should render as removed"
    assert z80[0]["status"] == "removed"
    assert "denied" in z80[0]["status_reason"].lower()
    assert any("family history" in s["text"].lower()
               for s in z80[0]["evidence_spans"]), \
        "the removal must show the patient's verbatim denial"


def test_unaddressed_obesity_held_with_reason(view_and_bundle):
    view, _, _ = view_and_bundle
    e66 = [c for c in view["icd_candidates"] if c["code"].startswith("E66")]
    assert e66 and e66[0]["status"] == "held"
    assert "not addressed" in e66[0]["status_reason"].lower()


def test_primary_covered_dx_leads_and_suggested_sort_first(view_and_bundle):
    view, _, _ = view_and_bundle
    icd = view["icd_candidates"]
    assert icd[0]["code"] == "K62.5"             # primary documented dx leads
    statuses = [c["status"] for c in icd]
    assert statuses == sorted(statuses, key=["suggested", "held",
                                             "removed"].index)


def test_no_duplicate_rows_across_codes_and_alternates(view_and_bundle):
    view, _, _ = view_and_bundle
    codes = [c["code"] for c in view["icd_candidates"]]
    assert len(codes) == len(set(codes))
    alternates = [a for c in view["icd_candidates"] for a in c["alternates"]]
    assert not (set(alternates) & set(codes)), \
        "an alternate must not also render as its own row"


def test_plan_items_link_to_justifying_code(view_and_bundle):
    view, _, _ = view_and_bundle
    k62 = next(c for c in view["icd_candidates"] if c["code"] == "K62.5")
    joined = " ".join(k62["linked_plan_items"]).lower()
    assert "colonoscopy" in joined, \
        "the colonoscopy referral should group under the justifying dx"


def test_copy_affordances(view_and_bundle):
    view, _, _ = view_and_bundle
    for c in view["icd_candidates"]:
        assert c["copy_text"] == c["code"]
    assert "K62.5" in view["copy_all_text"]
    assert "99214" in view["copy_all_text"]
    removed = [c["code"] for c in view["icd_candidates"]
               if c["status"] == "removed"]
    for code in removed:
        assert code not in view["copy_all_text"], \
            "copy-all must never include removed codes"


def test_clinical_note_never_mutated(view_and_bundle):
    _, bundle, note_before = view_and_bundle
    assert bundle.signed_note == note_before
    assert bundle.signed_note == SAMPLE_VISIT.signed_note


def test_view_is_json_serializable_with_contract_keys(view_and_bundle):
    view, _, _ = view_and_bundle
    parsed = json.loads(json.dumps(view))
    assert set(parsed) == {"visit_id", "generated_at", "engine_version", "em",
                           "icd_candidates", "cpt_candidates", "copy_all_text",
                           "completeness"}
    assert parsed["visit_id"] == "ENC-100237"
    assert parsed["generated_at"] == GENERATED_AT
    assert parsed["completeness"]["rulepack"] == "telehealth.v1"


def test_dedupe_folds_multi_code_finding_into_alternates():
    """Sherin's case: one finding, two legitimate codes → one row + alternate."""
    bundle = copy.deepcopy(SAMPLE_VISIT)
    ev = [Evidence("intake_text", "When I eat spicy food, I poop blood",
                   "documented symptom")]
    twins = [
        CodeSuggestion(kind=CodeKind.ICD10, code="K62.5",
                       label="Hemorrhage of anus and rectum",
                       disposition=Disposition.CAPTURED, evidence=list(ev)),
        CodeSuggestion(kind=CodeKind.ICD10, code="K92.1", label="Melena",
                       disposition=Disposition.CAPTURED, evidence=list(ev)),
    ]
    from cosmaneura_billing.billing_tab import _candidate, _dedupe
    rows = [( _candidate(bundle, s), s) for s in twins]
    deduped = _dedupe(rows)
    assert len(deduped) == 1
    row = deduped[0][0]
    assert row["code"] == "K62.5" and row["alternates"] == ["K92.1"]


def test_api_endpoint_serves_the_view():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import cosmaneura_billing.api as api
    client = TestClient(api.app)
    r = client.get("/api/encounters/ENC-100237/billing-tab")
    assert r.status_code == 200
    body = r.json()
    assert body["em"]["suggested_level"] == "99214"
    assert body["icd_candidates"][0]["code"] == "K62.5"
