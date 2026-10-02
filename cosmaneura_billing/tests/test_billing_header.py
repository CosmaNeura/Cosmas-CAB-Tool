"""Wholly invented headers; no model or external source calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.access_audit import AccessAuditEvent
from cosmaneura_billing.billing_header_store import MemoryBillingHeaderStore
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.schemas import VisitBundle

NOW = "2026-09-29T16:00:00+00:00"


def generated(stored):
    doc = h.source_documents(stored)[0]
    return {
        "facts": [{"key": key, "label": key.replace("_", " "), "status": "not_documented",
                   "value": None, "evidenceIds": []} for key in h.FACT_KEYS],
        "evidence": [{"id": "ev-1", "source": "note", "sourceId": doc["id"],
                      "sourceVersion": doc["version"], "start": 0, "end": 8,
                      "text": doc["text"][:8], "coordinateSystem": "unicode_code_point"}],
        "suggestions": [{"id": "code-1", "kind": "ICD10", "code": "R51.9", "label": "Headache",
                         "codeSet": "ICD-10-CM", "codeSetVersion": "2026", "reviewStatus": "unreviewed",
                         "evidenceIds": ["ev-1"], "basis": "Source documents headache."}],
        "flags": [{"id": "missing-consent", "message": "Consent is not documented."}],
        "policy": {"status": "not_verified", "message": "Payer rules not verified."},
        "generation": {"engineVersion": "synthetic-test", "runId": str(uuid4()), "inferenceMode": "mock"},
    }


@pytest.fixture
def fixture():
    encounter = VisitBundle("SYN-HEADER", 30, "female", "office_established", "11",
                            signed_note="Headache 🙂 unchanged synthetic note.", transcript="Synthetic transcript.")
    encounters = MemoryEncounterStore([encounter], practice_id="header-local")
    actor = h.HeaderActor(str(uuid4()), str(uuid4()), "Synthetic Provider", "Provider")
    store = MemoryBillingHeaderStore(encounters, practice_id="header-local", authorize=lambda _actor: True,
                                     clock=lambda: NOW)
    stored = encounters.get(encounter.encounter_id)
    return store, encounters, stored, actor


def create(fixture, **overrides):
    store, _, stored, actor = fixture
    options = {"expected_source": h.source_snapshot(stored), "generated": generated(stored),
               "actor": actor, "operation_id": str(uuid4()), **overrides}
    return store.create(stored.bundle.encounter_id, **options)


def save(fixture, view, **overrides):
    store, _, stored, actor = fixture
    draft = h.create_draft(view["header"], view["revision"])
    draft["note"] += "Human review comment."
    options = {"header_id": view["header"]["headerId"], "expected_source": h.source_snapshot(stored),
               "expected_revision": view["revisionNumber"], "draft": draft, "reason": "Review detail",
               "actor": actor, "operation_id": str(uuid4()), **overrides}
    return store.save(stored.bundle.encounter_id, **options)


def test_generated_baseline_and_both_roles_preserve_note_and_attribution(fixture):
    store, encounters, stored, actor = fixture
    original = deepcopy(stored.bundle)
    initial, created = create(fixture)
    assert created and initial["revisionNumber"] == 0 and initial["revision"] is None
    first, _ = save(fixture, initial)
    coder = replace(actor, user_id=str(uuid4()), name="Synthetic Coder", role="Coder")
    draft = h.create_draft(first["header"], first["revision"])
    draft["facts"]["consent"] = "Human entry; verify original documentation."
    draft["codes"][0]["code"] = "R53.83"
    draft["codes"][0]["label"] = "Human proposed code"
    second, _ = save(fixture, first, actor=coder, draft=draft)
    assert second["header"] == initial["header"]
    assert second["header"]["facts"][6]["status"] == "not_documented"
    assert second["header"]["flags"] == initial["header"]["flags"]
    assert second["revision"]["reviewStatus"] == "unreviewed"
    assert [row["actor"]["role"] for row in second["revision"]["history"]] == ["provider", "coder"]
    assert encounters.get(stored.bundle.encounter_id).bundle == original
    assert len(store.audit_events) == 3
    second["header"]["suggestions"][0]["code"] = "tampered"
    second["revision"]["history"][0]["reason"] = "tampered"
    assert store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored))["header"] == initial["header"]


@pytest.mark.parametrize("part", ["signed_note", "transcript", "intake_text", "place_of_service"])
def test_exact_source_changes_and_boolean_version_fail_closed(fixture, part):
    store, encounters, stored, _ = fixture
    initial, _ = create(fixture)
    expected = h.source_snapshot(stored)
    encounters.upsert(replace(stored.bundle, **{part: "Changed synthetic source"}))
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        store.latest(stored.bundle.encounter_id, expected_source=expected)
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        save(fixture, initial)


def test_boolean_expected_version_not_equal_to_integer(fixture):
    expected = h.source_snapshot(fixture[2])
    expected["encounterVersion"] = True
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        create(fixture, expected_source=expected)


def test_provenance_change_without_note_change_invalidates_header(fixture):
    store, encounters, stored, _ = fixture
    initial, _ = create(fixture)
    updated = encounters.upsert(stored.bundle, source="changed_test_provenance")
    assert updated.version == stored.version and updated.bundle == stored.bundle
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored))
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        save(fixture, initial)


def test_generation_replay_binds_operation_without_rerunning_pipeline(fixture):
    store, _, stored, actor = fixture
    operation = str(uuid4())
    assert store.generation_replay(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                                   operation_id=operation, actor=actor) is None
    baseline, _ = create(fixture, operation_id=operation)
    saved, _ = save(fixture, baseline)
    replayed = store.generation_replay(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                                       operation_id=operation, actor=actor)
    assert replayed == saved  # Deduplication never rolls the rendered head back.
    latest = store.generation_replay(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored),
                                     operation_id=str(uuid4()), actor=actor)
    assert latest == saved  # A different generation request never discards edits.


def test_exact_retry_after_later_revision_and_changed_intent_conflict(fixture):
    initial, _ = create(fixture)
    operation = str(uuid4())
    first, _ = save(fixture, initial, operation_id=operation)
    second, _ = save(fixture, first)
    replayed, created = save(fixture, initial, operation_id=operation)
    assert not created and replayed == second
    with pytest.raises(h.BillingHeaderError, match="operation_conflict"):
        save(fixture, initial, operation_id=operation, reason="Different reason")


def test_concurrent_saves_only_one_revision_wins(fixture):
    initial, _ = create(fixture)
    def attempt(_):
        try:
            return save(fixture, initial)[0]["revisionNumber"]
        except h.BillingHeaderError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sorted(results, key=str) == [1, "revision_conflict"]


@pytest.mark.parametrize("mutation", ["unknown_field", "bad_evidence", "bad_span", "duplicate", "bool_span",
                                     "missing_fact", "missing_evidence", "accepted", "surrogate", "fact_claim"])
def test_generated_projection_rejects_malformed_or_unproven_content(fixture, mutation):
    value = generated(fixture[2])
    if mutation == "unknown_field": value["billable"] = True
    elif mutation == "bad_evidence": value["evidence"][0]["text"] = "Not in source"
    elif mutation == "bad_span": value["evidence"][0]["end"] = 9999
    elif mutation == "duplicate": value["evidence"] *= 2
    elif mutation == "bool_span": value["evidence"][0]["start"] = False
    elif mutation == "missing_fact": value["facts"].pop()
    elif mutation == "missing_evidence": value["suggestions"][0]["evidenceIds"] = []
    elif mutation == "accepted": value["suggestions"][0]["reviewStatus"] = "accepted"
    elif mutation == "surrogate": value["flags"][0]["message"] = "\ud800"
    else: value["facts"][0]["value"] = "Undocumented assertion"
    with pytest.raises(h.BillingHeaderError, match="invalid_header"):
        create(fixture, generated=value)
    assert fixture[0].audit_events == []


@pytest.mark.parametrize("mutation", ["reason", "missing_refs", "unknown_refs", "duplicate_code", "bad_code",
                                     "extra_fact", "oversize", "accepted", "changed_id_kind"])
def test_human_proposals_do_not_bypass_closed_schema_and_evidence(fixture, mutation):
    initial, _ = create(fixture)
    draft = h.create_draft(initial["header"])
    draft["note"] = "Human text"
    reason = "Reason"
    if mutation == "reason": reason = ""
    elif mutation == "missing_refs": draft["codes"][0]["evidenceIds"] = []
    elif mutation == "unknown_refs": draft["codes"][0]["evidenceIds"] = ["new-proof"]
    elif mutation == "duplicate_code": draft["codes"].append({**draft["codes"][0], "id": "new-code"})
    elif mutation == "bad_code": draft["codes"][0].update(kind="EM", code="99058")
    elif mutation == "extra_fact": draft["facts"]["desired_level"] = "99215"
    elif mutation == "oversize": draft["note"] = "x" * 2001
    elif mutation == "accepted": draft["reviewStatus"] = "accepted"
    else: draft["codes"][0]["kind"] = "CPT"
    with pytest.raises(h.BillingHeaderError, match="invalid_edit"):
        save(fixture, initial, draft=draft, reason=reason)


def test_reset_preserves_history_and_original_evidence(fixture):
    initial, _ = create(fixture)
    first, _ = save(fixture, initial)
    reset, _ = save(fixture, first, draft=h.create_draft(initial["header"]), reason="Return to generated values")
    assert reset["revision"]["draft"] == h.create_draft(initial["header"])
    assert len(reset["revision"]["history"]) == 2
    assert reset["header"] == initial["header"]


def test_revocation_audit_failure_and_stale_save_roll_back_all_memory_state(fixture):
    store, _, stored, actor = fixture
    initial, _ = create(fixture)
    audit_before = store.audit_events
    event = AccessAuditEvent(str(uuid4()), NOW, "wrong-practice", actor.tenant_id, actor.user_id,
                             (actor.role,), "billing_header.save", stored.resource_id,
                             "/api/billing-header/save", "POST", 200, "allowed", "")
    with pytest.raises(h.BillingHeaderError, match="audit_required"):
        save(fixture, initial, audit_completion=event)
    assert store.latest(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored)) == initial
    assert store.audit_events == audit_before
    store.authorize = lambda _actor: False
    with pytest.raises(h.BillingHeaderError, match="not_authorized"):
        save(fixture, initial)


def test_unchanged_generated_content_and_code_order_never_become_human_inference(fixture):
    value = generated(fixture[2])
    value["suggestions"][0].update(kind="EM", code="99221", codeSet="CPT", label="x" * 150)
    initial, _ = create(fixture, generated=value)
    result, _ = save(fixture, initial)
    assert result["revision"]["changes"] == [{"path": "note", "label": "Note to reviewer",
                                             "before": "", "after": "Human review comment."}]
    assert result["header"]["suggestions"][0]["code"] == "99221"
