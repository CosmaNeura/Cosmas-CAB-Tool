"""Pure P4 scheduler rules: calendar cadence, quiet hours, and dispatch gates."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from cosmaneura_billing.chase.models import ChaseItem, Channel, State, Trigger
from cosmaneura_billing.chase.policy import ApprovalMode, ChasePolicy, Step
from cosmaneura_billing.chase.scheduling import (
    DurableScheduleEvidence,
    ScheduleAction,
    ScheduleConfigurationError,
    ScheduleReason,
    cadence_due_at,
    evaluate_step,
    next_permitted_due_at,
)
from cosmaneura_billing.chase.policy_fingerprint import (
    DeliveryConfiguration, policy_fingerprint,
)


UTC = timezone.utc
EASTERN = "America/New_York"
DELIVERY = DeliveryConfiguration(
    provider="azure_communication_services_email",
    provider_resource="https://pilot.communication.azure.com",
    enabled_channels=(Channel.EMAIL,),
    sender_address="sender@pilot.example",
    reply_to_address="billing@pilot.example",
    contact_snapshot_max_age_hours=168,
    scheduler_enabled=True,
    operational_enabled=True,
)
_MISSING = object()


def _item(*, state=State.DETECTED, detected_at="2026-08-17T13:15:00+00:00"):
    return ChaseItem(
        chase_id="CHASE-SYNTHETIC",
        patient_ref="PATIENT-SYNTHETIC",
        trigger=Trigger.NO_CARD_ON_FILE,
        detected_at=detected_at,
        state=state,
    )


def _policy(**changes):
    policy = ChasePolicy(**changes)
    return policy.approve(by="pilot.approver", at="2026-08-16T12:00:00+00:00")


def _utc(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def _authorization(
    policy, step, *, active=True, message_release_id="", delivery=DELIVERY,
):
    gate = "a" * 64
    return DurableScheduleEvidence(
        policy_revision_id="11111111-1111-4111-8111-111111111111",
        policy_step_ordinal=policy.steps.index(step),
        approval_event_id="22222222-2222-4222-8222-222222222222",
        approved_policy_fingerprint=policy_fingerprint(
            policy, practice_timezone=EASTERN, delivery=delivery,
        ),
        contact_snapshot_id="33333333-3333-4333-8333-333333333333",
        contact_verified_at="2026-08-17T13:15:00+00:00",
        current_gate_fingerprint=gate,
        reserved_gate_fingerprint=gate,
        gate_disposition="clear",
        message_release_id=message_release_id,
        active=active,
    )


def _evaluate(
    item, policy, step, *, now, practice_timezone=EASTERN,
    authorization=_MISSING, delivery=DELIVERY, **kwargs,
):
    if authorization is _MISSING:
        authorization = _authorization(policy, step, delivery=delivery)
    return evaluate_step(
        item, policy, step, now=now, practice_timezone=practice_timezone,
        authorization=authorization, delivery=delivery, **kwargs,
    )


def test_default_offsets_are_absolute_local_days_after_detection():
    policy = _policy()

    actual = [
        cadence_due_at(
            "2026-08-17T13:15:00+00:00",  # 09:15 in New York
            day_offset=step.day_offset,
            practice_timezone=EASTERN,
            quiet_hours=policy.quiet_hours,
        )
        for step in policy.steps
    ]

    assert [step.day_offset for step in policy.steps] == [0, 1, 3, 5]
    assert actual == [
        _utc("2026-08-17T13:15:00+00:00"),
        _utc("2026-08-18T13:15:00+00:00"),
        _utc("2026-08-20T13:15:00+00:00"),
        _utc("2026-08-22T13:15:00+00:00"),
    ]


@pytest.mark.parametrize(
    "detected,quiet,expected",
    [
        # Wraparound quiet period: 22:00 is held until 08:00 the next day.
        ("2026-08-17T22:00:00-04:00", (21, 8), "2026-08-18T12:00:00+00:00"),
        # Its end boundary is permitted, while its start boundary is not.
        ("2026-08-17T08:00:00-04:00", (21, 8), "2026-08-17T12:00:00+00:00"),
        ("2026-08-17T21:00:00-04:00", (21, 8), "2026-08-18T12:00:00+00:00"),
        # A wraparound window resolves its end in the new post-DST offset.
        ("2026-03-07T22:00:00-05:00", (21, 8), "2026-03-08T12:00:00+00:00"),
        # Non-wrap quiet period works the same way within one calendar date.
        ("2026-08-17T10:30:00-04:00", (8, 17), "2026-08-17T21:00:00+00:00"),
        ("2026-08-17T17:00:00-04:00", (8, 17), "2026-08-17T21:00:00+00:00"),
    ],
)
def test_due_instants_respect_wraparound_and_nonwrap_quiet_hours(
    detected, quiet, expected,
):
    assert cadence_due_at(
        detected,
        day_offset=0,
        practice_timezone=EASTERN,
        quiet_hours=quiet,
    ) == _utc(expected)


def test_overdue_work_is_held_when_the_scheduler_runs_during_quiet_hours():
    assert next_permitted_due_at(
        "2026-08-17T16:00:00+00:00",  # due at noon local
        as_of="2026-08-18T02:30:00+00:00",  # retry at 22:30 local
        practice_timezone=EASTERN,
        quiet_hours=(21, 8),
    ) == _utc("2026-08-18T12:00:00+00:00")


def test_calendar_offsets_follow_spring_dst_without_adding_24_hours():
    due = cadence_due_at(
        "2026-03-07T10:00:00-05:00",
        day_offset=1,
        practice_timezone=EASTERN,
        quiet_hours=(21, 8),
    )

    assert due == _utc("2026-03-08T14:00:00+00:00")  # 10:00 EDT
    assert due - _utc("2026-03-07T15:00:00+00:00") == timedelta(hours=23)


def test_calendar_offsets_follow_fall_dst_without_adding_24_hours():
    due = cadence_due_at(
        "2026-10-31T10:00:00-04:00",
        day_offset=1,
        practice_timezone=EASTERN,
        quiet_hours=(21, 8),
    )

    assert due == _utc("2026-11-01T15:00:00+00:00")  # 10:00 EST


def test_imaginary_spring_wall_time_is_shifted_across_the_dst_gap():
    assert cadence_due_at(
        "2026-03-07T02:30:00-05:00",
        day_offset=1,
        practice_timezone=EASTERN,
        quiet_hours=(12, 13),
    ) == _utc("2026-03-08T07:30:00+00:00")  # 03:30 EDT


@pytest.mark.parametrize("timezone_name", ["", "Not/A_Real_Zone", " America/New_York"])
def test_practice_timezone_must_be_an_iana_name(timezone_name):
    with pytest.raises(ScheduleConfigurationError, match="timezone"):
        cadence_due_at(
            "2026-08-17T13:15:00+00:00",
            day_offset=0,
            practice_timezone=timezone_name,
            quiet_hours=(21, 8),
        )


def test_naive_instants_and_ambiguous_quiet_configuration_fail_closed():
    with pytest.raises(ScheduleConfigurationError, match="UTC offset"):
        cadence_due_at(
            "2026-08-17T13:15:00",
            day_offset=0,
            practice_timezone=EASTERN,
            quiet_hours=(21, 8),
        )
    with pytest.raises(ScheduleConfigurationError, match="ambiguous"):
        cadence_due_at(
            "2026-08-17T13:15:00+00:00",
            day_offset=0,
            practice_timezone=EASTERN,
            quiet_hours=(8, 8),
        )


def test_malformed_instant_does_not_survive_in_exception_context():
    leaked = "PATIENT-IDENTIFIER-not-a-date"
    with pytest.raises(ScheduleConfigurationError) as caught:
        cadence_due_at(
            leaked,
            day_offset=0,
            practice_timezone=EASTERN,
            quiet_hours=(21, 8),
        )

    assert leaked not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_exact_due_boundary_is_eligible_but_one_microsecond_before_is_not():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 1),))
    item = _item()
    due = "2026-08-18T13:15:00+00:00"

    waiting = _evaluate(
        item,
        policy,
        policy.steps[0],
        now="2026-08-18T13:14:59.999999+00:00",
        practice_timezone=EASTERN,
    )
    exact = _evaluate(
        item,
        policy,
        policy.steps[0],
        now=due,
        practice_timezone=EASTERN,
    )

    assert waiting.action is ScheduleAction.WAIT
    assert waiting.reason_code is ScheduleReason.NOT_DUE
    assert waiting.next_permitted_at == _utc(due)
    assert exact.action is ScheduleAction.TRANSPORT
    assert exact.reason_code is ScheduleReason.TRANSPORT_READY
    assert exact.automated_dispatch_eligible


@pytest.mark.parametrize(
    "state",
    [
        State.SUBMITTED,
        State.NEEDS_REVIEW,
        State.VERIFIED_CLOSED,
        State.ESCALATED,
        State.UNREACHABLE,
        State.RESOLVED_OTHER,
    ],
)
def test_forbidden_item_states_never_enter_automated_dispatch(state):
    policy = _policy()

    decision = _evaluate(
        _item(state=state),
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert not decision.automated_dispatch_eligible


def test_pending_phone_task_blocks_every_further_automated_step():
    item = _item(state=State.IN_PROGRESS)
    item.pending_task = {"channel": "phone", "created_at": "synthetic"}
    policy = _policy()

    decision = _evaluate(
        item,
        policy,
        policy.steps[0],
        now="2026-08-20T13:15:00+00:00",
        practice_timezone=EASTERN,
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.STAFF_TASK_PENDING
    assert "pending staff task" in decision.reason


def test_absent_and_inactive_durable_policy_evidence_never_dispatches():
    item = _item()
    unapproved = ChasePolicy()
    approved = _policy()

    no_approval = _evaluate(
        item,
        unapproved,
        unapproved.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
        authorization=None,
    )
    retired = _evaluate(
        item,
        approved,
        approved.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
        authorization=_authorization(
            approved, approved.steps[0], active=False,
        ),
    )

    assert no_approval.action is ScheduleAction.BLOCKED
    assert no_approval.reason_code is ScheduleReason.DURABLE_EVIDENCE_MISSING
    assert retired.action is ScheduleAction.BLOCKED
    assert retired.reason_code is ScheduleReason.POLICY_INACTIVE


def test_per_message_mode_requires_a_durable_exact_release():
    policy = _policy(approval_mode=ApprovalMode.PER_MESSAGE)
    item = _item()

    held = _evaluate(
        item,
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
    )
    released = _evaluate(
        item,
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
        authorization=_authorization(
            policy,
            policy.steps[0],
            message_release_id="44444444-4444-4444-8444-444444444444",
        ),
    )

    assert held.action is ScheduleAction.BLOCKED
    assert released.action is ScheduleAction.TRANSPORT


def test_phone_step_is_a_staff_task_and_never_transport():
    policy = _policy(steps=(Step(Channel.PHONE, "phone.call", 0),))

    decision = _evaluate(
        _item(),
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
        authoritative_sms_consent=True,
    )

    assert decision.action is ScheduleAction.STAFF_TASK
    assert decision.reason_code is ScheduleReason.STAFF_TASK_READY
    assert decision.staff_task_due
    assert not decision.automated_dispatch_eligible


def test_phone_staff_task_survives_automated_messaging_kill_switch():
    policy = _policy(steps=(Step(Channel.PHONE, "phone.call", 0),))
    task_only = DeliveryConfiguration(
        scheduler_enabled=True,
        contact_snapshot_max_age_hours=168,
    )

    decision = _evaluate(
        _item(),
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        delivery=task_only,
    )

    assert decision.action is ScheduleAction.STAFF_TASK
    assert decision.reason_code is ScheduleReason.STAFF_TASK_READY
    assert decision.staff_task_due
    assert not decision.automated_dispatch_eligible


def test_sms_is_fail_closed_without_an_enabled_adapter_and_explicit_consent():
    policy = _policy(steps=(Step(Channel.SMS, "sms.first", 0),))
    item = _item()

    absent = _evaluate(
        item,
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
    )
    recorded = _evaluate(
        item,
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
        authoritative_sms_consent=True,
    )
    assert absent.action is ScheduleAction.BLOCKED
    assert recorded.action is ScheduleAction.BLOCKED
    assert recorded.reason_code is ScheduleReason.CHANNEL_UNAVAILABLE
    assert recorded.skippable_without_touch
    assert "no enabled production transport" in recorded.reason


@pytest.mark.parametrize("not_evidence", [None, 1, "yes"])
def test_truthy_values_are_not_authoritative_sms_consent(not_evidence):
    policy = _policy(steps=(Step(Channel.SMS, "sms.first", 0),))

    decision = _evaluate(
        _item(),
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
        authoritative_sms_consent=not_evidence,
    )

    assert decision.action is ScheduleAction.BLOCKED


def test_portal_is_blocked_without_a_deployed_portal_adapter():
    policy = _policy(steps=(Step(Channel.PORTAL, "portal.first", 0),))

    decision = _evaluate(
        _item(),
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.CHANNEL_UNAVAILABLE
    assert "no enabled production transport" in decision.reason


def test_email_may_proceed_without_an_opt_in_feed():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),))

    decision = _evaluate(
        _item(),
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        practice_timezone=EASTERN,
    )

    assert decision.action is ScheduleAction.TRANSPORT
    assert decision.reason_code is ScheduleReason.TRANSPORT_READY
    assert decision.automated_dispatch_eligible


def test_policy_or_transport_drift_invalidates_durable_approval():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),))
    evidence = _authorization(policy, policy.steps[0])
    changed_delivery = replace(DELIVERY, sender_address="changed@pilot.example")

    decision = _evaluate(
        _item(),
        policy,
        policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        authorization=evidence,
        delivery=changed_delivery,
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.POLICY_FINGERPRINT_MISMATCH
    assert "exact approval" in decision.reason


def test_preconfigured_transport_cannot_bypass_operational_kill_switches():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),))
    disabled = replace(DELIVERY, operational_enabled=False)
    evidence = replace(
        _authorization(policy, policy.steps[0]),
        approved_policy_fingerprint=policy_fingerprint(
            policy, practice_timezone=EASTERN, delivery=disabled,
        ),
    )

    decision = _evaluate(
        _item(), policy, policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        authorization=evidence,
        delivery=disabled,
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.MESSAGING_DISABLED
    assert not decision.skippable_without_touch
    assert "operational latch is off" in decision.reason


def test_record_only_skips_unavailable_system_channels_without_counting_touch():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),))
    record_only = DeliveryConfiguration(scheduler_enabled=True)
    evidence = replace(
        _authorization(policy, policy.steps[0]),
        approved_policy_fingerprint=policy_fingerprint(
            policy, practice_timezone=EASTERN, delivery=record_only,
        ),
    )

    decision = _evaluate(
        _item(), policy, policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        authorization=evidence,
        delivery=record_only,
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.CHANNEL_UNAVAILABLE
    assert decision.skippable_without_touch


def test_changed_direct_contract_gate_invalidates_release_evidence():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),))
    evidence = replace(
        _authorization(policy, policy.steps[0]),
        current_gate_fingerprint="b" * 64,
    )

    decision = _evaluate(
        _item(), policy, policy.steps[0],
        now="2026-08-17T13:15:00+00:00",
        authorization=evidence,
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.GATE_EVIDENCE_CHANGED
    assert not decision.skippable_without_touch
    assert "gate evidence changed" in decision.reason


def test_stale_verified_contact_snapshot_never_dispatches():
    policy = _policy(steps=(Step(Channel.EMAIL, "email.first", 0),))

    decision = _evaluate(
        _item(), policy, policy.steps[0],
        now="2026-08-24T13:15:01+00:00",
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.CONTACT_SNAPSHOT_STALE
    assert "contact snapshot is stale" in decision.reason


def test_step_beyond_approved_max_touches_never_dispatches():
    policy = _policy(max_touches=1)
    second = policy.steps[1]

    decision = _evaluate(
        _item(), policy, second,
        now="2026-08-18T13:15:00+00:00",
    )

    assert decision.action is ScheduleAction.BLOCKED
    assert decision.reason_code is ScheduleReason.STEP_IDENTITY_MISMATCH
    assert "outside the approved cadence" in decision.reason
