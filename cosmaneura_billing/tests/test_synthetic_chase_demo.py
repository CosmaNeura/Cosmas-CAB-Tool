"""Digest-pinned, outbound-dormant insurance follow-up demo contracts."""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cosmaneura_billing import api
from cosmaneura_billing.chase.models import ChaseItem, State
from cosmaneura_billing.chase.synthetic_demo import (
    EXPECTED_PACK_DIGEST,
    prepare_synthetic_chase_seed,
    synthetic_case_for,
)
from cosmaneura_billing.settings import Settings


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        environment="test",
        auth_mode="disabled",
        development_roles=("Admin",),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        access_audit_path=str(tmp_path / "access-audit.jsonl"),
    )
    return TestClient(api.create_app(
        settings=settings,
        frontend_dist=tmp_path / "missing-frontend",
    ))


def test_pack_is_digest_pinned_and_covers_the_four_approved_cases():
    seed = prepare_synthetic_chase_seed()

    assert seed.digest == EXPECTED_PACK_DIGEST
    assert [case.case_id for case in seed.cases] == [
        "SYN-CHASE-0001",
        "SYN-CHASE-0002",
        "SYN-CHASE-0003",
        "SYN-CHASE-0004",
    ]
    assert [case.item.trigger.value for case in seed.cases] == [
        "no_card_on_file", "expired", "eligibility_inactive", "no_card_on_file",
    ]
    assert [case.item.state for case in seed.cases] == [
        State.DETECTED, State.DETECTED, State.DETECTED, State.NEEDS_REVIEW,
    ]
    assert all(
        case.contact.destination.startswith("+1 202-555-01")
        for case in seed.cases
    )


def test_synthetic_contact_requires_exact_server_owned_seed_provenance():
    case = prepare_synthetic_chase_seed().cases[0]
    exact = ChaseItem.from_dict(case.item.to_dict())
    assert synthetic_case_for(exact) == case

    lookalike = ChaseItem.from_dict(case.item.to_dict())
    lookalike.direct_contract.pop("synthetic_demo_provenance")
    assert synthetic_case_for(lookalike) is None

    altered = ChaseItem.from_dict(case.item.to_dict())
    provenance = deepcopy(altered.direct_contract["synthetic_demo_provenance"])
    provenance["case_digest"] = "0" * 64
    altered.direct_contract["synthetic_demo_provenance"] = provenance
    assert synthetic_case_for(altered) is None

    # Legitimate worklist state changes preserve the server-owned provenance.
    exact.assignee = "local-developer"
    exact.state = State.IN_PROGRESS
    assert synthetic_case_for(exact) == case


def test_admin_seed_is_exact_idempotent_and_outbound_dormant(client):
    descriptor = client.get("/api/session").json()["actions"][
        "seedSyntheticChase"
    ]
    assert descriptor == {
        "seedDigest": EXPECTED_PACK_DIGEST,
        "caseCount": 4,
    }
    mismatch = client.post(
        "/api/chase/demo-seed",
        json={"seedDigest": "0" * 64},
    )
    assert mismatch.status_code == 409

    first = client.post(
        "/api/chase/demo-seed",
        json={"seedDigest": descriptor["seedDigest"]},
    )
    replay = client.post(
        "/api/chase/demo-seed",
        json={"seedDigest": descriptor["seedDigest"]},
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["casesReady"] == replay.json()["casesReady"] == 4
    assert first.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert first.json()["outbound"] == {
        "schedulerEnabled": False,
        "patientMessagingEnabled": False,
        "operationallyActivated": False,
        "transport": "record_only",
        "automaticContact": False,
    }

    worklist = client.post("/api/chase/worklist", json={}).json()
    assert worklist["counts"]["open"] == 4
    assert worklist["counts"]["by_bucket"] == {
        "missing": 2, "expired": 1, "eligibility": 1,
    }
    assert worklist["counts"]["needs_person"] == 1
    assert all(
        "patient_ref" not in row
        and "direct_contract" not in row
        and "attempts" not in row
        for row in worklist["rows"]
    )


def test_task_returns_only_fixed_synthetic_contact_and_withholds_review_case(client):
    client.post(
        "/api/chase/demo-seed",
        json={"seedDigest": EXPECTED_PACK_DIGEST},
    )
    rows = client.post("/api/chase/worklist", json={}).json()["rows"]
    ordinary = next(row for row in rows if row["state"] == "detected")
    review = next(row for row in rows if row["state"] == "needs_review")

    task = client.post(
        "/api/chase/task", json={"chaseId": ordinary["chaseId"]},
    )
    held = client.post(
        "/api/chase/task", json={"chaseId": review["chaseId"]},
    )

    assert task.status_code == held.status_code == 200
    assert task.json()["contact"]["destination"].startswith("+1 202-555-01")
    assert task.json()["contact"]["mode"] == "synthetic_demo"
    assert "log_attempt" in task.json()["allowedActions"]
    assert held.json()["contact"] is None
    assert "log_attempt" not in held.json()["allowedActions"]
    assert "direct-contract" in held.json()["instruction"]
    assert "patient_ref" not in task.text + held.text
    assert ordinary["chaseId"] not in task.request.url.path

    client.post("/api/chase/action", json={
        "chaseId": ordinary["chaseId"],
        "action_id": str(uuid4()),
        "action": "submitted",
    })
    awaiting_verification = client.post(
        "/api/chase/task", json={"chaseId": ordinary["chaseId"]},
    ).json()
    assert awaiting_verification["contact"] is None
    assert "do not contact again" in awaiting_verification["instruction"]


def test_operational_lookalike_never_unlocks_a_synthetic_destination(client):
    case = prepare_synthetic_chase_seed().cases[0]
    detected = client.post("/api/chase/detect", json={
        "today": "2026-08-21",
        "snapshots": [{
            "patient_ref": case.item.patient_ref,
            "appointment_at": case.item.appointment_at,
            "has_insurance_package": False,
        }],
    }).json()
    assert detected["detected"] == 1
    row = client.post("/api/chase/worklist", json={}).json()["rows"][0]

    task = client.post(
        "/api/chase/task", json={"chaseId": row["chaseId"]},
    ).json()

    assert row["dataClass"] == "operational"
    assert task["dataClass"] == "operational"
    assert task["contact"] is None
    assert task["systemOfRecord"]["deepLinkConfigured"] is False
    assert task["systemOfRecord"]["name"] == "Practice source system"


def test_seed_refuses_to_run_when_scheduler_is_enabled(tmp_path):
    settings = Settings(
        environment="test",
        auth_mode="disabled",
        development_roles=("Admin",),
        chase_scheduler_enabled=True,
        feedback_path=str(tmp_path / "feedback.jsonl"),
        access_audit_path=str(tmp_path / "access-audit.jsonl"),
    )
    client = TestClient(api.create_app(
        settings=settings,
        frontend_dist=tmp_path / "missing-frontend",
    ))

    response = client.post(
        "/api/chase/demo-seed",
        json={"seedDigest": EXPECTED_PACK_DIGEST},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "synthetic chase seed requires dormant record-only outreach"
    )


def test_durable_seed_passes_one_complete_batch_and_audit_completion(tmp_path):
    class RecordingScheduler:
        def __init__(self):
            self.entries = None
            self.audit_event = None

        def persist_detection_batch(self, entries, *, audit_event):
            self.entries = list(entries)
            self.audit_event = audit_event
            return [object() for _ in self.entries]

    settings = Settings(
        environment="test",
        auth_mode="disabled",
        development_roles=("Admin",),
        feedback_path=str(tmp_path / "feedback.jsonl"),
        access_audit_path=str(tmp_path / "access-audit.jsonl"),
    )
    application = api.create_app(
        settings=settings,
        frontend_dist=tmp_path / "missing-frontend",
    )
    scheduler = RecordingScheduler()
    application.state.atomic_chase_audit = True
    application.state.chase_context = {
        "store": api._build_chase_context(settings)["store"],
        "scheduler": scheduler,
    }

    response = TestClient(application).post(
        "/api/chase/demo-seed",
        json={"seedDigest": EXPECTED_PACK_DIGEST},
    )

    assert response.status_code == 200
    assert response.json()["casesReady"] == 4
    assert len(scheduler.entries) == 4
    assert scheduler.audit_event.action == "chase.decide"
    assert scheduler.audit_event.route == "/api/chase/demo-seed"
    assert scheduler.audit_event.outcome == "allowed"
    assert all(
        len(contact.routes) == 1
        and contact.routes[0].channel.value == "phone"
        and contact.routes[0].consent_status == "not_required"
        for _item, contact in scheduler.entries
    )
