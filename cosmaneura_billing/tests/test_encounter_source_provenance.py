"""Server-owned encounter provenance survives persistence adapters."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from cosmaneura_billing.persistence import (
    MemoryEncounterStore,
    PersistenceUnavailable,
    PostgresEncounterStore,
    SYNTHETIC_DEMO_SOURCE,
)
from cosmaneura_billing.schemas import VisitBundle


DEMO_SOURCE = SYNTHETIC_DEMO_SOURCE


def _bundle(encounter_id: str = "external-id-without-special-prefix") -> VisitBundle:
    return VisitBundle(
        encounter_id=encounter_id,
        patient_age=42,
        patient_sex="unknown",
        encounter_type="office",
        place_of_service="11",
        intake_text="Synthetic intake.",
        transcript="Synthetic transcript.",
        signed_note="Synthetic signed note.",
        provider_specialty="Family Medicine",
        submitted_em_code=None,
        submitted_icd10=[],
    )


def test_memory_update_without_source_preserves_server_owned_provenance():
    store = MemoryEncounterStore()
    first = store.upsert(_bundle(), source=DEMO_SOURCE)
    replayed = store.upsert(_bundle())

    assert first.source == DEMO_SOURCE
    assert replayed.source == DEMO_SOURCE
    assert store.get("external-id-without-special-prefix").source == DEMO_SOURCE


def test_memory_store_refuses_to_change_or_reclassify_demo_fixture():
    store = MemoryEncounterStore()
    original = _bundle()
    store.upsert(original, source=DEMO_SOURCE)

    with pytest.raises(PersistenceUnavailable):
        store.upsert(replace(original, signed_note="Changed synthetic note."))
    with pytest.raises(PersistenceUnavailable):
        store.upsert(original, source="explicit_import")

    assert store.get(original.encounter_id).bundle == original
    assert store.get(original.encounter_id).source == DEMO_SOURCE


def test_postgres_general_upsert_cannot_claim_reserved_demo_provenance():
    store = object.__new__(PostgresEncounterStore)
    store.context = None

    with pytest.raises(PersistenceUnavailable, match="fixed seeder"):
        store.upsert(_bundle(), source=DEMO_SOURCE)


def test_postgres_bundle_carries_persisted_source_independent_of_external_id():
    visit = _bundle()
    row = {
        "encounter_uid": uuid4(),
        "external_id": visit.encounter_id,
        "patient_age": visit.patient_age,
        "patient_sex": visit.patient_sex,
        "encounter_type": visit.encounter_type,
        "place_of_service": visit.place_of_service,
        "intake_text": visit.intake_text,
        "transcript": visit.transcript,
        "signed_note": visit.signed_note,
        "provider_specialty": visit.provider_specialty,
        "submitted_em_code": visit.submitted_em_code,
        "submitted_icd10": visit.submitted_icd10,
        "version": 1,
        "source": DEMO_SOURCE,
    }

    stored = PostgresEncounterStore._bundle(row)

    assert stored.bundle.encounter_id == "external-id-without-special-prefix"
    assert stored.source == DEMO_SOURCE
    assert "source" in PostgresEncounterStore._select()
