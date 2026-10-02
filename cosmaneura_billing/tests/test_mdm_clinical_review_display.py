"""Source-bound presentation contracts, independent of clinical-rule fixtures."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as header
from cosmaneura_billing.billing_header_runtime import generated_header
from cosmaneura_billing.billing_header_store import MemoryBillingHeaderStore
from cosmaneura_billing.billing_tab import _clinical_review, _spans, build_billing_tab_view, valid_clinical_review
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.schemas import (BillingResult, BillerWorksheet, CodeKind,
    CodeSuggestion, Disposition, Evidence, ProviderCard, VisitBundle)

GOLDEN = json.loads((Path(__file__).parent / "fixtures/mdm_clinical_review.json").read_text())


def example():
    bundle = VisitBundle("SYN-CLINICAL-REVIEW", 30, "F", "office_established", "11",
                         signed_note=GOLDEN["encounter"]["signedNote"])
    suggestion = CodeSuggestion(CodeKind.EM, "99213", "Office E/M", Disposition.CAPTURED,
                                rationale="Synthetic presentation fixture; not an adjudicated code.")
    suggestion.clinical_review = deepcopy(GOLDEN["raw"])
    suggestion.evidence = [Evidence(e["source"], e["quote"], f"MDM {name}: {event['id']}")
        for name in ("problems", "risk") for event in suggestion.clinical_review[name]["events"]
        if event["status"] == "counted" for e in event["evidence"]]
    result = BillingResult(ProviderCard(bundle.encounter_id, "Synthetic presentation", [suggestion]),
                           BillerWorksheet(bundle.encounter_id, em_code=suggestion))
    return bundle, suggestion, result


def stored_fixture(bundle, result):
    encounters = MemoryEncounterStore([bundle], practice_id="clinical-display")
    stored = encounters.get(bundle.encounter_id)
    actor = header.HeaderActor(str(uuid4()), str(uuid4()), "Synthetic provider", "Provider")
    store = MemoryBillingHeaderStore(encounters, practice_id="clinical-display", authorize=lambda _actor: True)
    body = generated_header(stored, result, engine_version="synthetic-display")
    view, _ = store.create(bundle.encounter_id, generated=body, expected_source=header.source_snapshot(stored),
                           actor=actor, operation_id=str(uuid4()))
    return store, stored, actor, view


def test_golden_clinical_decisions_are_detached_and_exact_in_unicode_coordinates():
    bundle, suggestion, result = example()
    before = deepcopy((bundle, suggestion))
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-display")
    assert view["em"]["clinical_review"] == GOLDEN["display"]
    assert valid_clinical_review(view["em"]["clinical_review"], bundle)
    assert view["em"]["clinical_review"]["problems"]["events"][0]["evidence_spans"][0]["loc"]["start"] == 2
    assert (bundle, suggestion) == before
    view["em"]["clinical_review"]["risk"]["events"][0]["subject"] = "changed"
    assert suggestion.clinical_review == GOLDEN["raw"]


@pytest.mark.parametrize("problem_revision,risk_revision", [("v1", "v1"), ("v2", "v2"), ("v3", "v3"), ("v4", "v3"), ("v4", "v4")])
def test_semantic_rule_revisions_keep_original_header_decisions_readable(problem_revision, risk_revision):
    bundle, suggestion, result = example()
    revisions = {"problems": problem_revision, "risk": risk_revision}
    for name, revision in revisions.items():
        suggestion.clinical_review[name]["rule_version"] = f"office-mdm-{name}.2026-09-30.{revision}"
    store, stored, _, original = stored_fixture(bundle, result)
    assert all(original["header"]["clinicalReview"][name]["rule_version"].endswith(revisions[name])
               for name in ("problems", "risk"))
    assert store.latest(bundle.encounter_id, expected_source=header.source_snapshot(stored)) == original


@pytest.mark.parametrize("tamper", ["removed", "wrong-element", "wrong-id", "fuzzy", "partial", "intake"])
def test_counted_anchors_require_exact_retained_element_and_event_attribution(tamper):
    bundle, suggestion, _ = example()
    if tamper == "removed": suggestion.evidence.pop(0)
    elif tamper == "wrong-element": suggestion.evidence[0].rationale = "MDM risk: mdm-problem-1"
    elif tamper == "wrong-id": suggestion.evidence[0].rationale = "MDM problems: mdm-problem-2"
    elif tamper == "fuzzy": suggestion.evidence[0].quote = suggestion.evidence[0].quote.upper()
    elif tamper == "partial": suggestion.clinical_review["problems"]["events"][0]["evidence"].append(
        {"source": "signed_note", "quote": "Not documented."})
    else:
        bundle.intake_text = bundle.signed_note
        suggestion.evidence[0].source = "intake_text"
        suggestion.clinical_review["problems"]["events"][0]["evidence"][0]["source"] = "intake_text"
    ledger = _clinical_review(suggestion, bundle)
    assert ledger["problems"]["events"][0]["status"] == "held"
    assert not ledger["problems"]["events"][0]["evidence_complete"]
    assert not ledger["problems"]["evidence_complete"]
    assert ledger["problems"]["level"] == "low"  # Original, not a replacement adjudication.
    assert ledger["risk"]["evidence_complete"]
    assert valid_clinical_review(ledger, bundle)


def test_held_duplicate_explanations_survive_without_becoming_code_support():
    bundle, suggestion, result = example()
    assert len(_spans(suggestion, bundle)) == 2
    assert all("Historical rash" not in e.quote for e in suggestion.evidence)
    suggestion.evidence = []
    for name in ("problems", "risk"):
        section = suggestion.clinical_review[name]
        section["level"] = "straightforward"
        for event in section["events"]:
            if event["status"] == "counted": event.update(status="held", category=None)
    view = build_billing_tab_view(bundle, result, engine_version="synthetic-display")
    assert view["em"] is None and view["cpt_candidates"] == [] and view["copy_all"] == ""
    assert view["clinical_review"]["problems"]["events"][1]["status"] == "duplicate"
    store, stored, _, original = stored_fixture(bundle, result)
    assert original["header"]["suggestions"] == [] and original["header"]["evidence"] == []
    assert original["header"]["clinicalReview"] == view["clinical_review"]
    assert store.latest(bundle.encounter_id, expected_source=header.source_snapshot(stored)) == original


def test_edits_cannot_replace_original_ledger_and_reload_revalidates_it():
    bundle, _, result = example()
    store, stored, actor, original = stored_fixture(bundle, result)
    draft = header.create_draft(original["header"])
    draft["note"] = "Reviewed the original findings."
    arguments = dict(header_id=original["header"]["headerId"], expected_revision=0,
        expected_source=header.source_snapshot(stored), reason="Reviewed source", actor=actor)
    with pytest.raises(header.BillingHeaderError):
        store.save(bundle.encounter_id, **arguments, draft={**draft, "clinicalReview": {}}, operation_id=str(uuid4()))
    saved, _ = store.save(bundle.encounter_id, **arguments, draft=draft, operation_id=str(uuid4()))
    assert saved["header"]["clinicalReview"] == GOLDEN["display"]
    assert "clinicalReview" not in saved["revision"]["draft"]
    assert store.latest(bundle.encounter_id, expected_source=header.source_snapshot(stored)) == saved
    stored_ledger = store._data["headers"][original["header"]["headerId"]]["header"]["clinicalReview"]
    stored_ledger["risk"]["events"][0]["evidence_spans"][0]["text"] = "invented"
    with pytest.raises(header.BillingHeaderError):
        store.latest(bundle.encounter_id, expected_source=header.source_snapshot(stored))


@pytest.mark.parametrize("tamper", ["quote", "offset", "bool-offset", "category", "version", "source",
                                    "duplicate-id", "missing", "too-many-events", "too-many-spans", "extra-field"])
def test_persisted_shape_and_source_tampering_is_rejected(tamper):
    bundle, _, result = example()
    stored = MemoryEncounterStore([bundle], practice_id="clinical-display").get(bundle.encounter_id)
    body = generated_header(stored, result, engine_version="synthetic-display")
    ledger = body["clinicalReview"]
    event = ledger["problems"]["events"][0]
    span = event["evidence_spans"][0]
    if tamper == "quote": span["text"] = "fabricated"
    elif tamper == "offset": span["loc"]["start"] += 1
    elif tamper == "bool-offset": span["loc"]["start"] = True
    elif tamper == "category": event["category"] = "prescription_management"
    elif tamper == "version": ledger["risk"]["rule_version"] = "future"
    elif tamper == "source": span["source"] = "transcript"
    elif tamper == "duplicate-id": ledger["problems"]["events"][1]["id"] = event["id"]
    elif tamper == "missing": event["evidence_spans"] = []
    elif tamper == "too-many-events": ledger["risk"]["events"] *= 21
    elif tamper == "too-many-spans": event["evidence_spans"] *= 4
    else: ledger["selected_level"] = "high"
    assert not valid_clinical_review(ledger, bundle)
    with pytest.raises(header.BillingHeaderError): header.validate_generated(body, stored)


@pytest.mark.parametrize("value", [None, [], {}, {"rule_version": "future"}])
def test_absent_or_invalid_raw_ledger_is_not_presented(value):
    bundle, suggestion, result = example()
    suggestion.clinical_review = value
    assert "clinical_review" not in build_billing_tab_view(bundle, result, engine_version="fixture")["em"]


def test_maximum_combined_envelope_preserves_all_320_references_through_save():
    bundle, suggestion, result = example()
    quotes = [f"Synthetic source assertion {i}." for i in range(320)]
    bundle.signed_note = "\n".join(quotes)
    suggestion.evidence = []
    data = json.loads((Path(__file__).parent / "fixtures/mdm_data_review.json").read_text())["raw"]
    data.update(level="moderate", category_one_units=40, qualifying_categories=["category_one"], events=[])
    suggestion.data_review = data
    offset = 0
    for name, section, size, category in [("data", data, 40, "category_one"),
        ("problems", suggestion.clinical_review["problems"], 20, "stable_chronic"),
        ("risk", suggestion.clinical_review["risk"], 20, "prescription_management")]:
        section.update(level="moderate", events=[])
        for index in range(size):
            event = {"id": f"{name}-{index}", "subject": f"Synthetic finding {index}", "status": "counted",
                "category": category, "reason": "Synthetic display envelope.",
                "evidence": [{"source": "signed_note", "quote": q} for q in quotes[offset:offset + 3]]}
            if name == "data": event["action"] = "test_order"
            section["events"].append(event)
            suggestion.evidence.extend(Evidence(e["source"], e["quote"], f"MDM {name}: {event['id']}") for e in event["evidence"])
            offset += 3
    suggestion.evidence.extend(Evidence("signed_note", q, "Other code support") for q in quotes[offset:])
    store, stored, actor, view = stored_fixture(bundle, result)
    body = view["header"]
    assert len(body["evidence"]) == len(body["suggestions"][0]["evidenceIds"]) == 320
    assert {e["text"] for e in body["evidence"]} == set(quotes)
    assert body["dataReview"]["evidence_complete"]
    assert all(body["clinicalReview"][name]["evidence_complete"] for name in ("problems", "risk"))
    draft = header.create_draft(body)
    draft["note"] = "All original source references retained."
    saved, _ = store.save(bundle.encounter_id, header_id=body["headerId"], expected_revision=0,
        expected_source=header.source_snapshot(stored), draft=draft, reason="Envelope check", actor=actor, operation_id=str(uuid4()))
    assert saved["header"] == body
    generated = {k: body[k] for k in ("facts", "evidence", "suggestions", "flags", "policy", "generation", "dataReview", "clinicalReview")}
    generated["evidence"] = [*generated["evidence"], {**generated["evidence"][0], "id": "overflow"}]
    with pytest.raises(header.BillingHeaderError): header.validate_generated(generated, stored)


def test_time_explanation_and_editable_header_preserve_care_without_claiming_mdm():
    from dataclasses import asdict
    from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
    from test_mdm_clinical_pipeline import current_care_case, pipeline, em
    kb = ICD10CCSR()
    from cosmaneura_billing.billing_header import validate_generated
    from cosmaneura_billing.billing_header_runtime import generated_header
    from cosmaneura_billing.billing_tab import build_billing_tab_view
    from cosmaneura_billing.persistence import MemoryEncounterStore
    raw, source = current_care_case()
    result = pipeline(raw, source, kb)
    suggestion = em(result)
    assert suggestion.disposition == Disposition.CAPTURED
    assert suggestion.label == "Office visit supported by documented total time (99213)"
    assert "MDM classification remains unestablished" in suggestion.rationale
    assert any("supported by documented total time" in line for line in result.biller_worksheet.audit_trail)
    assert "MDM ceiling" not in json.dumps(asdict(result))
    view = build_billing_tab_view(source, result, engine_version="synthetic-current-care")
    assert all(item["evidence_span"] is None for item in view["em"]["mdm_facts"])
    assert view["em"]["evidence_spans"]  # Care proof still accompanies the time-based suggestion.
    stored = MemoryEncounterStore([source], practice_id="synthetic-care").get(source.encounter_id)
    body = generated_header(stored, result, engine_version="synthetic-current-care")
    assert validate_generated(body, stored) == body
    assert body["clinicalReview"]["problems"]["events"][0]["status"] == "held"
    assert "Current clinical work" in body["clinicalReview"]["problems"]["events"][0]["reason"]
    assert {e.quote for e in suggestion.evidence} <= {e["text"] for e in body["evidence"]}
