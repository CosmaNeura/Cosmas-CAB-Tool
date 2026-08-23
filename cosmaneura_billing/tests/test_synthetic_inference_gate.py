"""The public-demo model latch trusts stored server-owned provenance only."""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import cosmaneura_billing.api as api
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import (
    BillingOrchestrator,
    InferenceInputPolicyError,
)
from cosmaneura_billing.persistence import (
    ATHENA_PREVIEW_SYNTHETIC_SOURCE,
    MemoryEncounterStore,
    SYNTHETIC_DEMO_SOURCE,
)
from cosmaneura_billing.settings import Settings


_CLIENT_ID = "33333333-3333-4333-8333-333333333333"


def _gated_settings() -> Settings:
    return Settings(
        azure_client_id=_CLIENT_ID,
        azure_openai_enabled=True,
        azure_openai_endpoint="https://cab-openai.openai.azure.com",
        azure_openai_deployment="cab-gpt",
        azure_openai_model="gpt-5.6-sol",
        azure_openai_synthetic_only=True,
    )


class CountingMockLLM(MockLLM):
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, *args, **kwargs):
        self.calls += 1
        return super().complete_json(*args, **kwargs)


def test_orchestrator_gate_requires_a_server_owned_synthetic_provenance():
    llm = CountingMockLLM()
    orchestrator = BillingOrchestrator(llm=llm, settings=_gated_settings())

    with pytest.raises(InferenceInputPolicyError, match="stored synthetic"):
        orchestrator.process(SAMPLE_VISIT)
    with pytest.raises(InferenceInputPolicyError, match="stored synthetic"):
        orchestrator.process(SAMPLE_VISIT, input_provenance="explicit_import")

    assert llm.calls == 0
    for provenance in (
        SYNTHETIC_DEMO_SOURCE,
        ATHENA_PREVIEW_SYNTHETIC_SOURCE,
    ):
        result = orchestrator.process(
            SAMPLE_VISIT,
            input_provenance=provenance,
        )
        assert result.inference["inferenceMode"] == "mock"
    assert llm.calls > 0


def test_api_gate_rejects_adhoc_and_non_synthetic_rows_before_model_use(
    monkeypatch, tmp_path,
):
    settings = _gated_settings()
    store = MemoryEncounterStore()
    store.upsert(SAMPLE_VISIT, source=SYNTHETIC_DEMO_SOURCE)
    misleading = replace(
        SAMPLE_VISIT,
        encounter_id="SYN-RC-MISLEADING-ID",
        signed_note="Operational documentation with patient-canary-729.",
    )
    store.upsert(misleading, source="explicit_import")

    llm = CountingMockLLM()
    orchestrator = BillingOrchestrator(llm=llm, settings=settings)
    monkeypatch.setattr(
        api,
        "get_orchestrator",
        lambda _settings=None, *, feedback_store=None: orchestrator,
    )
    client = TestClient(api.create_app(
        settings=settings,
        encounter_store=store,
        frontend_dist=tmp_path / "missing-frontend",
    ))

    for route in (
        "/api/encounters/billing",
        "/api/encounters/coder-view",
        "/api/encounters/billing-tab",
    ):
        response = client.post(
            route, json={"encounterId": misleading.encounter_id},
        )
        assert response.status_code == 403
        assert response.json() == {
            "detail": api._SYNTHETIC_INFERENCE_ONLY_DETAIL,
        }
        assert "patient-canary" not in response.text

    adhoc = client.post("/api/billing", json={
        "encounterId": "SYN-RC-CALLER-CLAIM",
        "signedNote": "Ad-hoc patient-canary-729 documentation.",
    })
    assert adhoc.status_code == 403
    assert adhoc.json() == {"detail": api._SYNTHETIC_INFERENCE_ONLY_DETAIL}
    assert "patient-canary" not in adhoc.text
    assert llm.calls == 0

    allowed = client.post(
        "/api/encounters/billing",
        json={"encounterId": SAMPLE_VISIT.encounter_id},
    )
    assert allowed.status_code == 200
    assert allowed.json()["dataClass"] == SYNTHETIC_DEMO_SOURCE
    assert llm.calls > 0

    health = client.get("/api/health").json()
    assert health["inferenceInputPolicy"] == "reserved_synthetic_only"
    assert health["multiEngineEnabled"] is False
