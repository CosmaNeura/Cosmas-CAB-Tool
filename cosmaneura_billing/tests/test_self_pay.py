"""F5 step-0 acceptance (synthesis §F5 + §F3): contract shape, deterministic
synthetic balances, and the integration proof that a direct-contract patient
can never enter a self-pay chase sequence — including the fail-closed path."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cosmaneura_billing.chase import ChaseQueue, QueueType, SequenceError
from cosmaneura_billing.chase.self_pay import (
    enqueue_balances, generate_synthetic_balances,
)
from cosmaneura_billing.guards import PracticeListSource

NOW = datetime(2026, 8, 12, 22, 0, tzinfo=timezone.utc)


def _list_source(tmp_path, refs=("PT-DC-0003",)):
    p = tmp_path / "dc.json"
    p.write_text(json.dumps({"version": "practice_list.v1",
                             "patient_refs": list(refs)}))
    return PracticeListSource(p)


def test_contract_shape_matches_synthesis_f5(tmp_path):
    b = generate_synthetic_balances(3, now=NOW,
                                    list_source=_list_source(tmp_path))[0]
    j = b.to_json()
    assert set(j) == {"balance_id", "patient_ref", "amount_cents",
                      "aging_days", "direct_contract_check",
                      "sequence_state", "contact_log"}
    assert j["sequence_state"] == "draft"
    assert j["contact_log"] == []
    assert set(j["direct_contract_check"]) == {"is_direct_contract", "source",
                                               "checked_at", "status"}


def test_generator_is_deterministic_and_plausible(tmp_path):
    src = _list_source(tmp_path)
    a = generate_synthetic_balances(10, now=NOW, list_source=src)
    b = generate_synthetic_balances(10, now=NOW, list_source=src)
    assert [x.to_json() for x in a] == [y.to_json() for y in b]
    for bal in a:
        assert 2500 <= bal.amount_cents < 40000
        assert 5 <= bal.aging_days <= 120


def test_direct_contract_patient_never_enters_the_sequence(tmp_path):
    """The §F3 acceptance integration test, on the real chassis door."""
    src = _list_source(tmp_path)
    balances = generate_synthetic_balances(6, now=NOW, list_source=src)
    queue = ChaseQueue(QueueType.SELF_PAY, list_source=src)
    accepted, blocked = enqueue_balances(queue, balances, now=NOW)

    assert len(blocked) == 1
    assert blocked[0]["patient_ref"] == "PT-DC-0003"
    assert "direct-contract" in blocked[0]["reason"]
    queued_refs = {i.patient_ref for i in queue.items.values()}
    assert "PT-DC-0003" not in queued_refs
    assert len(accepted) == len(balances) - 1
    assert all(i.sequence_state == "draft" for i in accepted)


def test_unverifiable_list_blocks_every_balance(tmp_path):
    missing = PracticeListSource(tmp_path / "nope.json")
    balances = generate_synthetic_balances(4, now=NOW, list_source=missing)
    queue = ChaseQueue(QueueType.SELF_PAY, list_source=missing)
    accepted, blocked = enqueue_balances(queue, balances, now=NOW)
    assert accepted == [] and len(blocked) == 4     # fail closed, all of it
    assert queue.items == {}


def test_draft_items_move_only_by_staff_approval(tmp_path):
    src = _list_source(tmp_path)
    queue = ChaseQueue(QueueType.SELF_PAY, list_source=src)
    balances = generate_synthetic_balances(3, now=NOW, list_source=src)
    accepted, _ = enqueue_balances(queue, balances, now=NOW)
    item = accepted[0]
    # no outreach mechanics exist for self-pay tonight: sending from draft is
    # structurally impossible, and there are no self-pay templates to send
    with pytest.raises(SequenceError):
        queue.send(item.chase_id, template_id="card_request.v0", now=NOW,
                   sent_by="s1")
    queue.approve(item.chase_id, staff_id="s1", now=NOW)
    assert item.sequence_state == "staff_approved"


def test_wrong_queue_type_is_refused(tmp_path):
    src = _list_source(tmp_path)
    card_queue = ChaseQueue(QueueType.CARD_CHASE, list_source=src)
    balances = generate_synthetic_balances(2, now=NOW, list_source=src)
    with pytest.raises(SequenceError, match="self_pay"):
        enqueue_balances(card_queue, balances, now=NOW)
