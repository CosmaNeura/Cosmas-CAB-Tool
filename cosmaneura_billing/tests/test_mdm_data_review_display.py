"""Synthetic presentation fixtures, independent of engine adjudication goldens."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as header
from cosmaneura_billing.billing_header_runtime import generated_header
from cosmaneura_billing.billing_header_store import MemoryBillingHeaderStore
from cosmaneura_billing.billing_tab import _data_review, _spans, build_billing_tab_view, valid_data_review
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.schemas import (BillingResult, BillerWorksheet, CodeKind,
    CodeSuggestion, Disposition, Evidence, ProviderCard, VisitBundle)

GOLDEN = json.loads((Path(__file__).parent / "fixtures/mdm_data_review.json").read_text())


def example():
    bundle = VisitBundle("SYN-DATA-REVIEW", 30, "female", "office_established", "11",
                         signed_note=GOLDEN["encounter"]["signedNote"])
    suggestion = CodeSuggestion(CodeKind.EM, "99212", "Office E/M", Disposition.CAPTURED,
                                rationale="Synthetic adapter fixture; not an engine decision.")
    suggestion.data_review = deepcopy(GOLDEN["raw"])
    suggestion.evidence = [Evidence(e["source"], e["quote"], f"MDM data: {event['id']}")
                           for event in suggestion.data_review["events"] if event["status"] == "counted"
                           for e in event["evidence"]]
    result = BillingResult(ProviderCard(bundle.encounter_id, "Synthetic display", [suggestion]),
                           BillerWorksheet(bundle.encounter_id, em_code=suggestion))
    return bundle, suggestion, result


def test_golden_ledger_exact_unicode_spans_and_original_source_unchanged():
    bundle, suggestion, result = example()
    before = deepcopy((bundle, suggestion.data_review, suggestion.evidence))
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-display")
    assert view["em"]["data_review"] == GOLDEN["display"]
    assert valid_data_review(view["em"]["data_review"], bundle)
    assert (bundle, suggestion.data_review, suggestion.evidence) == before


@pytest.mark.parametrize("revision", ["2026-09-30.v1", "2026-09-30.v2", "2026-10-02.v3"])
def test_data_rule_revisions_retain_original_decisions_in_valid_headers(revision):
    bundle, suggestion, result = example()
    suggestion.data_review["rule_version"] = f"office-mdm-data.{revision}"
    stored = MemoryEncounterStore([bundle], practice_id="synthetic-data").get(bundle.encounter_id)
    body = generated_header(stored, result, engine_version="synthetic-rule-revision")
    assert body["dataReview"]["rule_version"] == suggestion.data_review["rule_version"]
    assert header.validate_generated(body, stored) == body


def test_held_and_duplicate_decision_quotes_do_not_become_code_support():
    bundle, suggestion, result = example()
    before = deepcopy(suggestion.evidence)
    ledger = _data_review(suggestion, bundle)
    assert ledger["events"][1]["evidence_complete"] and ledger["events"][2]["evidence_complete"]
    assert suggestion.evidence == before
    assert _spans(suggestion, bundle) == GOLDEN["display"]["events"][0]["evidence_spans"]
    assert "Home log reviewed." not in [e.quote for e in suggestion.evidence]
    suggestion.evidence = []
    ledger = _data_review(suggestion, bundle)
    assert ledger["events"][0]["status"] == "held"  # cannot borrow a duplicate's quote
    assert ledger["events"][1]["evidence_complete"] and ledger["events"][2]["evidence_complete"]
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-display")
    assert view["em"] is None and view["cpt_candidates"] == []


def test_golden_held_only_ledger_stays_visible_without_any_code_or_code_evidence():
    bundle, suggestion, result = example()
    suggestion.data_review["events"] = suggestion.data_review["events"][2:]
    suggestion.data_review["category_one_units"] = 0
    suggestion.evidence = []
    expected = {**deepcopy(GOLDEN["display"]), "category_one_units": 0,
                "events": [deepcopy(GOLDEN["display"]["events"][2])]}
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-display")
    assert view["data_review"] == expected
    assert view["em"] is None and view["cpt_candidates"] == [] and view["copy_all"] == ""
    stored = MemoryEncounterStore([bundle], practice_id="data-display").get(bundle.encounter_id)
    body = generated_header(stored, result, engine_version="synthetic-display")
    assert body["dataReview"] == expected
    assert body["suggestions"] == [] and body["evidence"] == []
    assert header.validate_generated(body, stored) == body


def test_maximum_data_ledger_plus_existing_evidence_fits_header_without_dropping_anchors():
    bundle, suggestion, result = example()
    quotes = [f"Synthetic source assertion {i}." for i in range(200)]
    bundle.signed_note = "\n".join(quotes)
    raw = suggestion.data_review
    raw.update(level="moderate", category_one_units=40, qualifying_categories=["category_one"], events=[])
    suggestion.evidence = []
    for index in range(40):
        item = {"id": f"data-{index}", "action": "test_order", "subject": f"Synthetic test {index}",
                "status": "counted", "category": "category_one", "reason": "Synthetic display fixture.",
                "evidence": [{"source": "signed_note", "quote": q} for q in quotes[index * 3:index * 3 + 3]]}
        raw["events"].append(item)
        suggestion.evidence.extend(Evidence("signed_note", e["quote"], f"MDM data: {item['id']}") for e in item["evidence"])
    suggestion.evidence.extend(Evidence("signed_note", q, "Other synthetic code support") for q in quotes[120:])
    encounters = MemoryEncounterStore([bundle], practice_id="data-display")
    stored = encounters.get(bundle.encounter_id)
    body = generated_header(stored, result, engine_version="synthetic-display")
    assert len(body["dataReview"]["events"]) == 40
    assert body["dataReview"]["evidence_complete"]
    assert len(body["evidence"]) == len(body["suggestions"][0]["evidenceIds"]) == 200
    assert {e["text"] for e in body["evidence"]} == set(quotes)
    actor = header.HeaderActor(str(uuid4()), str(uuid4()), "Synthetic provider", "Provider")
    store = MemoryBillingHeaderStore(encounters, practice_id="data-display", authorize=lambda _actor: True)
    view, _ = store.create(bundle.encounter_id, generated=body, expected_source=header.source_snapshot(stored),
                           actor=actor, operation_id=str(uuid4()))
    draft = header.create_draft(view["header"])
    draft["note"] = "Reviewed maximum synthetic envelope."
    saved, _ = store.save(bundle.encounter_id, header_id=view["header"]["headerId"], expected_revision=0,
        expected_source=header.source_snapshot(stored), draft=draft, reason="Synthetic boundary review",
        actor=actor, operation_id=str(uuid4()))
    assert saved["header"]["evidence"] == body["evidence"]
    body["evidence"].extend({**body["evidence"][0], "id": f"overflow-{i}"} for i in range(121))
    with pytest.raises(header.BillingHeaderError): header.validate_generated(body, stored)


@pytest.mark.parametrize("invalid", ["repeated", "fuzzy", "fabricated", "wrong-source", "oversize-source"])
def test_explanation_only_quotes_still_require_one_exact_bounded_source_location(invalid):
    bundle, suggestion, _ = example()
    if invalid == "repeated": bundle.signed_note += " Home log reviewed."
    elif invalid == "oversize-source": bundle.signed_note += " " * 500_001
    else:
        ev = suggestion.data_review["events"][2]["evidence"][0]
        if invalid == "wrong-source": ev["source"] = "transcript"
        else: ev["quote"] = "HOME LOG REVIEWED." if invalid == "fuzzy" else "Missing text"
    ledger = _data_review(suggestion, bundle)
    assert ledger["events"][2]["status"] == "held"
    assert ledger["events"][2]["evidence_spans"] == []
    assert not ledger["events"][2]["evidence_complete"]


@pytest.mark.parametrize("tamper", ["removed", "fuzzy", "fabricated", "wrong-source", "risk-tag", "partial"])
def test_counted_events_require_all_exact_retained_event_evidence(tamper):
    bundle, suggestion, _ = example()
    if tamper == "removed": suggestion.evidence.pop(0)
    elif tamper == "risk-tag": suggestion.evidence[0].rationale = "supports risk"
    elif tamper == "partial":
        suggestion.data_review["events"][0]["evidence"].append({"source": "signed_note", "quote": "missing"})
    else:
        ev = suggestion.evidence[0]
        if tamper == "wrong-source": ev.source = "transcript"
        else: ev.quote = "CBC ORDERED." if tamper == "fuzzy" else "imaginary"
    ledger = _data_review(suggestion, bundle)
    assert ledger["events"][0]["status"] == "held"
    assert not ledger["events"][0]["evidence_complete"] and not ledger["evidence_complete"]
    assert ledger["level"] == GOLDEN["raw"]["level"]  # retained, never recalculated
    assert valid_data_review(ledger, bundle)


def test_intake_cannot_be_presented_as_counted_and_summary_cannot_invent_units():
    bundle, suggestion, _ = example()
    bundle.intake_text = bundle.signed_note
    suggestion.data_review["events"][0]["evidence"][0]["source"] = "intake_text"
    suggestion.evidence[0].source = "intake_text"
    ledger = _data_review(suggestion, bundle)
    assert ledger["events"][0]["status"] == "held" and not ledger["evidence_complete"]
    _, suggestion, _ = example()
    suggestion.data_review["category_one_units"] = 2
    assert not _data_review(suggestion, bundle)["evidence_complete"]


def test_boolean_categories_are_separate_from_historian_unit():
    bundle, suggestion, _ = example()
    raw = suggestion.data_review
    for category, flag in [("historian", "independent_historian"),
                           ("interpretation", "independent_interpretation"),
                           ("discussion", "external_discussion")]:
        raw["events"].append({**deepcopy(raw["events"][0]), "id": category, "category": category})
        suggestion.evidence.append(Evidence("signed_note", "CBC ordered.", f"MDM data: {category}"))
        raw[flag] = True
    raw["category_one_units"] = 2  # one unique test plus one historian, not four
    assert _data_review(suggestion, bundle)["evidence_complete"]
    raw["category_one_units"] = 4
    assert not _data_review(suggestion, bundle)["evidence_complete"]


def test_considered_test_remains_a_distinct_reported_action_and_projection_is_detached():
    bundle, suggestion, _ = example()
    suggestion.data_review["events"][0]["action"] = "test_considered"
    ledger = _data_review(suggestion, bundle)
    assert ledger["events"][0]["action"] == "test_considered"
    ledger["qualifying_categories"].append("discussion")
    assert suggestion.data_review["qualifying_categories"] == []


@pytest.mark.parametrize("value", [None, [], {}, {"events": []}])
def test_legacy_or_invalid_ledger_does_not_create_a_display(value):
    bundle, suggestion, result = example()
    suggestion.data_review = value
    assert "data_review" not in build_billing_tab_view(bundle, result, engine_version="test")["em"]


def test_header_ledger_is_immutable_across_edit_and_reload_with_no_source_change():
    bundle, _, result = example()
    encounters = MemoryEncounterStore([bundle], practice_id="data-display")
    stored = encounters.get(bundle.encounter_id)
    actor = header.HeaderActor(str(uuid4()), str(uuid4()), "Synthetic provider", "Provider")
    store = MemoryBillingHeaderStore(encounters, practice_id="data-display", authorize=lambda _actor: True)
    body = generated_header(stored, result, engine_version="synthetic-display")
    assert body["dataReview"] == GOLDEN["display"]
    view, _ = store.create(bundle.encounter_id, generated=body, expected_source=header.source_snapshot(stored),
                           actor=actor, operation_id=str(uuid4()))
    draft = header.create_draft(view["header"])
    draft["note"] = "Human review does not replace engine data findings."
    saved, _ = store.save(bundle.encounter_id, header_id=view["header"]["headerId"],
        expected_source=header.source_snapshot(stored), expected_revision=0, draft=draft,
        reason="Synthetic review", actor=actor, operation_id=str(uuid4()))
    assert saved["header"] == view["header"]
    assert store.latest(bundle.encounter_id, expected_source=header.source_snapshot(stored))["header"]["dataReview"] == GOLDEN["display"]
    assert encounters.get(bundle.encounter_id).bundle == bundle
    for change in ("quote", "offset", "count", "counted-without-evidence"):
        bad = deepcopy(body)
        event = bad["dataReview"]["events"][0]
        if change == "quote": event["evidence_spans"][0]["text"] = "fabricated"
        elif change == "offset": event["evidence_spans"][0]["loc"]["start"] += 1
        elif change == "count": bad["dataReview"]["category_one_units"] += 1
        else: event["evidence_spans"] = []
        with pytest.raises(header.BillingHeaderError): header.validate_generated(bad, stored)


def test_saved_data_ledger_revalidates_exact_source_on_reload():
    bundle, _, result = example()
    encounters = MemoryEncounterStore([bundle], practice_id="data-reload")
    stored = encounters.get(bundle.encounter_id)
    actor = header.HeaderActor(str(uuid4()), str(uuid4()), "Synthetic provider", "Provider")
    store = MemoryBillingHeaderStore(encounters, practice_id="data-reload", authorize=lambda _actor: True)
    body = generated_header(stored, result, engine_version="synthetic-display")
    view, _ = store.create(bundle.encounter_id, generated=body, expected_source=header.source_snapshot(stored),
                           actor=actor, operation_id=str(uuid4()))
    stored_ledger = store._data["headers"][view["header"]["headerId"]]["header"]["dataReview"]
    stored_ledger["events"][0]["evidence_spans"][0]["text"] = "invented"
    with pytest.raises(header.BillingHeaderError):
        store.latest(bundle.encounter_id, expected_source=header.source_snapshot(stored))
