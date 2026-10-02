"""Human review cannot restore invalid ICD-10 codes to final outcomes."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cosmaneura_billing import billing_header as h, icd10_validity
from cosmaneura_billing.billing_header_signoff import MemoryHeaderFeedbackAdapter
from cosmaneura_billing.coder_feedback import (
    CodeDecision, EncounterFeedback, FeedbackStore,
)
from test_billing_header import create, fixture, generated, save
from test_billing_header_signoff import options


def feedback(decision):
    return EncounterFeedback(
        encounter_id="SYN-HUMAN-ICD10", coder_id="synthetic-coder",
        engine_version="synthetic-test", submitted_em=None, engine_em=None,
        final_em=None, decisions=[decision],
    )


@pytest.mark.parametrize("code", ["E66.8", "Q99.9999"])
@pytest.mark.parametrize("action", ["add", "modify", "accept"])
def test_invalid_final_code_is_rejected_before_feedback_store_write(tmp_path, code, action):
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    assert store.append(feedback(CodeDecision("ICD10", None, "add", "R51.9")))
    before = store.path.read_bytes()
    engine_code = None if action == "add" else code if action == "accept" else "R51.9"
    record = feedback(CodeDecision("ICD10", engine_code, action, code, "other", "Reviewed source."))

    with pytest.raises(ValueError, match="currently billable"):
        store.append(record)

    assert store.path.read_bytes() == before
    assert store.count() == 1


@pytest.mark.parametrize("action,final_code", [("reject", None), ("modify", "E66.9")])
def test_invalid_historical_engine_code_can_be_rejected_or_corrected(tmp_path, action, final_code):
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    record = feedback(CodeDecision("ICD10", "E66.8", action, final_code, "other", "Reviewed correction."))

    assert store.append(record)
    saved = store.load()[0].decisions[0]
    assert (saved.engine_code, saved.action, saved.final_code) == ("E66.8", action, final_code)


@pytest.mark.parametrize("availability", ["absent", "missing_current", "unreadable", "unloadable"])
def test_unavailable_catalogue_blocks_final_codes_but_allows_rejection(tmp_path, monkeypatch, availability):
    def catalogue():
        if availability == "absent":
            return None
        if availability == "missing_current":
            return SimpleNamespace(has_current_fy=False)
        error = OSError if availability == "unreadable" else ValueError
        raise error("Synthetic catalogue failure")

    monkeypatch.setattr(icd10_validity, "_current_catalogue", catalogue)
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    with pytest.raises(ValueError, match="currently billable"):
        store.append(feedback(CodeDecision("ICD10", None, "add", "R51.9")))
    assert not store.path.exists()

    assert store.append(feedback(CodeDecision("ICD10", "E66.8", "reject", None)))
    assert store.load()[0].decisions[0].final_code is None


@pytest.mark.parametrize("code", ["E66.8", "Q99.9999"])
@pytest.mark.parametrize("unchanged_baseline", [False, True])
def test_header_cannot_save_invalid_human_addition_or_unchanged_baseline(fixture, code, unchanged_baseline):
    store, _, stored, _ = fixture
    value = generated(stored)
    if unchanged_baseline:
        # Simulate an older generated header predating current-code enforcement.
        value["suggestions"][0]["code"] = code
    view, _ = create(fixture, generated=value)
    draft = h.create_draft(view["header"])
    draft["note"] = "Only the review note changed."
    if not unchanged_baseline:
        draft["codes"].append({
            "id": "human-addition", "kind": "ICD10", "code": code,
            "label": "Synthetic human proposal", "evidenceIds": ["ev-1"],
        })
    before = deepcopy(store._data)

    with pytest.raises(h.BillingHeaderError, match="invalid_edit"):
        save(fixture, view, draft=draft)

    assert store._data == before


def test_header_allows_removal_of_invalid_old_baseline(fixture):
    value = generated(fixture[2])
    value["suggestions"][0]["code"] = "E66.8"
    view, _ = create(fixture, generated=value)
    draft = h.create_draft(view["header"])
    draft["codes"] = []

    revised, created = save(fixture, view, draft=draft)

    assert created and revised["revision"]["draft"]["codes"] == []
    assert revised["header"]["suggestions"][0]["code"] == "E66.8"


@pytest.mark.parametrize("retain", [False, True])
def test_signoff_cannot_retain_invalid_baseline_but_can_record_its_rejection(fixture, retain):
    store, _, stored, actor = fixture
    actor = replace(actor, role="Coder")
    value = generated(stored)
    value["suggestions"][0]["code"] = "E66.8"
    view, _ = create(fixture, generated=value)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    # The current recommendation manifest excludes the invalid earlier code.
    # Retaining it would require an explicit human addition and baseline choice.
    record = EncounterFeedback(
        encounter_id=stored.bundle.encounter_id, coder_id=actor.user_id,
        coder_tenant_id=actor.tenant_id, engine_version="synthetic-test",
        submitted_em=None, engine_em=None, final_em=None,
        encounter_version=stored.version,
        decisions=[CodeDecision("ICD10", None, "add", "E66.8", "other",
                                "Rationale: Reviewed baseline.\nEvidence: Headache")]
        if retain else [],
    )
    request = options(stored, actor, view, feedback_store, record)
    request["baseline_decisions"] = [{
        "id": "code-1", "decision": "retain" if retain else "reject",
        "reason": "Reviewed earlier invalid code against current catalogue.",
    }]
    before = deepcopy(store._data)

    if retain:
        with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
            store.signoff(stored.bundle.encounter_id, **request)
        assert store._data == before and feedback_store.count() == 0
    else:
        reviewed, created = store.signoff(stored.bundle.encounter_id, **request)
        assert created and feedback_store.count() == 1
        assert reviewed["signoff"]["baselineDecisions"] == request["baseline_decisions"]
        assert reviewed["signoff"]["codeDecisions"] == []
        assert reviewed["header"]["suggestions"][0]["code"] == "E66.8"
