"""Chase storage contracts that do not require a running database."""
from __future__ import annotations

import pytest

from cosmaneura_billing.chase import (
    Attempt, Channel, ChaseItem, State, Trigger, Worklist, detect,
)
from cosmaneura_billing.chase.models import CoverageSnapshot
from cosmaneura_billing.chase.store import chase_store_from_env
from cosmaneura_billing.direct_contract import DirectContractList


def _item(patient_ref: str = "PT-STORE") -> ChaseItem:
    return ChaseItem(
        chase_id="chase-store-1",
        patient_ref=patient_ref,
        trigger=Trigger.NO_CARD_ON_FILE,
        detected_at="2026-08-17T09:00:00+00:00",
        appointment_at="2026-08-18",
        state=State.IN_PROGRESS,
        priority=80,
        direct_contract={"is_direct_contract": False, "source": "test"},
        assignee="cindy",
        attempts=[Attempt(
            channel=Channel.PHONE,
            template_id="phone.call",
            at="2026-08-17T10:00:00+00:00",
            actor="cindy",
            outcome="left_message",
            minutes_spent=2.5,
            note="retry tomorrow",
        )],
        pending_task={"channel": "phone", "created_at": "2026-08-17T09:00:00+00:00"},
        resolution={"outcome": "submitted", "note": "awaiting verification"},
    )


def test_chase_item_round_trips_persisted_state_without_derived_fields():
    original = _item()
    payload = original.to_dict()
    payload.update({"touches": 999, "staff_minutes": 999, "open": False})

    restored = ChaseItem.from_dict(payload)

    assert restored.to_dict() == original.to_dict()
    assert restored.attempts[0].channel is Channel.PHONE
    assert restored.state is State.IN_PROGRESS


def test_memory_store_exposes_the_same_atomic_mutation_seam():
    store = Worklist()
    stored = store.upsert(_item())

    changed = store.mutate(stored.chase_id, lambda item: setattr(item, "assignee", "maggie"))

    assert changed is not None and changed.assignee == "maggie"
    assert store.all_items() == [changed]
    assert store.mutate("missing", lambda item: None) is None


def test_detector_ids_are_stable_and_do_not_expose_patient_reference():
    roster = DirectContractList(refs=[], version="test")
    missing = CoverageSnapshot(patient_ref="PATIENT-SENSITIVE", email="x@example.invalid")
    first = detect(missing, today="2026-08-17", roster=roster)
    second = detect(missing, today="2026-08-18", roster=roster)
    inactive = detect(
        CoverageSnapshot(
            patient_ref="PATIENT-SENSITIVE",
            has_insurance_package=True,
            card_image_on_file=True,
            eligibility_status="INACTIVE",
            email="x@example.invalid",
        ),
        today="2026-08-17",
        roster=roster,
    )

    assert first is not None and second is not None and inactive is not None
    assert first.chase_id == second.chase_id
    assert inactive.chase_id != first.chase_id
    assert "PATIENT-SENSITIVE" not in first.chase_id


def test_store_configuration_defaults_to_memory_outside_production():
    assert isinstance(chase_store_from_env({}), Worklist)


def test_production_refuses_local_state_before_constructing_memory_store():
    with pytest.raises(ValueError, match="STATE_STORE=postgres"):
        chase_store_from_env({
            "COSMANEURA_ENV": "production",
            "COSMANEURA_AUTH_MODE": "container_apps",
            "COSMANEURA_ENTRA_TENANT_ID": "11111111-1111-4111-8111-111111111111",
            "COSMANEURA_PRACTICE_ID": "practice-test",
            "COSMANEURA_CHASE_STORE": "memory",
        })


def test_postgres_configuration_requires_dsn_and_practice_scope():
    with pytest.raises(ValueError, match="database URL"):
        chase_store_from_env({
            "COSMANEURA_CHASE_STORE": "postgres",
            "COSMANEURA_PRACTICE_ID": "practice-a",
        })
    with pytest.raises(ValueError, match="PRACTICE_ID"):
        chase_store_from_env({
            "COSMANEURA_CHASE_STORE": "postgres",
            "DATABASE_URL": "postgresql://example.invalid/db",
        })


def test_unknown_store_backend_is_rejected():
    with pytest.raises(ValueError, match="memory.*postgres"):
        chase_store_from_env({"COSMANEURA_CHASE_STORE": "filesystem"})
