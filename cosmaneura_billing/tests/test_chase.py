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
                              "approved_by": "grace-demo",
                              "sent_by": "grace-demo",
                              "staff_approved": True}]
    assert "https://cardcapture.example.test/submit/" in body
    # personalized from the name the detector recorded on the item
    assert body.startswith("Hi Synthetic PT-INACT,")


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


# ------------------------------------------------------------------ #
# auto-continue mode: the system runs the follow-ups (config, §8 Q3)
# ------------------------------------------------------------------ #
def test_tick_is_noop_in_default_staff_triggered_mode(tmp_path):
    from cosmaneura_billing.chase import tick
    _, q, item = _one_open_item(tmp_path)
    q.approve(item.chase_id, staff_id="s1", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")
    result = tick(q, now=NOW + timedelta(days=30))
    assert result == {"mode": "staff_triggered", "reminded": [],
                      "escalated": [], "failed": []}
    assert len(item.attempts) == 1                     # nothing auto-fired


def test_auto_continue_chases_until_compliance_or_escalation(tmp_path):
    """Enroll once → the technology facilitates continued outreach: cadenced
    reminders fire with no staff action, then the item escalates back to
    staff when the limit is reached without a submission."""
    from cosmaneura_billing.chase import tick
    _, q, item = _one_open_item(tmp_path)
    q.config.outreach_mode = "auto_continue"
    q.approve(item.chase_id, staff_id="grace-demo", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW,
           sent_by="grace-demo")

    assert tick(q, now=NOW + timedelta(days=1))["reminded"] == []   # not due yet
    r1 = tick(q, now=NOW + timedelta(days=3))
    assert r1["reminded"] == [item.chase_id]
    auto = item.attempts[-1]
    # a machine send is never booked as a human approval, but stays traceable
    # to the staff member who enrolled the patient
    assert auto["staff_approved"] is False
    assert auto["approved_by"] is None
    assert auto["sent_by"] == "auto:cadence(enrolled_by=grace-demo)"
    r2 = tick(q, now=NOW + timedelta(days=6))
    assert r2["reminded"] == [item.chase_id]
    r3 = tick(q, now=NOW + timedelta(days=9))
    assert r3["escalated"] == [item.chase_id]
    assert item.sequence_state == "escalated_to_staff"
    assert item.resolution["reason"].startswith("reminder limit reached")


def test_auto_continue_stops_the_moment_a_card_lands(tmp_path):
    from cosmaneura_billing.chase import tick
    athena, q, item = _one_open_item(tmp_path)
    q.config.outreach_mode = "auto_continue"
    q.approve(item.chase_id, staff_id="s1", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")
    tick(q, now=NOW + timedelta(days=3))               # one auto reminder
    event = simulate_patient_submission(item.patient_ref,
                                        now=NOW + timedelta(days=4))
    on_card_verified(q, athena, event, now=NOW + timedelta(days=4))
    assert item.sequence_state == "verified_closed"
    later = tick(q, now=NOW + timedelta(days=30))
    assert later == {"mode": "auto_continue", "reminded": [],
                     "escalated": [], "failed": []}


def test_auto_continue_never_touches_unapproved_or_review_items(tmp_path):
    from cosmaneura_billing.chase import tick
    athena = _athena()
    athena._patients = {
        "PT-OPEN": _patient("PT-OPEN", outcome=ELIGIBILITY_INACTIVE),
        "PT-DC-0001": _patient("PT-DC-0001", insurance=None),
    }
    items = detect(athena, list_source=_list_source(tmp_path), now=NOW)
    q = ChaseQueue(QueueType.CARD_CHASE, list_source=_list_source(tmp_path))
    q.config.outreach_mode = "auto_continue"
    for it in items:
        q.enqueue(it, now=NOW)
    result = tick(q, now=NOW + timedelta(days=30))
    assert result["reminded"] == [] and result["escalated"] == []
    states = {i.patient_ref: i.sequence_state for i in q.items.values()}
    assert states["PT-OPEN"] == "open"                 # never staff-approved
    assert states["PT-DC-0001"] == "human_review"      # gate bucket untouched


# ------------------------------------------------------------------ #
# regressions from the Aug 12 adversarial review
# ------------------------------------------------------------------ #
def test_redetection_never_clobbers_an_inflight_or_terminal_item(tmp_path):
    """The detector emits deterministic chase_ids, so a second pass re-yields
    an item already in flight. Intake must be first-write-wins: staff
    decisions, the contact log, and the reminder cap all survive."""
    athena, q, item = _one_open_item(tmp_path)
    src = _list_source(tmp_path)
    q.approve(item.chase_id, staff_id="s1", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")

    later = NOW + timedelta(days=1)
    for again in detect(athena, list_source=src, now=later):
        q.enqueue(again, now=later)

    kept = q.items[item.chase_id]
    assert kept is item                              # same object, not replaced
    assert kept.sequence_state == "sent"             # not reset to open
    assert len(kept.attempts) == 1                   # contact log intact

    q.escalate(item.chase_id, staff_id="s1", now=later, reason="no response")
    for again in detect(athena, list_source=src, now=later + timedelta(days=1)):
        q.enqueue(again, now=later + timedelta(days=1))
    assert q.items[item.chase_id].sequence_state == "escalated_to_staff"
    with pytest.raises(SequenceError, match="terminal"):
        q.approve(item.chase_id, staff_id="s1", now=later + timedelta(days=2))


def test_reminder_cap_counts_every_contact_not_just_reminder_templates(tmp_path):
    """The bound on patient contact must not depend on template naming."""
    _, q, item = _one_open_item(tmp_path)
    q.approve(item.chase_id, staff_id="s1", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")
    for n in range(q.config.max_reminders):
        q.send(item.chase_id, template_id="card_request.v0",
               now=NOW + timedelta(days=n + 1), sent_by="s1")
    with pytest.raises(SequenceError, match="max reminders"):
        q.send(item.chase_id, template_id="card_request.v0",
               now=NOW + timedelta(days=9), sent_by="s1")
    assert len(item.attempts) == 1 + q.config.max_reminders


def test_tick_uses_a_configured_channel_and_isolates_failures(tmp_path):
    """A practice that texts instead of emailing must still get its cadence:
    tick sends on a configured channel, and one bad item cannot abort the pass."""
    from cosmaneura_billing.chase import tick
    athena = _athena()
    athena._patients = {
        "PT-A": _patient("PT-A", outcome=ELIGIBILITY_INACTIVE),
        "PT-B": _patient("PT-B", insurance=None, appt_days=2),
    }
    src = _list_source(tmp_path)
    q = ChaseQueue(QueueType.CARD_CHASE, list_source=src)
    q.config.channels = ("sms",)
    q.config.outreach_mode = "auto_continue"
    items = [q.enqueue(i, now=NOW) for i in detect(athena, list_source=src, now=NOW)]
    for it in items:
        q.approve(it.chase_id, staff_id="s1", now=NOW)
        q.send(it.chase_id, template_id="card_request.v0", channel="sms",
               now=NOW, sent_by="s1")

    result = tick(q, now=NOW + timedelta(days=3))
    assert set(result["reminded"]) == {i.chase_id for i in items}
    assert result["failed"] == []
    assert all(a["channel"] == "sms" for i in items for a in i.attempts)


def test_machine_sends_never_count_as_staff_touches(tmp_path):
    """Auto-continue exists to cut staff minutes — machine work must not be
    booked as human work in the metric the Aug 13 baseline is judged against."""
    from cosmaneura_billing.chase import tick
    _, q, item = _one_open_item(tmp_path)
    q.config.outreach_mode = "auto_continue"
    q.approve(item.chase_id, staff_id="s1", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")
    tick(q, now=NOW + timedelta(days=3))
    tick(q, now=NOW + timedelta(days=6))

    m = summarize(q.items.values(), q.staff_actions, q.machine_actions)
    assert m["staff_touches"] == 2                  # approve + the human send
    assert m["machine_touches"] == 2                # the two auto reminders


def test_completion_rate_never_exceeds_100_percent(tmp_path):
    """A patient who submits before any outreach closes the item but was never
    chased — counting them in the chase's completion rate produced 200%."""
    athena = _athena()
    athena._patients = {
        "PT-SELF1": _patient("PT-SELF1", insurance=None, appt_days=3),
        "PT-SELF2": _patient("PT-SELF2", insurance=None, appt_days=4),
        "PT-CHASED": _patient("PT-CHASED", outcome=ELIGIBILITY_INACTIVE),
    }
    src = _list_source(tmp_path)
    q = ChaseQueue(QueueType.CARD_CHASE, list_source=src)
    items = {i.patient_ref: q.enqueue(i, now=NOW)
             for i in detect(athena, list_source=src, now=NOW)}

    chased = items["PT-CHASED"]
    q.approve(chased.chase_id, staff_id="s1", now=NOW)
    q.send(chased.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")
    for ref in ("PT-SELF1", "PT-SELF2"):            # no outreach at all
        on_card_verified(q, athena,
                         simulate_patient_submission(ref, now=NOW),
                         now=NOW + timedelta(hours=2))

    m = summarize(q.items.values(), q.staff_actions, q.machine_actions)
    assert m["completed"] == 2
    assert m["closed_without_outreach"] == 2
    assert 0.0 <= m["completion_rate"] <= 1.0
    assert m["completion_rate"] == 0.0              # the chased one is still open


def test_naive_now_and_offset_appointments_are_handled(tmp_path):
    """A tz-naive `now` must not crash the pass, and an appointment written in
    a non-UTC offset must not be misread as past by string comparison."""
    athena = _athena()
    p = PatientRecord(patient_ref="PT-TZ", display_name="Synthetic TZ")
    p.appointments.append(Appointment(
        appointment_id="AP-TZ", patient_ref="PT-TZ",
        # 18:00-05:00 == 23:00Z: later than NOW in time, earlier as a string
        scheduled_at="2026-08-12T18:00:00-05:00"))
    athena._patients = {"PT-TZ": p}

    items = detect(athena, list_source=_list_source(tmp_path),
                   now=NOW.replace(tzinfo=None))    # naive: must not raise
    assert [i.trigger for i in items] == [TRIGGER_NO_CARD]
    assert items[0].priority == "P0"


def test_unreachable_is_a_recordable_outcome(tmp_path):
    _, q, item = _one_open_item(tmp_path)
    q.approve(item.chase_id, staff_id="s1", now=NOW)
    q.send(item.chase_id, template_id="card_request.v0", now=NOW, sent_by="s1")
    q.mark_unreachable(item.chase_id, staff_id="s1", now=NOW + timedelta(days=2),
                       reason="phone disconnected, email bouncing")
    assert item.sequence_state == "unreachable"
    assert item.resolution["unreachable"] is True
    with pytest.raises(SequenceError):              # terminal
        q.approve(item.chase_id, staff_id="s1", now=NOW + timedelta(days=3))
