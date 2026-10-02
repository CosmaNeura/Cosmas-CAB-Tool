"""Stored receipts replay exactly; catalogue changes never authorize new writes."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cosmaneura_billing import billing_header as h, icd10_validity
from cosmaneura_billing.billing_header_signoff import MemoryHeaderFeedbackAdapter
from cosmaneura_billing.coder_feedback import (
    CodeDecision, EncounterFeedback, FeedbackConflict, FeedbackStore,
    InvalidCurrentICD10, feedback_payload_digest, validate_feedback,
)
from cosmaneura_billing.persistence import PostgresFeedbackStore
from test_billing_header import fixture, create
from test_billing_header_signoff import options


def feedback():
    return EncounterFeedback("SYN-REPLAY", str(uuid4()), "synthetic-test", None, None, None,
        [CodeDecision("ICD10", "R51.9", "accept", "R51.9")], coder_tenant_id=str(uuid4()))


def unavailable(monkeypatch, active):
    monkeypatch.setattr(icd10_validity, "_current_catalogue", lambda: SimpleNamespace(
        has_current_fy=active is not None, is_active=lambda _code: active))


@pytest.mark.parametrize("active", [False, None])
@pytest.mark.parametrize("adapter", ["file", "header_memory"])
def test_feedback_replay_keeps_original_receipt_after_catalogue_change(
        fixture, tmp_path, monkeypatch, active, adapter):
    store = (FeedbackStore(tmp_path / "feedback.jsonl") if adapter == "file"
             else MemoryHeaderFeedbackAdapter(fixture[0]))
    record = feedback()
    assert store.append(record)
    before = [row.to_dict() for row in store.load()]
    unavailable(monkeypatch, active)

    assert store.exact_replay(record) is True
    assert store.append(record) is False
    assert [row.to_dict() for row in store.load()] == before
    with pytest.raises(InvalidCurrentICD10):
        store.append(replace(record, review_id=str(uuid4())))
    with pytest.raises(FeedbackConflict):
        store.append(replace(record, overall_note="Changed retry intent"))
    malformed = deepcopy(record)
    malformed.decisions[0].action = "not-an-action"
    with pytest.raises(ValueError, match="controlled feedback decision"):
        store.exact_replay(malformed)
    assert [row.to_dict() for row in store.load()] == before


@pytest.mark.parametrize("active", [False, None])
def test_header_signoff_replay_does_not_revalidate_current_codes(fixture, monkeypatch, active):
    store, encounters, stored, actor = fixture
    actor = replace(actor, role="Coder")
    view, _ = create(fixture)
    feedback_store = MemoryHeaderFeedbackAdapter(store)
    request = options(stored, actor, view, feedback_store)
    reviewed, created = store.signoff(stored.bundle.encounter_id, **request)
    assert created
    before = deepcopy(store._data)
    unavailable(monkeypatch, active)

    replay, created = store.signoff(stored.bundle.encounter_id, **request)
    assert not created and replay == reviewed
    assert store._data["signoffs"] == before["signoffs"] and feedback_store.count() == 1
    changed = {**request, "feedback": replace(request["feedback"], overall_note="Changed intent")}
    with pytest.raises(h.BillingHeaderError, match="operation_conflict"):
        store.signoff(stored.bundle.encounter_id, **changed)
    for change in ({"acknowledged": False}, {"verified_context": {}},
                   {"actor": replace(actor, role="Provider")}):
        with pytest.raises(h.BillingHeaderError):
            store.signoff(stored.bundle.encounter_id, **{**request, **change})

    # A second header has no receipt to replay: the same now-invalid final code
    # must pass the current check before either feedback or a receipt is stored.
    fresh_source = encounters.upsert(replace(stored.bundle, encounter_id="SYN-NEW-REVIEW"))
    fresh, _ = create((store, encounters, fresh_source, actor))
    new_request = options(fresh_source, actor, fresh, feedback_store)
    with pytest.raises(h.BillingHeaderError, match="invalid_signoff"):
        store.signoff(fresh_source.bundle.encounter_id, **new_request)
    assert feedback_store.count() == 1


class ReceiptConnection:
    """Only receipt lookup, reviewer and encounter reads are allowed here."""
    def __init__(self, record, existing):
        self.record, self.existing = record, existing
        self.queries = []

    def execute(self, sql, args):
        self.queries.append(sql)
        if "SELECT payload_hash" in sql:
            row = {"payload_hash": feedback_payload_digest(self.record)} if self.existing else None
        elif "cosmaneura_feedback_reviewer_allowed" in sql:
            row = {"allowed": True}
        elif "SELECT encounter_uid, version" in sql:
            row = {"encounter_uid": str(uuid4()), "version": self.record.encounter_version}
        else:
            pytest.fail("invalid new feedback attempted a database mutation")
        return SimpleNamespace(fetchone=lambda: row)


class ReceiptContext:
    practice_id = "synthetic-practice"

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        yield self.conn


@pytest.mark.parametrize("active", [False, None])
def test_postgres_receipt_lookup_precedes_current_validation_without_write_bypass(monkeypatch, active):
    record = feedback()
    validate_feedback(record)  # The stored receipt represents a previously valid request.
    conn = ReceiptConnection(record, existing=True)
    store = PostgresFeedbackStore(ReceiptContext(conn))
    unavailable(monkeypatch, active)

    assert store.exact_replay(record) is True
    assert store.append(record) is False
    assert all("SELECT payload_hash" in sql for sql in conn.queries)
    with pytest.raises(FeedbackConflict):
        store.append(replace(record, coder_id=str(uuid4())))
    with pytest.raises(FeedbackConflict):
        store.exact_replay(replace(record, overall_note="Changed intent"))

    conn.existing = False
    with pytest.raises(InvalidCurrentICD10):
        store.append(record)
    assert not any("INSERT" in sql for sql in conn.queries)
