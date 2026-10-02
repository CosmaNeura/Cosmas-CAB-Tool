"""Current-header receipts compose, rather than replace, coder feedback."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.billing_header_signoff import MemoryHeaderFeedbackAdapter, review_manifest
from cosmaneura_billing.coder_feedback import CodeDecision, EncounterFeedback, FeedbackStore
from test_billing_header import fixture, create, save, NOW


def review_record(stored, actor, view, **changes):
    decisions = [CodeDecision(row["kind"], row["code"], "accept", row["code"])
                 for row in view["header"]["suggestions"]]
    em = next((d.engine_code for d in decisions if d.kind == "EM"), None)
    return EncounterFeedback(encounter_id=stored.bundle.encounter_id, coder_id=actor.user_id,
        coder_tenant_id=actor.tenant_id, engine_version="synthetic-test",
        submitted_em=stored.bundle.submitted_em_code, engine_em=em, final_em=em,
        decisions=decisions, encounter_version=stored.version, **changes)


def context_for(record):
    count, digest = review_manifest(record)
    return {"recommendationCount": count, "recommendationDigest": digest, "engineVersion": record.engine_version}


def options(stored, actor, view, feedback_store, record=None):
    record = record or review_record(stored, actor, view)
    return dict(header_id=view["header"]["headerId"], expected_source=h.source_snapshot(stored),
        expected_revision=view["revisionNumber"], feedback=record, verified_context=context_for(record),
        human_proposal_decisions=[], acknowledged=True, actor=actor, feedback_store=feedback_store)


def test_memory_feedback_and_receipt_commit_together_and_edit_invalidates(fixture):
    store, _, stored, actor = fixture
    actor = replace(actor, role="Coder")
    current, _ = create(fixture)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    request = options(stored, actor, current, feedback_store)
    reviewed, created = store.signoff(stored.bundle.encounter_id, **request)
    assert created and feedback_store.count() == 1
    assert reviewed["signoff"]["recommendationDigest"] == request["verified_context"]["recommendationDigest"]
    assert reviewed["signoff"]["headerRevision"] == 0
    replay, created = store.signoff(stored.bundle.encounter_id, **request)
    assert not created and replay == reviewed and feedback_store.count() == 1
    revised, _ = save(fixture, reviewed)
    assert revised["signoff"] is None and revised["previousSignoff"]["headerRevision"] == 0
    with pytest.raises(h.BillingHeaderError, match="revision_conflict"):
        store.signoff(stored.bundle.encounter_id, **request)
    signed_again, _ = store.signoff(stored.bundle.encounter_id, **options(stored, actor, revised, feedback_store))
    assert signed_again["signoff"]["headerRevision"] == 1 and feedback_store.count() == 2
    signed_again["signoff"]["reviewerNote"] = "tamper"
    assert store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored))["signoff"]["reviewerNote"] == ""


@pytest.mark.parametrize("bad", ["provider", "no_ack", "truthy_ack", "manifest", "feedback_only", "separate_store", "source"])
def test_signoff_cannot_bypass_review_or_source_boundary(fixture, tmp_path, bad):
    store, encounters, stored, actor = fixture
    actor = replace(actor, role="Coder")
    current, _ = create(fixture)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    request = options(stored, actor, current, feedback_store)
    if bad == "provider": request["actor"] = replace(actor, role="Provider")
    elif bad == "no_ack": request["acknowledged"] = False
    elif bad == "truthy_ack": request["acknowledged"] = 1
    elif bad == "manifest": request["verified_context"]["recommendationDigest"] = "0" * 64
    elif bad == "feedback_only": feedback_store.append(request["feedback"])
    elif bad == "separate_store": request["feedback_store"] = FeedbackStore(tmp_path / "separate.jsonl")
    else: encounters.upsert(replace(stored.bundle, transcript="Changed synthetic source"))
    with pytest.raises(h.BillingHeaderError):
        store.signoff(stored.bundle.encounter_id, **request)
    assert store._data["signoffs"] == {}
    assert feedback_store.count() == (1 if bad == "feedback_only" else 0)


def test_each_human_code_change_needs_explicit_outcome_consistent_with_feedback(fixture):
    store, _, stored, actor = fixture
    actor = replace(actor, role="Coder")
    baseline, _ = create(fixture)
    draft = h.create_draft(baseline["header"])
    draft["codes"][0].update(code="R53.83", label="Human proposed fatigue")
    edited, _ = save(fixture, baseline, draft=draft)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    request = options(stored, actor, edited, feedback_store)
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        store.signoff(stored.bundle.encounter_id, **request)
    request["human_proposal_decisions"] = [{"id": "code-1", "decision": "accept", "reason": "Reviewed evidence"}]
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        store.signoff(stored.bundle.encounter_id, **request)  # Feedback still accepts the original.
    request["feedback"].decisions[0] = CodeDecision("ICD10", "R51.9", "modify", "R53.83", "other", "Synthetic reviewed rationale")
    request["verified_context"] = context_for(request["feedback"])
    reviewed, _ = store.signoff(stored.bundle.encounter_id, **request)
    assert reviewed["signoff"]["humanProposalDecisions"][0]["decision"] == "accept"
    assert reviewed["header"] == baseline["header"]


@pytest.mark.parametrize("drift", [False, True])
def test_signoff_memory_audit_failure_rolls_back_feedback_and_receipt(fixture, monkeypatch, drift):
    from cosmaneura_billing.billing_header_store import _MemoryUnit
    store, _, stored, actor = fixture
    view, _ = create(fixture)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    before = deepcopy(store._data)
    def unavailable(*args, **kwargs): raise h.BillingHeaderError("audit_required")
    monkeypatch.setattr(_MemoryUnit, "audit", unavailable)
    actor = replace(actor, role="Coder")
    request = options(stored, actor, view, feedback_store,
                      drift_record(stored, actor, view) if drift else None)
    if drift:
        request["baseline_decisions"] = [{"id": "code-1", "decision": "reject", "reason": "Reviewed earlier source"}]
    with pytest.raises(h.BillingHeaderError, match="audit_required"):
        store.signoff(stored.bundle.encounter_id, **request)
    assert store._data == before


@pytest.mark.parametrize("choice,final_action,valid", [
    ("accept", "reject", True), ("accept", "accept", False),
    ("reject", "accept", True), ("reject", "reject", False),
])
def test_removal_proposal_outcome_matches_retained_or_removed_feedback(fixture, choice, final_action, valid):
    store, _, stored, actor = fixture
    baseline, _ = create(fixture)
    draft = h.create_draft(baseline["header"]); draft["codes"] = []
    edited, _ = save(fixture, baseline, draft=draft)
    request = options(stored, replace(actor, role="Coder"), edited, MemoryHeaderFeedbackAdapter(store))
    request["human_proposal_decisions"] = [{"id": "code-1", "decision": choice, "reason": "Reviewed removal"}]
    if final_action == "reject":
        request["feedback"].decisions[0] = CodeDecision("ICD10", "R51.9", "reject", None, "other", "Reviewed removal")
    request["verified_context"] = context_for(request["feedback"])
    if valid:
        assert store.signoff(stored.bundle.encounter_id, **request)[1]
    else:
        with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
            store.signoff(stored.bundle.encounter_id, **request)


def test_concurrent_coders_cannot_overwrite_one_header_revision(fixture):
    store, _, stored, actor = fixture
    current, _ = create(fixture)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    def attempt(_):
        try:
            store.signoff(stored.bundle.encounter_id, **options(stored, replace(actor, role="Coder"), current, feedback_store))
            return "recorded"
        except h.BillingHeaderError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ["recorded", "review_conflict"]
    assert feedback_store.count() == 1


def drift_record(stored, actor, view, *, retain=False, empty=False):
    record = review_record(stored, actor, view)
    record.decisions = [] if empty else [CodeDecision("ICD10", "R05.9", "accept", "R05.9")]
    if retain:
        record.decisions.append(CodeDecision("ICD10", None, "add", "R51.9",
            "dx_missed_by_engine", "Rationale: Reviewed original headache.\nEvidence: Headache"))
    record.engine_em = record.final_em = None
    return record


@pytest.mark.parametrize("retain,empty", [(False, False), (True, False), (False, True)])
def test_same_source_manifest_drift_requires_reconciliation_and_preserves_edits(fixture, retain, empty):
    store, encounters, stored, actor = fixture
    actor = replace(actor, role="Coder")
    baseline, _ = create(fixture)
    view, _ = save(fixture, baseline)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    request = options(stored, actor, view, feedback_store,
                      drift_record(stored, actor, view, retain=retain, empty=empty))
    before = deepcopy(store._data)
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        store.signoff(stored.bundle.encounter_id, **request)
    assert store._data == before
    choice = {"id": "code-1", "decision": "retain" if retain else "reject",
              "reason": "Reviewed the earlier suggestion against the unchanged source"}
    request["baseline_decisions"] = [choice]
    reviewed, created = store.signoff(stored.bundle.encounter_id, **request)
    assert created and reviewed["header"] == baseline["header"] and reviewed["revision"] == view["revision"]
    assert reviewed["signoff"]["baselineDecisions"] == [choice]
    assert encounters.get(stored.bundle.encounter_id) == stored and feedback_store.count() == 1
    assert store.signoff(stored.bundle.encounter_id, **request) == (reviewed, False)
    request["baseline_decisions"][0]["reason"] = "Changed retry intent"
    with pytest.raises(h.BillingHeaderError, match="operation_conflict"):
        store.signoff(stored.bundle.encounter_id, **request)


@pytest.mark.parametrize("bad", ["missing", "unknown", "duplicate", "blank", "extra", "retain_absent", "reject_present"])
def test_baseline_reconciliation_cannot_invent_review_or_override_final_codes(fixture, bad):
    store, _, stored, actor = fixture
    view, _ = create(fixture)
    actor = replace(actor, role="Coder")
    request = options(stored, actor, view, MemoryHeaderFeedbackAdapter(store),
                      drift_record(stored, actor, view, retain=bad == "reject_present"))
    choice = {"id": "code-1", "decision": "reject", "reason": "Reviewed evidence"}
    choices = [choice]
    if bad == "missing": choices = []
    elif bad == "unknown": choice["id"] = "unknown"
    elif bad == "duplicate": choices *= 2
    elif bad == "blank": choice["reason"] = " "
    elif bad == "extra": choice["code"] = "R51.9"
    elif bad == "retain_absent": choice["decision"] = "retain"
    request["baseline_decisions"] = choices
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        store.signoff(stored.bundle.encounter_id, **request)
    assert request["feedback_store"].count() == 0 and store._data["signoffs"] == {}


def test_baseline_reconciliation_does_not_replace_human_proposal_review(fixture):
    store, _, stored, actor = fixture
    baseline, _ = create(fixture)
    draft = h.create_draft(baseline["header"])
    draft["codes"][0].update(code="R53.83", label="Human fatigue proposal")
    view, _ = save(fixture, baseline, draft=draft)
    actor = replace(actor, role="Coder")
    request = options(stored, actor, view, MemoryHeaderFeedbackAdapter(store), drift_record(stored, actor, view))
    request["baseline_decisions"] = [{"id": "code-1", "decision": "reject", "reason": "Original not retained"}]
    request["feedback"].decisions.append(CodeDecision("ICD10", None, "add", "R53.83",
        "dx_missed_by_engine", "Rationale: Human proposal reviewed.\nEvidence: Synthetic excerpt"))
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        store.signoff(stored.bundle.encounter_id, **request)
    request["human_proposal_decisions"] = [{"id": "code-1", "decision": "accept", "reason": "Human proposal reviewed"}]
    reviewed, _ = store.signoff(stored.bundle.encounter_id, **request)
    assert reviewed["revision"] == view["revision"]
