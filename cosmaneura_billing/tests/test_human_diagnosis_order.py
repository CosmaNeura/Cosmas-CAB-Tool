"""Explicit human sequencing never changes original inference or promotes a hold."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.billing_header_signoff import (
    MemoryHeaderFeedbackAdapter, accepted_diagnosis_order, validate_signoff,
)
from cosmaneura_billing.coder_feedback import CodeDecision
from test_billing_header import fixture, create, generated, save
from test_billing_header_signoff import context_for, options, review_record


def baseline(fixture):
    body = generated(fixture[2])
    body["suggestions"].append({**body["suggestions"][0], "id": "code-2",
                                "code": "R53.83", "label": "Fatigue"})
    return create(fixture, generated=body)[0]


def confirm(fixture, view):
    draft = h.create_draft(view["header"], view["revision"])
    draft["diagnosisOrderConfirmed"] = True
    return save(fixture, view, draft=draft)[0]


def choice(decision="accept"):
    return {"id": h.DIAGNOSIS_ORDER_DECISION, "decision": decision,
            "reason": "Primary and order reviewed against source evidence"}


def test_order_only_and_unchanged_order_confirmation_are_audited(fixture):
    initial = baseline(fixture)
    draft = h.create_draft(initial["header"])
    assert draft["diagnosisOrderConfirmed"] is False and h.diagnosis_order(draft) == []
    draft["codes"].reverse()
    reordered, _ = save(fixture, initial, draft=draft)
    assert [c["path"] for c in reordered["revision"]["changes"]] == ["diagnosisOrder"]
    assert h.diagnosis_order(reordered["revision"]["draft"]) == []
    confirmed = confirm(fixture, reordered)
    change = confirmed["revision"]["changes"][0]
    assert change["path"] == "diagnosisOrder" and not change["before"]["confirmed"]
    assert change["after"]["confirmed"] and change["after"]["codes"] == ["R53.83", "R51.9"]
    assert confirmed["revision"]["actor"] == fixture[3].to_api()
    assert confirmed["revision"]["number"] == 2 and confirmed["revision"]["reviewStatus"] == "unreviewed"
    assert confirmed["header"] == initial["header"]
    assert fixture[1].get(fixture[2].bundle.encounter_id).bundle == fixture[2].bundle


@pytest.mark.parametrize("flag", [None, 0, 1, "true", [], {}, ""])
def test_confirmation_is_a_strict_optional_boolean(fixture, flag):
    view = baseline(fixture)
    draft = h.create_draft(view["header"]); draft["diagnosisOrderConfirmed"] = flag
    with pytest.raises(h.BillingHeaderError, match="invalid_edit"):
        save(fixture, view, draft=draft)


def test_legacy_unconfirmed_single_code_and_empty_boundaries(fixture):
    view = create(fixture)[0]
    legacy = h.create_draft(view["header"]); del legacy["diagnosisOrderConfirmed"]
    assert h.normalize_draft(view["header"], legacy)["diagnosisOrderConfirmed"] is False
    single = confirm(fixture, view)
    assert h.diagnosis_order(single["revision"]["draft"])[0]["code"] == "R51.9"
    empty = h.create_draft(view["header"]); empty["codes"] = []; empty["diagnosisOrderConfirmed"] = True
    with pytest.raises(h.BillingHeaderError, match="invalid_edit"):
        save(fixture, single, draft=empty)
    with pytest.raises(h.BillingHeaderError):
        h.identifier(h.DIAGNOSIS_ORDER_DECISION)


def test_sequence_audit_binds_evidence_changes_with_a_bounded_digest(fixture):
    view = baseline(fixture)
    draft = h.create_draft(view["header"]); draft["diagnosisOrderConfirmed"] = True
    maximum = [{**draft["codes"][0], "id": "x" * 155 + str(i), "evidenceIds": ["e" * 150 + str(j) for j in range(320)]} for i in range(20)]
    large = {**draft, "codes": maximum}
    snapshot = h.diagnosis_order_snapshot(large)
    assert len(h.canonical(snapshot)) < 10000 and len(snapshot["contentSha256"]) == 64
    changed = deepcopy(large); changed["codes"][0]["evidenceIds"].pop()
    assert h.diagnosis_order_snapshot(changed)["contentSha256"] != snapshot["contentSha256"]
    unchanged = deepcopy(large); unchanged["codes"][0]["evidenceIds"].reverse()
    assert h.diagnosis_order_snapshot(unchanged) == snapshot


@pytest.mark.parametrize("primary_action,sequence_decision,valid", [
    ("accept", "accept", True), ("reject", "accept", False),
    ("reject", "reject", True), ("accept", "reject", True),
])
def test_primary_rejection_never_substitutes_secondary(fixture, primary_action, sequence_decision, valid):
    view = confirm(fixture, baseline(fixture))
    record = review_record(fixture[2], fixture[3], view)
    if primary_action == "reject":
        record.decisions[0] = CodeDecision("ICD10", "R51.9", "reject", None, "other", "Not supported")
    decisions = [choice(sequence_decision)]
    args = (view["header"], view["revision"], record, context_for(record), decisions, True)
    if not valid:
        with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
            validate_signoff(*args)
        return
    outcomes, _ = validate_signoff(*args)
    result = accepted_diagnosis_order(view["header"], view["revision"], record.decisions,
                                      {d["id"]: d for d in outcomes})
    assert [row["code"] for row in result] == (["R51.9", "R53.83"] if sequence_decision == "accept" else [])


def test_secondary_rejection_preserves_primary_but_unlisted_addition_blocks_order(fixture):
    view = confirm(fixture, baseline(fixture))
    record = review_record(fixture[2], fixture[3], view)
    record.decisions[1] = CodeDecision("ICD10", "R53.83", "reject", None, "other", "Not retained")
    validate_signoff(view["header"], view["revision"], record, context_for(record), [choice()], True)
    assert [row["code"] for row in accepted_diagnosis_order(view["header"], view["revision"], record.decisions,
        {h.DIAGNOSIS_ORDER_DECISION: choice()})] == ["R51.9"]
    record.decisions.append(CodeDecision("ICD10", None, "add", "Z31.69", "other", "Rationale: reviewed. Evidence: source"))
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        validate_signoff(view["header"], view["revision"], record, context_for(record), [choice()], True)


@pytest.mark.parametrize("kind", ["modify", "add"])
def test_human_primary_requires_eligible_matching_code_decision(fixture, kind):
    initial = create(fixture)[0]
    draft = h.create_draft(initial["header"])
    if kind == "modify": draft["codes"][0].update(code="R53.83", label="Human proposed fatigue")
    else: draft["codes"].insert(0, {"id": "human-headache", "kind": "ICD10", "code": "R53.83", "label": "Manual diagnosis", "evidenceIds": ["ev-1"]})
    draft["diagnosisOrderConfirmed"] = True
    view = save(fixture, initial, draft=draft)[0]
    record = review_record(fixture[2], fixture[3], view)
    row = draft["codes"][0]
    outcomes = [choice(), {"id": row["id"], "decision": "accept", "reason": "Human source verification"}]
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        validate_signoff(view["header"], view["revision"], record, context_for(record), outcomes, True)
    decision = CodeDecision("ICD10", "R51.9" if kind == "modify" else None, kind, "R53.83", "other",
                            "Rationale: human verification. Evidence: Headache")
    if kind == "modify": record.decisions[0] = decision
    else: record.decisions.append(decision)
    validate_signoff(view["header"], view["revision"], record, context_for(record), outcomes, True)
    assert initial["header"] == view["header"] and view["revision"]["draft"]["codes"][0]["evidenceIds"] == ["ev-1"]


def test_rejected_evidence_proposal_cannot_authorize_primary(fixture):
    view = baseline(fixture)
    draft = h.create_draft(view["header"]); draft["codes"][0]["label"] = "Revised human description"
    draft["diagnosisOrderConfirmed"] = True
    view = save(fixture, view, draft=draft)[0]
    record = review_record(fixture[2], fixture[3], view)
    outcomes = [choice(), {"id": "code-1", "decision": "reject", "reason": "Reject altered support"}]
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        validate_signoff(view["header"], view["revision"], record, context_for(record), outcomes, True)


@pytest.mark.parametrize("mutation", ["absent", "duplicate", "empty_reason", "unknown", "legacy"])
def test_confirmation_requires_exact_separate_decision(fixture, mutation):
    view = baseline(fixture)
    if mutation != "legacy": view = confirm(fixture, view)
    record = review_record(fixture[2], fixture[3], view)
    decisions = [choice()]
    if mutation == "absent": decisions = []
    elif mutation == "duplicate": decisions *= 2
    elif mutation == "empty_reason": decisions[0]["reason"] = " "
    elif mutation == "unknown": decisions[0]["id"] += "-unknown"
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        validate_signoff(view["header"], view["revision"], record, context_for(record), decisions, True)


def test_confirmed_order_receipt_replays_reloads_and_new_revision_invalidates(fixture):
    store, _, stored, actor = fixture
    view = confirm(fixture, baseline(fixture))
    request = options(stored, replace(actor, role="Coder"), view, MemoryHeaderFeedbackAdapter(store))
    request["human_proposal_decisions"] = [choice()]
    reviewed, created = store.signoff(stored.bundle.encounter_id, **request)
    assert created and reviewed["signoff"]["actor"]["id"] == actor.user_id
    assert store.signoff(stored.bundle.encounter_id, **request) == (reviewed, False)
    assert store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored)) == reviewed
    changed, _ = save(fixture, reviewed)
    assert changed["signoff"] is None and changed["previousSignoff"]["headerRevision"] == 1
    assert changed["revision"]["draft"]["diagnosisOrderConfirmed"] is True  # Only note changed.
    assert len([c for r in changed["revision"]["history"] for c in r["changes"] if c["path"] == "diagnosisOrder"]) == 1
    assert changed["header"] == view["header"]
    with pytest.raises(h.BillingHeaderError, match="revision_conflict"):
        store.signoff(stored.bundle.encounter_id, **request)


def test_maximum_twenty_row_replacement_has_41_decisions_and_51_audit_changes(fixture):
    from cosmaneura_billing.knowledge.icd10_ccsr import ICD10CCSR
    catalogue = ICD10CCSR()
    raw = [code for code, (billable, _) in catalogue._current.items() if billable][:40]
    codes = [code[:3] + "." + code[3:] if len(code) > 3 else code for code in raw]
    body = generated(fixture[2]); template = body["suggestions"][0]
    body["suggestions"] = [{**template, "id": f"original-{i}", "code": code} for i, code in enumerate(codes[:20])]
    initial = create(fixture, generated=body)[0]
    draft = h.create_draft(initial["header"])
    draft["codes"] = [{**h.code_fields(template), "id": f"human-{i}", "code": code} for i, code in enumerate(codes[20:])]
    draft["facts"] = {key: "Synthetic human entry" for key in h.FACT_KEYS}
    draft["note"] = "Synthetic capacity review"; draft["diagnosisOrderConfirmed"] = True
    view = save(fixture, initial, draft=draft)[0]
    assert len(view["revision"]["changes"]) == 51
    record = review_record(fixture[2], fixture[3], view)
    record.decisions = [CodeDecision("ICD10", before, "modify", after, "other", "Synthetic source review")
                        for before, after in zip(codes[:20], codes[20:])]
    decisions = [choice()] + [{"id": f"{prefix}-{i}", "decision": "accept", "reason": "Source reviewed"}
                              for prefix in ("original", "human") for i in range(20)]
    assert len(validate_signoff(view["header"], view["revision"], record, context_for(record), decisions, True)[0]) == 41
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        validate_signoff(view["header"], view["revision"], record, context_for(record), [*decisions, choice()], True)
