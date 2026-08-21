"""The TA4H shadow lane observes but cannot influence billing output."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from threading import Event
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from cosmaneura_billing.api import create_app
from cosmaneura_billing.llm import MockLLM
from cosmaneura_billing.orchestrator import BillingOrchestrator
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion, Disposition, Evidence
from cosmaneura_billing.settings import Settings, SettingsError
from cosmaneura_billing.ta4h import MockTA4H
from cosmaneura_billing.ta4h_shadow import (
    TA4HShadowMetrics,
    is_safe_ta4h_shadow_snapshot,
)
from demo_visit import SAMPLE_VISIT


CLIENT_ID = "11111111-1111-4111-8111-111111111111"
OPENAI_ENDPOINT = "https://cab-openai.openai.azure.com"
LANGUAGE_ENDPOINT = "https://cab-language.cognitiveservices.azure.com"


def _shadow_settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "azure_client_id": CLIENT_ID,
        "azure_openai_enabled": True,
        "azure_openai_endpoint": OPENAI_ENDPOINT,
        "azure_openai_deployment": "gpt-5-6-sol-2026-07-09",
        "azure_openai_model": "gpt-5.6-sol",
        "azure_language_shadow_enabled": True,
        "azure_language_endpoint": LANGUAGE_ENDPOINT,
        "cpt_auto_fetch": False,
    }
    values.update(overrides)
    return Settings(**values)


def _baseline(settings: Settings):
    return BillingOrchestrator(
        settings=replace(settings, azure_language_shadow_enabled=False),
        llm=MockLLM(),
        ta4h=None,
    ).process(SAMPLE_VISIT)


def test_shadow_result_is_byte_equivalent_to_primary_only_result():
    settings = _shadow_settings()
    expected = _baseline(settings)
    orchestrator = BillingOrchestrator(
        settings=settings, llm=MockLLM(), ta4h=MockTA4H(),
    )

    observed = orchestrator.process(SAMPLE_VISIT)

    assert asdict(observed) == asdict(expected)
    assert orchestrator.wait_for_shadow(1.0)
    snapshot = orchestrator.ta4h_observability()
    assert snapshot["mode"] == "shadow"
    assert snapshot["status"] == "healthy"
    assert snapshot["runs"] == snapshot["succeeded"] == 1
    assert snapshot["failed"] == 0
    assert snapshot["pending"] == snapshot["dropped"] == 0
    assert is_safe_ta4h_shadow_snapshot(snapshot)
    orchestrator.shutdown()


def test_hostile_secondary_output_cannot_reach_any_result_field():
    settings = _shadow_settings()
    expected = _baseline(settings)

    class HostileSecondary:
        def run(self, bundle):
            del bundle
            return (
                {
                    "chief_complaint": "SHADOW MUST NOT REPLACE PRIMARY",
                    "problems": [{"name": "invented"}],
                    "ta4h_contradictions": {"K62.5": "invented veto"},
                    "ta4h_entity_count": 9,
                },
                [CodeSuggestion(
                    kind=CodeKind.ICD10,
                    code="Z99.99",
                    label="shadow-only canary",
                    disposition=Disposition.CAPTURED,
                    evidence=[Evidence("signed_note", "Electronically signed")],
                    rationale="shadow-only rationale",
                    notes=["shadow-only note"],
                )],
            )

    orchestrator = BillingOrchestrator(
        settings=settings,
        llm=MockLLM(),
        secondary_coding=HostileSecondary(),
    )
    observed = orchestrator.process(SAMPLE_VISIT)

    assert asdict(observed) == asdict(expected)
    assert "shadow" not in repr(asdict(observed)).casefold()
    assert orchestrator.wait_for_shadow(1.0)
    orchestrator.shutdown()


def test_secondary_mutation_cannot_race_or_change_primary_bundle_output():
    settings = _shadow_settings()
    bundle = deepcopy(SAMPLE_VISIT)
    original_bundle = asdict(bundle)
    expected = _baseline(settings)
    mutated = Event()

    class MutatingSecondary:
        def run(self, shadow_bundle):
            shadow_bundle.encounter_id = "MUTATED-BY-SECONDARY"
            shadow_bundle.signed_note = "malicious replacement note"
            shadow_bundle.submitted_icd10.clear()
            shadow_bundle.submitted_icd10.append("Z99.99")
            mutated.set()
            return {"ta4h_contradictions": {}, "ta4h_entity_count": 0}, []

    orchestrator = BillingOrchestrator(
        settings=settings,
        llm=MockLLM(),
        secondary_coding=MutatingSecondary(),
    )
    real_procedures = orchestrator.procedures

    class RaceBarrierProcedures:
        def propose(self, primary_bundle, *, has_em):
            assert mutated.wait(1.0), "secondary did not reach the mutation barrier"
            return real_procedures.propose(primary_bundle, has_em=has_em)

    # The first downstream consumer pauses until the background mutation has
    # definitely happened. Without a deep task copy this makes the race fully
    # deterministic: later evidence and response assembly read mutated data.
    orchestrator.procedures = RaceBarrierProcedures()

    observed = orchestrator.process(bundle)

    assert orchestrator.wait_for_shadow(1.0)
    assert asdict(observed) == asdict(expected)
    assert asdict(bundle) == original_bundle
    orchestrator.shutdown()


def test_shadow_runtime_failure_degrades_only_aggregate_health():
    settings = _shadow_settings()
    expected = _baseline(settings)
    canary = "Jane Doe MRN-123 secret-token"

    class BrokenSecondary:
        def run(self, bundle):
            del bundle
            raise RuntimeError(canary)

    orchestrator = BillingOrchestrator(
        settings=settings,
        llm=MockLLM(),
        secondary_coding=BrokenSecondary(),
    )
    observed = orchestrator.process(SAMPLE_VISIT)

    assert asdict(observed) == asdict(expected)
    assert orchestrator.wait_for_shadow(1.0)
    snapshot = orchestrator.ta4h_observability()
    assert snapshot["status"] == "degraded"
    assert snapshot["runs"] == snapshot["failed"] == 1
    assert snapshot["succeeded"] == 0
    assert canary not in repr(snapshot)
    orchestrator.shutdown()


def test_blocking_secondary_does_not_delay_or_change_primary_response():
    settings = _shadow_settings()
    expected = _baseline(settings)
    entered = Event()
    release = Event()

    class BlockingSecondary:
        def run(self, bundle):
            del bundle
            entered.set()
            release.wait(5.0)
            return {"ta4h_contradictions": {}, "ta4h_entity_count": 0}, []

    orchestrator = BillingOrchestrator(
        settings=settings,
        llm=MockLLM(),
        secondary_coding=BlockingSecondary(),
    )
    started = perf_counter()
    observed = orchestrator.process(SAMPLE_VISIT)
    elapsed = perf_counter() - started

    # The secondary blocks for five seconds; the primary path must return well
    # before that even on a loaded CI worker.
    assert elapsed < 1.0
    assert asdict(observed) == asdict(expected)
    assert entered.wait(1.0)
    snapshot = orchestrator.ta4h_observability()
    assert snapshot["pending"] == 1
    shutdown_started = perf_counter()
    orchestrator.shutdown()
    assert perf_counter() - shutdown_started < 0.2
    release.set()
    assert orchestrator.wait_for_shadow(1.0)


def test_saturated_shadow_queue_drops_work_without_blocking_requests():
    settings = _shadow_settings()
    release = Event()

    class BlockingSecondary:
        def run(self, bundle):
            del bundle
            release.wait(5.0)
            return {"ta4h_contradictions": {}, "ta4h_entity_count": 0}, []

    orchestrator = BillingOrchestrator(
        settings=settings,
        llm=MockLLM(),
        secondary_coding=BlockingSecondary(),
    )
    started = perf_counter()
    for _ in range(3):
        orchestrator.process(SAMPLE_VISIT)
    elapsed = perf_counter() - started

    snapshot = orchestrator.ta4h_observability()
    # Three synchronous shadow calls would take fifteen seconds.
    assert elapsed < 2.0
    assert snapshot["runs"] == 3
    assert snapshot["pending"] == 2
    assert snapshot["dropped"] == 1
    assert snapshot["status"] == "degraded"
    release.set()
    assert orchestrator.wait_for_shadow(2.0)
    orchestrator.shutdown()


def test_shadow_configuration_fails_closed():
    with pytest.raises(SettingsError, match="primary Azure OpenAI"):
        Settings(
            azure_language_shadow_enabled=True,
            azure_language_endpoint=LANGUAGE_ENDPOINT,
            azure_client_id=CLIENT_ID,
        )
    with pytest.raises(SettingsError, match="mutually exclusive"):
        _shadow_settings(azure_language_enabled=True)
    with pytest.raises(SettingsError, match="AZURE_LANGUAGE_ENDPOINT"):
        _shadow_settings(azure_language_endpoint="")


def test_shadow_env_parser_and_boolean_type_are_strict():
    env = {
        "COSMANEURA_ENV": "test",
        "AZURE_CLIENT_ID": CLIENT_ID,
        "COSMANEURA_AZURE_OPENAI_ENABLED": "true",
        "AZURE_OPENAI_ENDPOINT": OPENAI_ENDPOINT,
        "AZURE_OPENAI_DEPLOYMENT": "gpt-5-6-sol-2026-07-09",
        "AZURE_OPENAI_MODEL": "gpt-5.6-sol",
        "COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED": "true",
        "AZURE_LANGUAGE_ENDPOINT": LANGUAGE_ENDPOINT,
    }
    assert Settings.from_env(env).azure_language_shadow_enabled is True
    secret = "shadow-direct-credential-canary"
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({**env, "AZURE_LANGUAGE_KEY": secret})
    assert secret not in str(caught.value)
    with pytest.raises(SettingsError, match="must be a boolean"):
        Settings(azure_language_shadow_enabled="true")


def test_observability_schema_rejects_free_form_or_expanded_data():
    safe = TA4HShadowMetrics("shadow").snapshot()
    assert is_safe_ta4h_shadow_snapshot(safe)
    assert not is_safe_ta4h_shadow_snapshot({**safe, "encounterId": "patient-1"})
    assert not is_safe_ta4h_shadow_snapshot({**safe, "status": "patient text"})


def test_active_consensus_remains_a_separate_explicit_mode():
    orchestrator = BillingOrchestrator(ta4h=MockTA4H())
    assert orchestrator.ta4h_mode == "active_consensus"
    result = orchestrator.process(SAMPLE_VISIT)
    assert result.biller_worksheet.audit_trail[0].startswith(
        "CONSENSUS two extraction engines ran"
    )


def test_health_exposes_only_the_closed_aggregate_shadow_schema(tmp_path):
    settings = _shadow_settings()
    orchestrator = BillingOrchestrator(
        settings=settings, llm=MockLLM(), ta4h=MockTA4H(),
    )
    orchestrator.process(SAMPLE_VISIT)
    assert orchestrator.wait_for_shadow(1.0)
    app = create_app(
        settings=settings,
        frontend_dist=tmp_path / "missing-frontend",
    )
    app.state.orchestrator = orchestrator

    payload = TestClient(app).get("/api/health").json()["ta4h"]

    assert payload == orchestrator.ta4h_observability()
    assert is_safe_ta4h_shadow_snapshot(payload)
    orchestrator.shutdown()
