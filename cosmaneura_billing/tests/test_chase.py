"""F9 acceptance (synthesis §F9): triggers fire on seeded states and stay
silent on clean ones; direct-contract never auto-sequences; message bodies
never carry insurance data; the end-to-end mock loop closes items and emits
metrics; writes are idempotent."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from cosmaneura_billing.athena_client import (
    Appointment, ELIGIBILITY_INACTIVE, ELIGIBILITY_TIMEOUT, InsurancePackage,
    MockAthenaNet, PatientRecord,
)
from cosmaneura_billing.chase import (
    ChaseConfig, ChaseQueue, QueueType, SequenceError, detect,
    on_card_verified, simulate_patient_submission, summarize,
)
from cosmaneura_billing.chase.detector import (
    STATE_HUMAN_REVIEW, TRIGGER_EXPIRED, TRIGGER_INACTIVE, TRIGGER_MANUAL,
    TRIGGER_NO_CARD, TRIGGER_TIMEOUT,
)
from cosmaneura_billing.guards import PracticeListSource

NOW = datetime(2026, 8, 12, 21, 0, tzinfo=timezone.utc)


def _athena() -> MockAthenaNet:
    return MockAthenaNet(seed=7, now=NOW)


def _patient(ref: str, *, insurance="active", appt_days: float = 1.0,
             outcome: str = "ACTIVE") -> PatientRecord:
    p = PatientRecord(patient_ref=ref, display_name=f"Synthetic {ref}")
    if insurance == "active":
        p.insurance = InsurancePackage(
            package_id=f"PKG-{ref}", payer_name="Synthetic Health Plan",
            member_id_masked="…1111",
            effective_from="2026-01-01", effective_to="2026-12-31")
    elif insurance == "expired":
        p.insurance = InsurancePackage(
            package_id=f"PKG-{ref}", payer_name="Synthetic Health Plan",
            member_id_masked="…1111",
            effective_from="2025-01-01", effective_to="2026-06-30",
            status="expired")
    p.eligibility_outcome = outcome
    if appt_days is not None:
        p.appointments.append(Appointment(
            appointment_id=f"AP-{ref}", patient_ref=ref,
            scheduled_at=(NOW + timedelta(days=appt_days)).isoformat()))
    return p


def _list_source(tmp_path, refs=("PT-DC-0001",)):
    p = tmp_path / "dc.json"
    p.write_text(json.dumps({"version": "practice_list.v1",
                             "patient_refs": list(refs)}))
    return PracticeListSource(p)


# ------------------------------------------------------------------ #
# F9a — detector triggers
# ------------------------------------------------------------------ #
def test_each_trigger_fires_and_clean_world_is_silent(tmp_path):
    athena = _athena()
    athena._patients = {
        "PT-CLEAN": _patient("PT-CLEAN"),
        "PT-NOCARD": _patient("PT-NOCARD", insurance=None),
        "PT-EXP": _patient("PT-EXP", insurance="expired"),
        "PT-INACT": _patient("PT-INACT", outcome=ELIGIBILITY_INACTIVE),
        "PT-TIME": _patient("PT-TIME", outcome=ELIGIBILITY_TIMEOUT),
        "PT-MANUAL": _patient("PT-MANUAL"),
    }
    items = detect(athena, list_source=_list_source(tmp_path), now=NOW,
                   manual_flags=("PT-MANUAL",))
    got = {i.patient_ref: i.trigger for i in items}
    assert got == {
        "PT-NOCARD": TRIGGER_NO_CARD,
        "PT-EXP": TRIGGER_EXPIRED,
        "PT-INACT": TRIGGER_INACTIVE,
        "PT-TIME": TRIGGER_TIMEOUT,
        "PT-MANUAL": TRIGGER_MANUAL,
    }
    assert "PT-CLEAN" not in got            # silent on clean state


def test_priority_from_appointment_proximity(tmp_path):
    athena = _athena()
    athena._patients = {
        "PT-P0": _patient("PT-P0", insurance=None, appt_days=1),
        "PT-P1": _patient("PT-P1", insurance=None, appt_days=5),
        "PT-P2": _patient("PT-P2", insurance=None, appt_days=12),
    }
    items = detect(athena, list_source=_list_source(tmp_path), now=NOW)
    assert [(i.patient_ref, i.priority) for i in items] == [
        ("PT-P0", "P0"), ("PT-P1", "P1"), ("PT-P2", "P2")]


def test_direct_contract_routes_to_human_review_never_auto(tmp_path):
    athena = _athena()
    athena._patients = {
        "PT-DC-0001": _patient("PT-DC-0001", insurance=None),   # on the list
        "PT-FREE": _patient("PT-FREE", insurance=None),
    }
    items = {i.patient_ref: i for i in
             detect(athena, list_source=_list_source(tmp_path), now=NOW)}
    dc = items["PT-DC-0001"]
    assert dc.direct_contract["is_direct_contract"] is True
    assert dc.direct_contract["routed_to_review"] is True
    assert dc.sequence_state == STATE_HUMAN_REVIEW
    assert items["PT-FREE"].sequence_state == "open"


def test_unavailable_list_fails_closed_to_review(tmp_path):
    athena = _athena()
    athena._patients = {"PT-X": _patient("PT-X", insurance=None)}
    missing = PracticeListSource(tmp_path / "missing.json")
    items = detect(athena, list_source=missing, now=NOW)
    assert items[0].sequence_state == STATE_HUMAN_REVIEW
    assert items[0].direct_contract["status"] == "unknown_fail_closed"


def test_seeded_world_emits_designed_eligibility_mix(tmp_path):
    athena = _athena()
    athena.seed_world(n_patients=200)
    items = detect(athena, list_source=_list_source(tmp_path), now=NOW)
    triggers = [i.trigger for i in items]
    assert triggers.count(TRIGGER_INACTIVE) >= 5      # ~10% of insured
    assert triggers.count(TRIGGER_TIMEOUT) >= 2       # ~5% of insured
    assert triggers.count(TRIGGER_NO_CARD) >= 5
    assert triggers.count(TRIGGER_EXPIRED) >= 5


# ------------------------------------------------------------------ #
# F9b — chassis rails
# ------------------------------------------------------------------ #
def _one_open_item(tmp_path, ref="PT-INACT"):
    athena = _athena()
    athena._patients = {ref: _patient(ref, outcome=ELIGIBILITY_INACTIVE)}
    items = detect(athena, list_source=_list_source(tmp_path), now=NOW)
    q = ChaseQueue(QueueType.CARD_CHASE, list_source=_list_source(tmp_path))
    return athena, q, q.enqueue(items[0], now=NOW)


def test_send_without_approval_is_refused(tmp_path):
    _, q, item = _one_open_item(tmp_path)
    with pytest.raises(SequenceError, match="approval"):
        q.send(item.chase_id, template_id="card_request.v0", now=NOW,
               sent_by="grace-demo")


def test_approved_send_logs_attempt_and_contact(tmp_path):
    _, q, item = _one_open_item(tmp_path)
    q.approve(item.chase_id, staff_id="grace-demo", now=NOW)
    body = q.send(item.chase_id, template_id="card_request.v0", now=NOW,
                  sent_by="grace-demo")
    assert item.sequence_state == "sent"
    assert item.attempts == [{"channel": "email",
                              "template_id": "card_request.v0",
                              "sent_at": NOW.isoformat(),
                              "approved_by": "grace-demo"}]
    assert "https://cardcapture.example.test/submit/" in body


def test_message_bodies_never_carry_insurance_values(tmp_path):
    """No member IDs, no card numbers, no long digit runs — in any template."""
    _, q, item = _one_open_item(tmp_path)
    for template_id in q.config.templates:
        body = q.render_message(template_id, item, display_name="Synthetic P")
        low = body.lower()
        for frag in ("member", "policy", "card number", "…1111"):
            assert frag not in low, f"{template_id} leaks {frag!r}"
        # outside the opaque submission link, no digit runs (IDs) may appear
        without_link = re.sub(r"https://\S+", "", body)
        assert not re.search(r"\d{5,}", without_link), \
            f"{template_id} has a digit run"


def test_reminder_cap_from_config(tmp_path):
    _, q, item = _one_open_item(tmp_path)
    q.approve(item.chase_id, staff_id="s1", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")
    for n in range(q.config.max_reminders):
        q.send(item.chase_id, template_id="card_reminder.v0",
               now=NOW + timedelta(days=3 * (n + 1)), sent_by="s1")
    with pytest.raises(SequenceError, match="max reminders"):
        q.send(item.chase_id, template_id="card_reminder.v0",
               now=NOW + timedelta(days=30), sent_by="s1")


def test_review_release_requires_reason(tmp_path):
    athena = _athena()
    athena._patients = {"PT-DC-0001": _patient("PT-DC-0001", insurance=None)}
    items = detect(athena, list_source=_list_source(tmp_path), now=NOW)
    q = ChaseQueue(QueueType.CARD_CHASE, list_source=_list_source(tmp_path))
    item = q.enqueue(items[0], now=NOW)
    with pytest.raises(SequenceError, match="reason"):
        q.review_release(item.chase_id, staff_id="s1", now=NOW, reason="  ")


# ------------------------------------------------------------------ #
# F9c — completion loop + idempotency + metrics
# ------------------------------------------------------------------ #
def test_end_to_end_inactive_271_to_closed_with_metrics(tmp_path):
    athena, q, item = _one_open_item(tmp_path)
    ref = item.patient_ref

    q.approve(item.chase_id, staff_id="grace-demo", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0",
           now=NOW + timedelta(hours=1), sent_by="grace-demo")

    event = simulate_patient_submission(ref, now=NOW + timedelta(hours=20))
    result = on_card_verified(q, athena, event, now=NOW + timedelta(hours=20))

    assert result["written"] is True and result["duplicate"] is False
    assert result["eligibility"] == "ACTIVE"          # re-check confirms
    assert result["closed_items"] == [item.chase_id]
    assert item.sequence_state == "verified_closed"
    assert item.resolution["eligibility_confirmed"] is True
    assert item.resolution["card_document_ref"] == event.card_document_ref

    docs = athena.get_patient(ref).documents
    assert any(d.doc_type == "ADMIN_INSURANCECARD" for d in docs)

    m = summarize(q.items.values(), q.staff_actions)
    assert m["completed"] == 1 and m["cards_outstanding"] == 0
    assert m["completion_rate"] == 1.0
    assert m["median_hours_to_card"] == pytest.approx(20.0)  # detect → closed
    assert m["staff_touches"] == 2                    # approve + send


def test_replayed_card_event_is_idempotent(tmp_path):
    athena, q, item = _one_open_item(tmp_path)
    event = simulate_patient_submission(item.patient_ref, now=NOW)
    first = on_card_verified(q, athena, event, now=NOW)
    replay = on_card_verified(q, athena, event, now=NOW + timedelta(hours=1))
    assert first["written"] is True
    assert replay["duplicate"] is True and replay["written"] is False
    assert replay["closed_items"] == []               # nothing closes twice


def test_review_bucket_items_do_not_autoclose(tmp_path):
    athena = _athena()
    athena._patients = {"PT-DC-0001": _patient("PT-DC-0001", insurance=None)}
    items = detect(athena, list_source=_list_source(tmp_path), now=NOW)
    q = ChaseQueue(QueueType.CARD_CHASE, list_source=_list_source(tmp_path))
    item = q.enqueue(items[0], now=NOW)
    event = simulate_patient_submission("PT-DC-0001", now=NOW)
    result = on_card_verified(q, athena, event, now=NOW)
    assert result["closed_items"] == []
    assert item.sequence_state == STATE_HUMAN_REVIEW   # parked for staff


# ------------------------------------------------------------------ #
# F5 gate semantics on the shared chassis (full F5 lands separately)
# ------------------------------------------------------------------ #
def test_direct_contract_patient_can_never_enter_self_pay_queue(tmp_path):
    from cosmaneura_billing.chase.detector import ChaseItem
    q = ChaseQueue(QueueType.SELF_PAY, list_source=_list_source(tmp_path))
    item = ChaseItem(chase_id="CH-PT-DC-0001-selfpay", patient_ref="PT-DC-0001",
                     trigger="self_pay_balance", detected_at=NOW.isoformat(),
                     appointment_at=None, priority="P2")
    with pytest.raises(SequenceError, match="direct-contract"):
        q.enqueue(item, now=NOW)
    assert q.items == {}                               # nothing entered
