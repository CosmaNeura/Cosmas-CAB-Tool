"""Provider handoff context reaches the authorized header detail without inference."""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from cosmaneura_billing.api import create_app
from cosmaneura_billing.billing_header import source_snapshot
from cosmaneura_billing.persistence import MemoryEncounterStore
from cosmaneura_billing.settings import Settings
from provider_admission_support import register_route
from test_provider_handoff import BASE, envelope


@pytest.mark.parametrize("service_date,status", [(None, "completed"), ("2026-09-24", "signed")])
def test_detail_exposes_exact_provider_context_and_preserves_replay(tmp_path, monkeypatch, service_date, status):
    encounters = MemoryEncounterStore(practice_id="provider-context-api")
    app = create_app(settings=Settings(environment="test", practice_id=encounters.practice_id,
        development_roles=("Provider",), billing_header_enabled=True, provider_handoff_enabled=True),
        encounter_store=encounters, frontend_dist=tmp_path / "absent")
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.orchestrator, "process", lambda *a, **k: pytest.fail("context must not infer"))
        payload = envelope()
        payload["completion"]["status"] = status
        if service_date is not None:
            payload["visit"]["serviceDate"] = service_date
        original = deepcopy(payload)
        register_route(client, payload)
        accepted = client.post(BASE, json=payload)
        assert accepted.status_code == 200, accepted.text
        receipt = accepted.json()["receipt"]
        stored = encounters.get(receipt["encounterId"])
        pin = source_snapshot(stored)
        detail = client.post("/api/encounters/detail", json={"encounterId": receipt["encounterId"]})
        assert detail.status_code == 200, detail.text
        assert detail.json()["sourceVisitContext"] == {
            "schemaVersion": "cab.source-visit-context.v1", "expectedSource": pin,
            "origin": "provider_manifest", "sourceSha256": receipt["sourceManifestSha256"],
            "serviceDate": service_date, "noteStatus": status, "statusMeaning": "provider_attested",
        }
        assert detail.json()["headerSource"] == pin == receipt["headerSource"]
        assert "serviceDate" not in pin and "noteStatus" not in pin
        assert stored.bundle.signed_note == original["note"]["text"]
        replay = client.post(BASE, json=payload)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["receipt"] == receipt
        assert payload == original
