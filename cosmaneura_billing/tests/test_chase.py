"""Insurance-data chase (F9) — detector, chassis, worklist, metrics.

Acceptance criteria from the requirements synthesis §3 F9:

  * each detector trigger fires on seeded states and stays silent on clean ones
  * a direct-contract patient can never enter the auto-sequence (integration with
    F3 — routed to review instead)
  * no outbound message body contains an insurance value or card field
  * end-to-end: inactive eligibility -> queue item -> approved outreach ->
    simulated submission -> verification -> auto-close -> metrics emitted
"""
import pytest

from cosmaneura_billing.chase import (
    ApprovalMode, Channel, ChasePolicy, CoverageSnapshot, MessageContainsPHI,
    NotApproved, SequenceRunner, State, Step, Template, Trigger, Worklist,
    detect, detect_all, priority_for, summarize,
)
from cosmaneura_billing.chase.metrics import baseline_comparison
from cosmaneura_billing.chase.policy import assert_no_phi
from cosmaneura_billing.direct_contract import DirectContractList

TODAY = "2026-08-17"
CLOCK = lambda: "2026-08-17T09:00:00+00:00"  # noqa: E731
ROSTER = DirectContractList(refs=["PT-DIRECT"], version="test")


def _snap(ref="PT-1", **kw) -> CoverageSnapshot:
    base = dict(patient_ref=ref, email="p@example.invalid", portal_enrolled=True,
                phone="+15550100", sms_consent=True, voice_consent=True,
                has_insurance_package=True, card_image_on_file=True)
    base.update(kw)
    return CoverageSnapshot(**base)


def _policy(**kw) -> ChasePolicy:
    p = ChasePolicy(practice_name="MyCatholicDoctor",
                    upload_url="https://mcd.invalid/insurance", **kw)
    return p.approve(by="grace.targonski", at=CLOCK())


def _runner(policy=None, transport=None) -> SequenceRunner:
    sent: list[dict] = []

    def record(*, channel, recipient, subject, body, operation_id=""):
        sent.append({"channel": channel, "recipient": recipient,
                     "operation_id": operation_id,
                     "subject": subject, "body": body})
        return "sent"

    runner = SequenceRunner(policy=policy or _policy(),
                            transport=transport or record, now=CLOCK)
    runner.sent = sent           # type: ignore[attr-defined]
    return runner


def _detect(snap, **kw):
    return detect(snap, today=TODAY, now=CLOCK, roster=ROSTER, **kw)


# --------------------------------------------------------------------------- #
# F9a detector: each trigger fires, and stays silent on a clean record
# --------------------------------------------------------------------------- #
def test_clean_record_produces_no_row():
    assert _detect(_snap(appointment_at="2026-08-20")) is None


@pytest.mark.parametrize("kw,expected", [
    ({"has_insurance_package": False}, Trigger.NO_CARD_ON_FILE),
    ({"card_image_on_file": False}, Trigger.NO_CARD_ON_FILE),
    ({"coverage_end_date": "2026-07-31"}, Trigger.EXPIRED),
    ({"eligibility_status": "INACTIVE"}, Trigger.ELIGIBILITY_INACTIVE),
    ({"eligibility_status": "TIMEOUT"}, Trigger.ELIGIBILITY_TIMEOUT),
    ({"plan_year_rollover": True}, Trigger.PLAN_YEAR_ROLLOVER),
    ({"staff_flag": "patient says new insurance"}, Trigger.MANUAL),
    ({"denial_reason_code": "CO-27"}, Trigger.DENIAL_COVERAGE),
])
def test_each_trigger_fires(kw, expected):
    item = _detect(_snap(**kw))
    assert item is not None and item.trigger is expected


def test_a_package_without_a_card_image_is_not_done():
    """Aug 13: the card image has to be in the right Athena encounter, so a
    package on file with no image is still an open row."""
    item = _detect(_snap(card_image_on_file=False))
    assert item is not None and item.trigger is Trigger.NO_CARD_ON_FILE


def test_payer_facts_outrank_our_own_observation():
    """Chasing a new card for a plan the payer says is inactive asks the patient
    the wrong question, so the more specific trigger wins."""
    item = _detect(_snap(has_insurance_package=False, eligibility_status="INACTIVE"))
    assert item.trigger is Trigger.ELIGIBILITY_INACTIVE


def test_future_coverage_end_date_is_not_expired():
    assert _detect(_snap(coverage_end_date="2027-01-01")) is None


# --------------------------------------------------------------------------- #
# Priority
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("appt,expected", [
    ("2026-08-16", 90),   # already happened: claim is held
    ("2026-08-17", 100),  # today: fixable at check-in
    ("2026-08-18", 80),
    ("2026-08-22", 60),
    ("2026-09-30", 30),
    ("", 40),             # no appointment: steady backlog
])
def test_priority_tracks_appointment_proximity(appt, expected):
    assert priority_for(_snap(appointment_at=appt), TODAY) == expected


def test_batch_is_ordered_by_priority():
    items = detect_all(
        [_snap("PT-A", has_insurance_package=False, appointment_at="2026-09-30"),
         _snap("PT-B", has_insurance_package=False, appointment_at="2026-08-17")],
        today=TODAY, now=CLOCK, roster=ROSTER)
    assert [i.patient_ref for i in items] == ["PT-B", "PT-A"]


# --------------------------------------------------------------------------- #
# F3 integration: a direct-contract patient never auto-sequences
# --------------------------------------------------------------------------- #
def test_direct_contract_patient_goes_to_review_not_the_sequence():
    item = _detect(_snap("PT-DIRECT", has_insurance_package=False))
    assert item.state is State.NEEDS_REVIEW
    assert "direct-contract list" in item.review_reason
    assert item.direct_contract["is_direct_contract"] is True


def test_direct_contract_patient_cannot_be_advanced():
    item = _detect(_snap("PT-DIRECT", has_insurance_package=False))
    runner = _runner()
    result = runner.advance(item, _snap("PT-DIRECT", has_insurance_package=False))
    assert result["action"] == "awaiting_review"
    assert runner.sent == [], "no message may go to a direct-contract patient"
    assert not runner.is_actionable(item)


def test_unknown_direct_contract_status_also_blocks(tmp_path):
    item = detect(_snap("PT-9", has_insurance_package=False), today=TODAY, now=CLOCK,
                  roster=DirectContractList(path=tmp_path / "missing.csv"))
    assert item.state is State.NEEDS_REVIEW
    assert "could not be determined" in item.review_reason


def test_a_person_can_release_a_reviewed_item():
    item = _detect(_snap("PT-DIRECT", has_insurance_package=False))
    runner = _runner()
    runner.release(item, by="cindy", reason="confirmed we do bill this plan")
    assert item.state is State.READY and item.assignee == "cindy"
    assert runner.advance(item, _snap("PT-DIRECT"))["action"] == "sent"


def test_release_requires_a_named_person():
    item = _detect(_snap("PT-DIRECT", has_insurance_package=False))
    with pytest.raises(ValueError):
        _runner().release(item, by="  ")


# --------------------------------------------------------------------------- #
# Approval: nothing sends without a human behind it
# --------------------------------------------------------------------------- #
def test_unapproved_policy_sends_nothing():
    runner = _runner(policy=ChasePolicy())      # never approved
    item = _detect(_snap(has_insurance_package=False))
    with pytest.raises(NotApproved) as e:
        runner.advance(item, _snap(has_insurance_package=False))
    assert "approved by anyone" in str(e.value)
    assert runner.sent == []


def test_policy_approval_requires_a_name():
    with pytest.raises(ValueError):
        ChasePolicy().approve(by="", at=CLOCK())


def test_per_policy_mode_sends_without_per_message_clicks():
    """The automation the request asks for: approve once, the system works the
    list. At 300-500 held claims a month, per-message approval is a queue nobody
    drains."""
    runner = _runner(policy=_policy(approval_mode=ApprovalMode.PER_POLICY))
    item = _detect(_snap(has_insurance_package=False))
    assert runner.advance(item, _snap(has_insurance_package=False))["action"] == "sent"


def test_per_message_mode_holds_each_touch_for_release():
    runner = _runner(policy=_policy(approval_mode=ApprovalMode.PER_MESSAGE))
    item = _detect(_snap(has_insurance_package=False))
    snap = _snap(has_insurance_package=False)
    assert runner.advance(item, snap)["action"] == "awaiting_release"
    assert runner.sent == []
    assert runner.advance(item, snap, released_by="maggie")["action"] == "sent"


# --------------------------------------------------------------------------- #
# PHI guard on outbound bodies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    "Your member ID is 12345",
    "We have your policy number on file",
    "Confirm your group # please",
    "Your subscriber id looks wrong",
])
def test_bodies_mentioning_insurance_data_are_rejected(body):
    with pytest.raises(MessageContainsPHI):
        assert_no_phi(body)


def test_a_template_carrying_phi_fails_at_configuration_time():
    """Caught when the policy is built, not when a patient is messaged."""
    bad = Template("email.bad", Channel.EMAIL, "hi",
                   "Please confirm your member id {upload_url}")
    with pytest.raises(MessageContainsPHI):
        ChasePolicy(templates=(bad,))


def test_no_sent_message_contains_insurance_data():
    runner = _runner()
    item = _detect(_snap(has_insurance_package=False))
    snap = _snap(has_insurance_package=False)
    for _ in range(4):
        runner.advance(item, snap)
    assert runner.sent
    for message in runner.sent:
        assert_no_phi(message["body"])
        assert_no_phi(message["subject"])
        assert "insurance" in message["body"].lower(), "the ask should still be clear"


def test_transport_receives_route_not_internal_patient_reference():
    runner = _runner()
    snap = _snap(
        "INTERNAL-PATIENT-REF", has_insurance_package=False,
        email="synthetic@example.invalid",
    )

    result = runner.advance(item=_detect(snap), snap=snap, delivery_id="delivery-1")

    assert result["action"] == "sent"
    assert runner.sent[0]["recipient"] == "synthetic@example.invalid"
    assert runner.sent[0]["operation_id"] == "delivery-1"
    assert "INTERNAL-PATIENT-REF" not in str(runner.sent[0])


def test_templates_can_only_substitute_three_safe_values():
    """There is no placeholder for a member id, so a template cannot carry one."""
    rendered = ChasePolicy().template("email.first").render(
        practice="MCD", upload_url="https://x.invalid")
    assert rendered["subject"] == "MCD: we need your insurance card"
    assert rendered["body"] == (
        "Hi there,\n\nWe're missing current insurance information for your "
        "upcoming visit with MCD. You can add it securely here:\n"
        "https://x.invalid\n\nIt takes about a minute, and it means we can bill "
        "your insurer instead of sending you a bill.\n\nThank you."
    )


# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #
def test_sms_without_consent_is_not_sent():
    policy = _policy(steps=(Step(Channel.SMS, "sms.first", 0),))
    runner = _runner(policy=policy)
    snap = _snap(has_insurance_package=False, sms_consent=False)
    item = _detect(snap)
    result = runner.advance(item, snap)
    assert result["action"] == "awaiting_review"
    assert runner.sent == []


def test_no_usable_step_is_reported_as_a_policy_problem_not_a_patient_one():
    """Zero touches reported as an escalation would read to staff as 'the patient
    ignored us' about messages that never went out. This is a policy or consent
    gap, and the reason has to say so."""
    policy = _policy(steps=(Step(Channel.SMS, "sms.first", 0),))
    snap = _snap(has_insurance_package=False, sms_consent=False)
    item = _detect(snap)
    result = _runner(policy=policy).advance(item, snap)
    assert result["touches"] == 0
    assert item.state is State.NEEDS_REVIEW
    assert "no step this patient can receive" in item.review_reason
    assert "email" in item.review_reason, "say what they ARE reachable on"
    assert item.is_open, "a policy gap must not close the row"


def test_consent_withdrawn_between_touches_stops_the_next_message():
    """Consent is re-checked at send time, so withdrawal stops the next touch
    rather than the next sequence."""
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),
                            Step(Channel.SMS, "sms.first", 1)))
    runner = _runner(policy=policy)
    snap = _snap(has_insurance_package=False)
    item = _detect(snap)
    assert runner.advance(item, snap)["action"] == "sent"
    withdrawn = _snap(has_insurance_package=False, sms_consent=False)
    assert runner.advance(item, withdrawn)["action"] != "sent"
    assert len(runner.sent) == 1


def test_a_patient_with_no_reachable_channel_is_marked_unreachable():
    snap = _snap(has_insurance_package=False, email="", phone="",
                 portal_enrolled=False, sms_consent=False, voice_consent=False)
    item = _detect(snap)
    result = _runner().advance(item, snap)
    assert result["action"] == "unreachable"
    assert item.state is State.UNREACHABLE and not item.is_open


def test_cadence_skips_steps_the_patient_cannot_receive():
    """An unreachable step must be dropped, not counted as a touch — otherwise a
    patient with no mobile 'gets' an SMS and the sequence exhausts unseen."""
    policy = _policy()
    snap = _snap(has_insurance_package=False, phone="", sms_consent=False,
                 voice_consent=False)
    steps = policy.steps_for(snap.reachable_on())
    assert all(s.channel is not Channel.SMS for s in steps)
    assert all(s.channel is not Channel.PHONE for s in steps)


# --------------------------------------------------------------------------- #
# Phone is a staff task, not a robocall
# --------------------------------------------------------------------------- #
def test_phone_step_creates_a_task_and_sends_nothing():
    policy = _policy(steps=(Step(Channel.PHONE, "phone.call", 0),))
    runner = _runner(policy=policy)
    snap = _snap(has_insurance_package=False)
    item = _detect(snap)
    result = runner.advance(item, snap)
    assert result["action"] == "staff_task"
    assert "Log the outcome" in result["script"]
    assert runner.sent == [], "the system must not place calls itself"


def test_an_open_call_task_is_not_reissued_every_pass():
    """A task records no attempt until its outcome is logged, so without this the
    same call lands back on the worklist tomorrow and the day after."""
    policy = _policy(steps=(Step(Channel.PHONE, "phone.call", 0),))
    runner = _runner(policy=policy)
    snap = _snap(has_insurance_package=False)
    item = _detect(snap)

    assert runner.advance(item, snap)["action"] == "staff_task"
    for _ in range(3):
        again = runner.advance(item, snap)
        assert again["action"] == "awaiting_staff_task"
    assert item.pending_task is not None


def test_logging_the_call_outcome_lets_the_cadence_move_on():
    policy = _policy(steps=(Step(Channel.PHONE, "phone.call", 0),
                            Step(Channel.EMAIL, "email.reminder", 1)))
    runner = _runner(policy=policy)
    snap = _snap(has_insurance_package=False)
    item = _detect(snap)

    runner.advance(item, snap)
    runner.log_staff_attempt(item, channel=Channel.PHONE, template_id="phone.call",
                             actor="maggie", outcome="no_answer", minutes_spent=2.0)
    assert item.pending_task is None
    assert runner.advance(item, snap)["action"] == "sent"


def test_staff_log_their_call_into_the_same_contact_history():
    runner = _runner()
    item = _detect(_snap(has_insurance_package=False))
    runner.log_staff_attempt(item, channel=Channel.PHONE, template_id="phone.call",
                             actor="maggie", outcome="left_message",
                             minutes_spent=4.5, note="mailbox full on 1st try")
    assert item.touches == 1
    assert item.staff_minutes == 4.5
    assert item.attempts[0].actor == "maggie"


def test_a_staff_attempt_must_say_who_made_it():
    item = _detect(_snap(has_insurance_package=False))
    with pytest.raises(ValueError):
        _runner().log_staff_attempt(item, channel=Channel.PHONE,
                                    template_id="phone.call", actor="",
                                    outcome="no_answer")


# --------------------------------------------------------------------------- #
# Completion loop (F9c): verified, not submitted
# --------------------------------------------------------------------------- #
def test_a_submission_does_not_close_the_row():
    runner = _runner()
    item = _detect(_snap(has_insurance_package=False))
    runner.mark_submitted(item, note="patient uploaded a photo")
    assert item.state is State.SUBMITTED
    assert item.is_open, "a photo is not a verified card on file"


def test_only_verification_closes_the_row():
    runner = _runner()
    item = _detect(_snap(has_insurance_package=False))
    runner.mark_submitted(item)
    runner.close_verified(item, verified_by="cindy", athena_write_id="ath-991",
                          eligibility_status="ACTIVE")
    assert item.state is State.VERIFIED_CLOSED and not item.is_open
    assert item.resolution["verified_by"] == "cindy"
    assert item.resolution["athena_write_id"] == "ath-991"


def test_closing_requires_the_verifier_identity():
    item = _detect(_snap(has_insurance_package=False))
    with pytest.raises(ValueError):
        _runner().close_verified(item, verified_by="")


def test_exhausted_cadence_escalates_rather_than_going_quiet():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),))
    runner = _runner(policy=policy)
    snap = _snap(has_insurance_package=False)
    item = _detect(snap)
    runner.advance(item, snap)
    result = runner.advance(item, snap)
    assert result["action"] == "escalated"
    assert item.state is State.ESCALATED
    assert "no card received" in item.resolution["reason"]


def test_a_person_can_suppress_with_a_reason():
    item = _detect(_snap(has_insurance_package=False))
    runner = _runner()
    runner.suppress(item, by="cindy", reason="patient is self-pay by choice")
    assert item.state is State.RESOLVED_OTHER and not item.is_open
    with pytest.raises(ValueError):
        _runner().suppress(_detect(_snap("PT-2", has_insurance_package=False)),
                           by="cindy", reason="")


# --------------------------------------------------------------------------- #
# The master worklist
# --------------------------------------------------------------------------- #
def test_worklist_groups_the_three_buckets_staff_already_use():
    wl = Worklist()
    wl.extend(detect_all([
        _snap("PT-A", has_insurance_package=False),
        _snap("PT-B", coverage_end_date="2026-07-31"),
        _snap("PT-C", eligibility_status="INACTIVE"),
    ], today=TODAY, now=CLOCK, roster=ROSTER))
    counts = wl.counts()
    assert counts["by_bucket"] == {"missing": 1, "expired": 1, "eligibility": 1}
    assert counts["open"] == 3
    assert [i.patient_ref for i in wl.rows(bucket="expired")] == ["PT-B"]


def test_rerunning_detection_does_not_duplicate_a_row():
    """Daily detection over the same unresolved patient must not inflate the
    backlog or the metrics."""
    wl = Worklist()
    for _ in range(3):
        wl.extend(detect_all([_snap("PT-A", has_insurance_package=False)],
                             today=TODAY, now=CLOCK, roster=ROSTER))
    assert wl.counts()["open"] == 1


def test_rerunning_detection_preserves_contact_history():
    wl = Worklist()
    snap = _snap("PT-A", has_insurance_package=False)
    item = wl.upsert(_detect(snap))
    _runner().advance(item, snap)
    assert item.touches == 1
    wl.upsert(_detect(snap))
    assert wl.get(item.chase_id).touches == 1, "re-detection must not reset the log"


def test_closed_rows_are_not_reopened_by_detection():
    wl = Worklist()
    snap = _snap("PT-A", has_insurance_package=False)
    item = wl.upsert(_detect(snap))
    _runner().close_verified(item, verified_by="cindy")
    wl.upsert(_detect(snap))
    assert wl.counts()["open"] == 0


def test_worklist_surfaces_what_is_stuck_on_a_person():
    wl = Worklist()
    wl.extend(detect_all([
        _snap("PT-DIRECT", has_insurance_package=False),
        _snap("PT-A", has_insurance_package=False),
    ], today=TODAY, now=CLOCK, roster=ROSTER))
    assert wl.counts()["needs_person"] == 1
    assert [i.patient_ref for i in wl.rows(needs_person=True)] == ["PT-DIRECT"]


def test_assignment_and_filtering_by_assignee():
    wl = Worklist()
    item = wl.upsert(_detect(_snap("PT-A", has_insurance_package=False)))
    wl.assign(item.chase_id, to="maggie")
    assert [i.patient_ref for i in wl.rows(assignee="maggie")] == ["PT-A"]
    assert wl.counts()["unassigned"] == 0


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_metrics_report_outstanding_and_completion():
    wl = Worklist()
    snaps = [_snap(f"PT-{i}", has_insurance_package=False) for i in range(3)]
    items = wl.extend(detect_all(snaps, today=TODAY, now=CLOCK, roster=ROSTER))
    runner = _runner()
    runner.log_staff_attempt(items[0], channel=Channel.PHONE,
                             template_id="phone.call", actor="cindy",
                             outcome="reached", minutes_spent=6.0)
    runner.close_verified(items[0], verified_by="cindy")

    m = summarize(wl.items.values())
    assert m["chases"] == 3
    assert m["outstanding"] == 2
    assert m["verified_closed"] == 1
    assert m["completion_rate"] == 1.0     # 1 of 1 resolved ended verified
    assert m["staff_minutes_per_card"] == 6.0
    assert m["median_touches_to_card"] == 1


def test_automated_share_distinguishes_system_from_staff_work():
    snap = _snap(has_insurance_package=False)
    item = _detect(snap)
    runner = _runner()
    runner.advance(item, snap)                       # system email
    runner.log_staff_attempt(item, channel=Channel.PHONE, template_id="phone.call",
                             actor="cindy", outcome="reached", minutes_spent=3.0)
    assert summarize([item])["automated_touch_share"] == 0.5


def test_missing_baseline_stays_visibly_missing():
    """The month-end window passed uninstrumented; a missing baseline must not
    turn into an estimate somebody quotes."""
    out = baseline_comparison([_detect(_snap(has_insurance_package=False))])
    assert out["baseline_available"] is False
    assert "re-ask" in out["note"]
    assert "minutes_per_card_delta" not in out


def test_baseline_delta_when_the_practice_number_exists():
    snap = _snap(has_insurance_package=False)
    item = _detect(snap)
    runner = _runner()
    runner.log_staff_attempt(item, channel=Channel.PHONE, template_id="phone.call",
                             actor="cindy", outcome="reached", minutes_spent=4.0)
    runner.close_verified(item, verified_by="cindy")
    out = baseline_comparison([item], baseline_minutes_per_card=12.0,
                              baseline_claims_held_monthly=400)
    assert out["baseline_available"] is True
    assert out["minutes_per_card_delta"] == -8.0
    assert out["claims_held_baseline"] == 400


# --------------------------------------------------------------------------- #
# End to end (the F9 acceptance demo)
# --------------------------------------------------------------------------- #
def test_end_to_end_inactive_eligibility_to_verified_close():
    snap = _snap("PT-77", eligibility_status="INACTIVE",
                 appointment_at="2026-08-18")
    wl = Worklist()
    item = wl.upsert(_detect(snap))
    assert item.trigger is Trigger.ELIGIBILITY_INACTIVE
    assert item.bucket == "eligibility"
    assert item.priority == 80

    runner = _runner()
    first = runner.advance(item, snap)
    assert first["action"] == "sent" and first["channel"] == "email"

    second = runner.advance(item, snap)
    assert second["channel"] == "sms"

    runner.mark_submitted(item, note="uploaded via the secure link")
    assert item.is_open

    runner.close_verified(item, verified_by="cindy", athena_write_id="ath-1",
                          eligibility_status="ACTIVE")
    assert not item.is_open

    m = summarize(wl.items.values())
    assert m["outstanding"] == 0 and m["verified_closed"] == 1
    assert m["touches_by_channel"] == {"email": 1, "sms": 1}
    for message in runner.sent:
        assert_no_phi(message["body"])


def test_policy_serializes_for_review_by_the_staff_who_run_it():
    body = _policy().to_dict()
    assert body["approved"] is True and body["approved_by"] == "grace.targonski"
    assert body["approval_mode"] == "per_policy"
    performers = {s["channel"]: s["performed_by"] for s in body["steps"]}
    assert performers["phone"] == "staff"
    assert performers["email"] == "system"
