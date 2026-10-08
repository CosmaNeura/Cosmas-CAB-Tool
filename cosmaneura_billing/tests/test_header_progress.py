"""Saved work-queue metadata never contains clinical or reviewer payloads."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from cosmaneura_billing import billing_header as h
from cosmaneura_billing.billing_header_signoff import MemoryHeaderFeedbackAdapter
from cosmaneura_billing.billing_header_store import _MemoryUnit
from test_billing_header import fixture, create, save
from test_billing_header_signoff import options


def progress(fixture, **overrides):
    store, _, stored, actor = fixture
    return store.progress(stored.bundle.encounter_id, expected_source=h.source_snapshot(stored), actor=actor, **overrides)


def test_progress_golden_saved_signoff_edit_and_reload(fixture):
    store, _, stored, actor = fixture
    assert progress(fixture)["status"] == "not_generated"
    baseline, _ = create(fixture)
    assert progress(fixture)["status"] == "awaiting_review"
    feedback = MemoryHeaderFeedbackAdapter(store)
    reviewed, _ = store.signoff(stored.bundle.encounter_id,
        **options(stored, replace(actor, role="Coder"), baseline, feedback))
    summary = progress(fixture)
    golden = json.loads((Path(__file__).resolve().parents[1] / "frontend/src/headerProgress.golden.json").read_text())
    expected = {**golden["reviewed"], "bundleSha256": h.source_snapshot(stored)["bundleSha256"],
                "ingestionSource": stored.source}
    assert summary == expected
    assert summary["reviewedRevision"] == 0
    assert not set(summary) & {"header", "codes", "actor", "reviewId", "note", "facts", "evidence"}
    edited, _ = save(fixture, reviewed)
    assert progress(fixture)["status"] == "review_required"
    assert progress(fixture)["revisionNumber"] == 1
    store.signoff(stored.bundle.encounter_id,
        **options(stored, replace(actor, role="Coder"), edited, feedback))
    assert progress(fixture)["status"] == "reviewed"
    assert progress(fixture)["reviewedRevision"] == 1


def test_progress_current_source_change_is_not_previous_review(fixture):
    store, encounters, stored, actor = fixture
    create(fixture)
    changed = encounters.upsert(replace(stored.bundle, signed_note="Different synthetic documentation."))
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        progress(fixture)
    assert store.progress(changed.bundle.encounter_id, expected_source=h.source_snapshot(changed), actor=actor)["status"] == "not_generated"


@pytest.mark.parametrize("field,value", [("headerId", "wrong"), ("expectedSource", {}), ("headerRevision", True), ("headerRevision", 1)])
def test_progress_rejects_corrupt_receipt(fixture, monkeypatch, field, value):
    store, _, stored, actor = fixture
    baseline, _ = create(fixture)
    store.signoff(stored.bundle.encounter_id,
        **options(stored, replace(actor, role="Coder"), baseline, MemoryHeaderFeedbackAdapter(store)))
    receipt = deepcopy(next(iter(store._data["signoffs"].values())))
    receipt["receipt"][field] = value
    monkeypatch.setattr(_MemoryUnit, "signoff_receipt", lambda *_args, **_kwargs: receipt)
    with pytest.raises(h.BillingHeaderError, match="store_invalid"):
        progress(fixture)


def test_progress_rechecks_membership_and_source_under_read_lock(fixture, monkeypatch):
    store, encounters, stored, _ = fixture
    store.authorize = lambda _: False
    with pytest.raises(h.BillingHeaderError, match="not_authorized"):
        progress(fixture)
    store.authorize = lambda _: True
    original = _MemoryUnit.latest
    def changed(unit, digest):
        encounters.upsert(replace(stored.bundle, transcript="Changed while reading."))
        return original(unit, digest)
    monkeypatch.setattr(_MemoryUnit, "latest", changed)
    with pytest.raises(h.BillingHeaderError, match="source_changed"):
        progress(fixture)
