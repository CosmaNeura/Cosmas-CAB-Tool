"""Synthetic MCD-admin Card Chase ingest and record-only phone worklist."""
import copy
import json
from pathlib import Path

import pytest

from cosmaneura_billing.chase.mcd_admin import (
    IdempotencyConflict,
    McdAdminChaseConfig,
    McdAdminChaseService,
    McdAdminChaseStore,
    PhoneTaskStatus,
    ROSTER_SCHEMA,
    SyntheticRosterRejected,
    TaskStateConflict,
    UnsafeMcdAdminConfiguration,
)
from cosmaneura_billing.direct_contract import DirectContractList

NOW = "2026-08-29T14:00:00+00:00"
GOLDEN = Path(__file__).resolve().parents[1] / "frontend/src/operationalMcdWorklist.golden.json"


def _roster():
    return {
        "schema": ROSTER_SCHEMA,
        "synthetic": True,
        "source": "synthetic_fixture",
        "roster_id": "SYN-MCD-ROSTER-DEMO-01",
        "people": [
            {
                "synthetic": True,
                "person_ref": "SYN-MCD-001",
                "display_label": "Synthetic Patient 001",
                "phone": "+12025550101",
                "outreach_reason": "missing_insurance_card",
                "due_at": "2026-08-30T13:00:00+00:00",
                "voice_consent": True,
                "requested_channel": "phone",
            },
            {
                "synthetic": True,
                "person_ref": "SYN-MCD-002",
                "display_label": "Synthetic Patient 002",
                "phone": "+12025550102",
                "outreach_reason": "expired_insurance_card",
                "due_at": "2026-08-29T15:00:00+00:00",
                "voice_consent": True,
                "requested_channel": "phone",
            },
        ],
    }


def _service(now=lambda: NOW, roster=None):
    kwargs = {"now": now}
    if roster is not None:
        kwargs["direct_contract_roster"] = roster
    return McdAdminChaseService(McdAdminChaseStore(), **kwargs)


def test_defaults_are_dormant_and_ingest_records_queryable_phone_tasks():
    service = _service()
    receipt = service.ingest_roster(json.dumps(_roster()), key="ingest-demo-01")

    assert receipt.created_count == 2 and not receipt.replayed
    worklist = service.worklist()
    encoded = (json.dumps(worklist, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert GOLDEN.read_bytes() == encoded
    assert worklist["mode"] == "synthetic_only"
    assert worklist["safety"] == {
        "transport": "record_only",
        "scheduler_enabled": False,
        "patient_messaging_enabled": False,
        "patient_messaging_operationally_activated": False,
        "cab_deploy_enabled": False,
        "athena_mode": "disabled",
    }
    assert worklist["counts"] == {"open": 2, "completed": 0, "total": 2}
    assert [row["person"]["person_ref"] for row in worklist["tasks"]] == [
        "SYN-MCD-002", "SYN-MCD-001",
    ]
    for task in worklist["tasks"]:
        assert task["channel"] == "phone"
        assert task["performed_by"] == "staff"
        assert task["automated"] is False
        assert task["transport"] == "record_only"
        assert task["person"]["voice_consent"] is True
        assert task["direct_contract"]["known"] is True
        assert task["direct_contract"]["blocked"] is False
        assert task["direct_contract"]["source"] == "in_memory:mcd-admin-synthetic-v1"
        assert len(task["direct_contract"]["gate_fingerprint"]) == 64


@pytest.mark.parametrize("setting,value", [
    ("chase_transport", "acs_email"),
    ("chase_scheduler_enabled", True),
    ("patient_messaging_enabled", True),
    ("patient_messaging_operationally_activated", True),
    ("cab_deploy_enabled", True),
    ("athena_mode", "live"),
])
def test_unsafe_runtime_postures_fail_closed(setting, value):
    with pytest.raises(UnsafeMcdAdminConfiguration):
        McdAdminChaseConfig(**{setting: value})


def test_existing_environment_latches_are_checked_and_invalid_booleans_reject():
    with pytest.raises(UnsafeMcdAdminConfiguration):
        McdAdminChaseConfig.from_env({"COSMANEURA_PATIENT_MESSAGING_ENABLED": "true"})
    with pytest.raises(UnsafeMcdAdminConfiguration):
        McdAdminChaseConfig.from_env({"COSMANEURA_CHASE_SCHEDULER_ENABLED": "maybe"})


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(synthetic=False),
    lambda r: r.update(source="athena_live"),
    lambda r: r["people"][0].update(synthetic=False),
    lambda r: r["people"][0].update(phone="+14155552671"),
    lambda r: r["people"][0].update(requested_channel="sms"),
    lambda r: r["people"][0].update(voice_consent=False),
    lambda r: r["people"][0].update(direct_contract_status="not_direct_contract"),
    lambda r: r["people"][0].update(member_id="not-accepted"),
])
def test_real_or_unsafe_roster_rows_fail_atomically(mutate):
    roster, service = copy.deepcopy(_roster()), _service()
    mutate(roster)
    with pytest.raises(SyntheticRosterRejected):
        service.ingest_roster(roster)
    assert service.list_phone_tasks() == []


def test_roster_ingest_is_idempotent_and_conflicts_fail_closed():
    service, roster = _service(), _roster()
    first = service.ingest_roster(roster, key="request-1")
    replay = service.ingest_roster(copy.deepcopy(roster), key="request-1")
    assert replay.replayed and replay.created_count == 0
    assert replay.task_ids == first.task_ids
    assert len(service.list_phone_tasks()) == 2

    changed = copy.deepcopy(roster)
    changed["people"][0]["due_at"] = "2026-09-01T13:00:00+00:00"
    with pytest.raises(IdempotencyConflict):
        service.ingest_roster(changed, key="request-1")
    assert len(service.list_phone_tasks()) == 2


def test_direct_contract_match_or_unknown_rejects_the_whole_roster(tmp_path):
    blocked = DirectContractList(refs=["SYN-MCD-001"], version="synthetic-test")
    unknown = DirectContractList(path=tmp_path / "missing.csv")
    for roster in (blocked, unknown):
        service = _service(roster=roster)
        with pytest.raises(SyntheticRosterRejected, match="direct-contract gate"):
            service.ingest_roster(_roster())
        assert service.list_phone_tasks() == []


def test_staff_phone_action_is_recorded_queryable_and_retry_idempotent():
    times = iter([
        NOW, "2026-08-29T14:04:00+00:00", "2026-08-29T14:05:00+00:00",
        "2026-08-29T14:06:00+00:00", "2026-08-29T14:07:00+00:00",
    ])
    service = _service(lambda: next(times))
    task_id = service.ingest_roster(_roster()).task_ids[0]

    first = service.record_phone_action(
        task_id=task_id, action_id="phone-action-001",
        actor_ref="SYN-STAFF-001", outcome="no_answer", minutes_spent=2.25,
    )
    replay = service.record_phone_action(
        task_id=task_id, action_id="phone-action-001",
        actor_ref="SYN-STAFF-001", outcome="no_answer", minutes_spent=2.25,
    )
    assert not first.replayed and replay.replayed
    assert replay.action.occurred_at == "2026-08-29T14:04:00+00:00"
    assert service.get_phone_task(task_id).status is PhoneTaskStatus.OPEN
    assert len(service.list_phone_tasks(status="open")) == 2
    assert len(service.get_phone_task(task_id).actions) == 1
    recorded = service.get_phone_task(task_id).to_dict()["actions"][0]
    assert recorded["automated_outbound_initiated"] is False
    assert recorded["manual_outreach_recorded"] is True

    completed = service.record_phone_action(
        task_id=task_id, action_id="phone-action-002",
        actor_ref="SYN-STAFF-001", outcome="reached", minutes_spent=1,
    )
    assert completed.task.status is PhoneTaskStatus.COMPLETED
    assert len(completed.task.actions) == 2

    with pytest.raises(IdempotencyConflict):
        service.record_phone_action(
            task_id=task_id, action_id="phone-action-001",
            actor_ref="SYN-STAFF-001", outcome="reached", minutes_spent=2.25,
        )


@pytest.mark.parametrize("terminal_outcome", ["reached", "declined"])
def test_a_terminal_outcome_completes_and_rejects_another_action(terminal_outcome):
    service = _service()
    task_id = service.ingest_roster(_roster()).task_ids[0]
    service.record_phone_action(
        task_id=task_id, action_id="action-1", actor_ref="SYN-STAFF-001",
        outcome=terminal_outcome, minutes_spent=3,
    )
    with pytest.raises(TaskStateConflict):
        service.record_phone_action(
            task_id=task_id, action_id="action-2", actor_ref="SYN-STAFF-002",
            outcome="no_answer", minutes_spent=1,
        )


@pytest.mark.parametrize("retryable", ["no_answer", "left_message", "wrong_number"])
def test_retryable_phone_outcomes_leave_task_open(retryable):
    service = _service()
    task_id = service.ingest_roster(_roster()).task_ids[0]
    result = service.record_phone_action(
        task_id=task_id, action_id="attempt-1", actor_ref="SYN-STAFF-001",
        outcome=retryable, minutes_spent=1,
    )
    assert result.task.status is PhoneTaskStatus.OPEN


def test_due_times_are_normalized_to_utc_before_worklist_sorting():
    roster = _roster()
    roster["people"][0]["due_at"] = "2026-08-29T10:00:00-04:00"  # 14:00Z
    roster["people"][1]["due_at"] = "2026-08-29T13:00:00+00:00"
    roster["people"][1]["outreach_reason"] = "missing_insurance_card"
    service = _service()
    service.ingest_roster(roster)
    tasks = service.list_phone_tasks()
    assert [task.person.person_ref for task in tasks] == ["SYN-MCD-002", "SYN-MCD-001"]
    assert all(task.person.due_at.endswith("+00:00") for task in tasks)
