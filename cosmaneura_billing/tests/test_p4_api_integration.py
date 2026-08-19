"""Focused API contracts for the durable P4 chase scheduler seam."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cosmaneura_billing import api
from cosmaneura_billing.access_audit import classify_access
from cosmaneura_billing.chase import (
    ChaseItem,
    ChasePolicy,
    SequenceRunner,
    State,
    Trigger,
    Worklist,
)
from cosmaneura_billing.chase.policy_fingerprint import (
    DeliveryConfiguration,
    policy_fingerprint,
)
from cosmaneura_billing.chase.scheduler_store import (
    ApprovalRecord,
    PolicyRevisionRecord,
    ReleaseRecord,
    StaffTaskCompletionResult,
)
from cosmaneura_billing.direct_contract import DirectContractList
from cosmaneura_billing.settings import Settings


_GATE_FINGERPRINT = "b" * 64
_POLICY_REVISION_ID = "11111111-1111-4111-8111-111111111111"
_CONTACT_SNAPSHOT_ID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize(
    ("method", "path", "action", "route"),
    [
        (
            "GET",
            "/api/chase/policy/status",
            "chase.policy.view",
            "/api/chase/policy/status",
        ),
        (
            "POST",
            "/api/chase/policy/create",
            "chase.policy.decide",
            "/api/chase/policy/create",
        ),
        (
            "POST",
            "/api/chase/policy/approve",
            "chase.policy.decide",
            "/api/chase/policy/approve",
        ),
        (
            "POST",
            "/api/chase/policy/activate",
            "chase.policy.decide",
            "/api/chase/policy/activate",
        ),
        (
            "POST",
            "/api/chase/policy/revoke",
            "chase.policy.decide",
            "/api/chase/policy/revoke",
        ),
        (
            "POST",
            "/api/chase/synthetic-patient/release-message",
            "chase.decide",
            "/api/chase/{id}/release-message",
        ),
    ],
)
def test_access_audit_classifies_p4_scheduler_routes(
    method, path, action, route,
):
    assert classify_access(method, path) == (action, route, "")


class RecordingScheduler:
    """Repository-shaped fake that records the API-to-scheduler boundary."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        self.active = False
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))

    def calls_for(self, name: str) -> list[dict]:
        return [kwargs for called, _args, kwargs in self.calls if called == name]

    def persist_detection_batch(self, *args, **kwargs):
        self._record("persist_detection_batch", args, kwargs)
        entries = args[0] if args else kwargs["detections"]
        return [object() for _entry in entries]

    def policy_status(self, *args, **kwargs):
        self._record("policy_status", args, kwargs)
        return {
            "configuredPolicyFingerprint": self.fingerprint,
            "registered": True,
            "active": self.active,
            "policyRevisionId": _POLICY_REVISION_ID,
            "revisionNo": 1,
            "approvalMode": "per_policy",
            "activatedAt": (
                "2026-08-18T12:00:00+00:00" if self.active else ""
            ),
        }

    def create_policy(self, *args, **kwargs):
        self._record("create_policy", args, kwargs)
        policy = args[0] if args else kwargs["policy"]
        return PolicyRevisionRecord(
            kwargs["policy_revision_id"],
            policy.name,
            1,
            self.fingerprint,
            policy.approval_mode.value,
        )

    def approve_policy(self, *args, **kwargs):
        self._record("approve_policy", args, kwargs)
        return ApprovalRecord(
            kwargs["approval_event_id"],
            kwargs["policy_revision_id"],
            self.fingerprint,
            "approved",
            datetime(2026, 8, 18, 12, tzinfo=timezone.utc),
            "c" * 64,
        )

    def activate_policy(self, *args, **kwargs):
        self._record("activate_policy", args, kwargs)

    def revoke_policy(self, *args, **kwargs):
        self._record("revoke_policy", args, kwargs)
        return ApprovalRecord(
            kwargs["approval_event_id"],
            kwargs["policy_revision_id"],
            self.fingerprint,
            "revoked",
            datetime(2026, 8, 18, 13, tzinfo=timezone.utc),
            "d" * 64,
        )

    def release_direct_contract(self, *args, **kwargs):
        self._record("release_direct_contract", args, kwargs)
        return ReleaseRecord(
            kwargs["review_release_id"], kwargs["chase_id"], "e" * 64,
        )

    def complete_staff_task(self, *args, **kwargs):
        self._record("complete_staff_task", args, kwargs)
        return StaffTaskCompletionResult(kwargs["chase_id"], True, True)

    def message_release_target(self, *args, **kwargs):
        self._record("message_release_target", args, kwargs)
        return {
            "policy_revision_id": _POLICY_REVISION_ID,
            "contact_snapshot_id": _CONTACT_SNAPSHOT_ID,
            "step_ordinal": 2,
        }

    def release_message(self, *args, **kwargs):
        self._record("release_message", args, kwargs)
        return ReleaseRecord(
            kwargs["release_id"], kwargs["chase_id"], "f" * 64,
        )


@pytest.fixture()
def p4_api(tmp_path):
    settings = Settings(
        environment="test",
        practice_id="synthetic-practice",
        development_roles=("Admin", "BillingStaff"),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        direct_contract_list=str(tmp_path / "direct-contract.csv"),
        chase_scheduler_enabled=True,
    )
    policy = ChasePolicy(
        practice_name="Synthetic Practice",
        upload_url="https://upload.example.invalid/insurance",
    )
    delivery = DeliveryConfiguration.from_settings(settings)
    fingerprint = policy_fingerprint(
        policy,
        practice_timezone=settings.practice_timezone,
        delivery=delivery,
    )
    scheduler = RecordingScheduler(fingerprint)
    store = Worklist()
    application = api.create_app(
        settings=settings,
        frontend_dist=tmp_path / "missing-frontend",
    )
    # The fake repository accepts the same pre-reserved audit completion as
    # PostgreSQL. Production refuses these mutations without this atomic seam.
    application.state.atomic_chase_audit = True
    application.state.chase_context = {
        "store": store,
        "policy": policy,
        "runner": SequenceRunner(policy=policy),
        "roster": DirectContractList(refs=[], version="synthetic-v1"),
        "scheduler": scheduler,
        "delivery": delivery,
        "practice_timezone": settings.practice_timezone,
        "policy_fingerprint": fingerprint,
    }
    return TestClient(application), store, scheduler, fingerprint


def _chase_item(*, chase_id: str, direct_hold: bool = False) -> ChaseItem:
    return ChaseItem(
        chase_id=chase_id,
        patient_ref=f"SYNTHETIC-{chase_id}",
        trigger=Trigger.NO_CARD_ON_FILE,
        detected_at="2026-08-18T12:00:00+00:00",
        state=State.NEEDS_REVIEW if direct_hold else State.IN_PROGRESS,
        direct_contract={
            "known": True,
            "is_direct_contract": direct_hold,
            "blocked": direct_hold,
            "gate_fingerprint": _GATE_FINGERPRINT,
        },
        review_reason=(
            "patient is on the direct-contract list — review required"
            if direct_hold else ""
        ),
        pending_task=(
            None if direct_hold else {
                "channel": "phone",
                "template_id": "phone.call",
            }
        ),
    )


def _put_chase(store: Worklist, *, chase_id: str, direct_hold: bool = False) -> None:
    store.upsert(_chase_item(chase_id=chase_id, direct_hold=direct_hold))


class NoLegacySnapshotStore:
    """Fail if durable detection tries to assemble a legacy worklist response."""

    def __init__(self) -> None:
        self.to_dict_calls = 0

    def to_dict(self, **_filters):
        self.to_dict_calls += 1
        raise AssertionError("scheduler detection must not read the legacy store")


class SingleLookupStore:
    """Allow the pre-mutation lookup, then fail on a stale post-write read."""

    def __init__(self, item: ChaseItem) -> None:
        self.item = item
        self.get_calls = 0

    def get(self, chase_id: str):
        self.get_calls += 1
        if self.get_calls > 1:
            raise AssertionError("scheduler mutation must not re-read the legacy store")
        return self.item if chase_id == self.item.chase_id else None


def test_detect_rejects_caller_asserted_sms_consent_before_scheduler(p4_api):
    client, store, scheduler, _fingerprint = p4_api

    response = client.post("/api/chase/detect", json={
        "today": "2026-08-18",
        "snapshots": [{
            "patient_ref": "SYNTHETIC-SMS",
            "appointment_at": "2026-08-19",
            "has_insurance_package": False,
            "phone": "+15555550100",
            "sms_consent": True,
        }],
    })

    assert response.status_code == 422
    assert "sms" in response.json()["detail"].casefold()
    assert "consent" in response.json()["detail"].casefold()
    assert store.all_items() == []
    assert scheduler.calls == []


def test_scheduler_detect_does_not_read_a_legacy_worklist_snapshot(p4_api):
    client, _store, scheduler, _fingerprint = p4_api
    legacy_store = NoLegacySnapshotStore()
    client.app.state.chase_context["store"] = legacy_store

    response = client.post("/api/chase/detect", json={
        "today": "2026-08-18",
        "snapshots": [{
            "patient_ref": "SYNTHETIC-DURABLE",
            "appointment_at": "2026-08-19",
            "has_insurance_package": False,
            "email": "synthetic@example.invalid",
        }],
    })

    assert response.status_code == 200, response.text
    assert response.json() == {"checked": 1, "detected": 1}
    assert legacy_store.to_dict_calls == 0
    assert len(scheduler.calls_for("persist_detection_batch")) == 1


def test_phone_log_attempt_routes_to_atomic_scheduler_completion(p4_api):
    client, _store, scheduler, _fingerprint = p4_api
    chase_id = "scheduler-phone"
    action_id = str(uuid4())
    legacy_store = SingleLookupStore(_chase_item(chase_id=chase_id))
    client.app.state.chase_context["store"] = legacy_store

    response = client.post(f"/api/chase/{chase_id}/action", json={
        "action_id": action_id,
        "action": "log_attempt",
        "channel": "phone",
        "outcome": "left_message",
        "minutes_spent": 3.5,
        "note": "asked patient to return the call",
    })

    assert response.status_code == 200, response.text
    [call] = scheduler.calls_for("complete_staff_task")
    assert call["chase_id"] == chase_id
    assert call["action_id"] == action_id
    assert call["outcome"] == "left_message"
    assert call["minutes_spent"] == 3.5
    assert call["note"] == "asked patient to return the call"
    assert legacy_store.get_calls == 1


def test_direct_contract_release_routes_to_exact_gate_release(p4_api):
    client, _store, scheduler, _fingerprint = p4_api
    chase_id = "direct-hold"
    action_id = str(uuid4())
    legacy_store = SingleLookupStore(
        _chase_item(chase_id=chase_id, direct_hold=True),
    )
    client.app.state.chase_context["store"] = legacy_store

    response = client.post(f"/api/chase/{chase_id}/action", json={
        "action_id": action_id,
        "action": "release",
        "reason": "staff confirmed insurance outreach is appropriate",
    })

    assert response.status_code == 200, response.text
    [call] = scheduler.calls_for("release_direct_contract")
    assert call["review_release_id"] == action_id
    assert call["chase_id"] == chase_id
    assert call["gate_fingerprint"] == _GATE_FINGERPRINT
    assert call["reason"] == "staff confirmed insurance outreach is appropriate"
    assert legacy_store.get_calls == 1


def test_per_message_release_resolves_and_releases_the_exact_cadence(p4_api):
    client, store, scheduler, fingerprint = p4_api
    chase_id = "per-message"
    release_id = str(uuid4())
    _put_chase(store, chase_id=chase_id)

    response = client.post(
        f"/api/chase/{chase_id}/release-message",
        json={
            "release_id": release_id,
            "reason": "staff reviewed this exact scheduled message",
        },
    )

    assert response.status_code == 200, response.text
    [target] = scheduler.calls_for("message_release_target")
    assert target["release_id"] == release_id
    assert target["chase_id"] == chase_id
    assert target["expected_policy_fingerprint"] == fingerprint
    assert target["reason"] == "staff reviewed this exact scheduled message"
    [release] = scheduler.calls_for("release_message")
    assert release["release_id"] == release_id
    assert release["chase_id"] == chase_id
    assert release["policy_revision_id"] == _POLICY_REVISION_ID
    assert release["contact_snapshot_id"] == _CONTACT_SNAPSHOT_ID
    assert release["step_ordinal"] == 2
    assert release["expected_policy_fingerprint"] == fingerprint


def test_durable_worklist_uses_active_policy_status(p4_api):
    client, _store, scheduler, fingerprint = p4_api
    scheduler.active = True

    response = client.get("/api/chase/worklist")

    assert response.status_code == 200, response.text
    policy = response.json()["policy"]
    assert policy["approved"] is True
    assert policy["approved_at"] == "2026-08-18T12:00:00+00:00"
    assert policy["durable_status"] == {
        "configuredPolicyFingerprint": fingerprint,
        "registered": True,
        "active": True,
        "policyRevisionId": _POLICY_REVISION_ID,
        "revisionNo": 1,
        "approvalMode": "per_policy",
        "activatedAt": "2026-08-18T12:00:00+00:00",
    }


def test_admin_policy_routes_call_the_exact_scheduler_seams(p4_api):
    client, _store, scheduler, fingerprint = p4_api
    revision_id = str(uuid4())
    approval_id = str(uuid4())
    revocation_id = str(uuid4())

    status = client.get("/api/chase/policy/status")
    created = client.post(
        "/api/chase/policy/create",
        json={"policy_revision_id": revision_id},
    )
    approved = client.post(
        "/api/chase/policy/approve",
        json={
            "approval_event_id": approval_id,
            "policy_revision_id": revision_id,
            "reason": "practice owner reviewed the configured policy",
        },
    )
    activated = client.post(
        "/api/chase/policy/activate",
        json={
            "approval_event_id": approval_id,
            "policy_revision_id": revision_id,
        },
    )
    revoked = client.post(
        "/api/chase/policy/revoke",
        json={
            "approval_event_id": revocation_id,
            "policy_revision_id": revision_id,
            "reason": "practice owner paused outbound messaging",
        },
    )

    for response in (status, created, approved, activated, revoked):
        assert response.status_code == 200, response.text
    assert status.json()["configuredPolicyFingerprint"] == fingerprint
    assert created.json()["policyRevisionId"] == revision_id
    assert approved.json()["approvalEventId"] == approval_id
    assert activated.json() == {"status": "active"}
    assert revoked.json()["approvalEventId"] == revocation_id

    [status_call] = scheduler.calls_for("policy_status")
    assert status_call["expected_policy_fingerprint"] == fingerprint
    [create_call] = scheduler.calls_for("create_policy")
    assert create_call["policy_revision_id"] == revision_id
    [approve_call] = scheduler.calls_for("approve_policy")
    assert approve_call["approval_event_id"] == approval_id
    assert approve_call["policy_revision_id"] == revision_id
    [activate_call] = scheduler.calls_for("activate_policy")
    assert activate_call["approval_event_id"] == approval_id
    assert activate_call["policy_revision_id"] == revision_id
    [revoke_call] = scheduler.calls_for("revoke_policy")
    assert revoke_call["approval_event_id"] == revocation_id
    assert revoke_call["policy_revision_id"] == revision_id
