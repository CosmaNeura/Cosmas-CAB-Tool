"""Offline-mode honesty: the mock answers only for the encounter it models, and
every surface says which inference path ran.

Before 2026-08-17 the mock returned the demo encounter's GI-bleed facts for any
input. The evidence verifier stripped the unverifiable quotes downstream, so the
claim came out empty — but the pipeline had been handed invented findings, and a
reviewer saw a blank claim with no explanation. Ten of the eleven bundled
encounters behaved that way.
"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from cosmaneura_billing.api import app
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT
from cosmaneura_billing.llm import (
    AzureOpenAILLM, MockLLM, TerminalLLMError, describe_client,
)
from cosmaneura_billing.orchestrator import BillingOrchestrator

OTHER_ENCOUNTER = (
    "Intake:\nTASK: extract_clinical_facts\n\n"
    "62 y/o female with a three-week history of exertional dyspnea and ankle "
    "oedema. Known heart failure with reduced ejection fraction."
)


# --------------------------------------------------------------------------- #
# The mock
# --------------------------------------------------------------------------- #
def test_mock_answers_for_the_demo_encounter():
    facts = MockLLM().complete_json(
        system="", user=f"TASK: extract_clinical_facts\n\n{SAMPLE_VISIT.intake_text}")
    assert facts.get("problems"), "the mock must still cover the encounter it models"


def test_mock_returns_nothing_for_another_encounter():
    assert MockLLM().complete_json(system="", user=OTHER_ENCOUNTER) == {}


def test_mock_does_not_leak_demo_findings_into_another_encounter():
    """The specific old failure: a heart-failure note came back with GI-bleed facts."""
    out = MockLLM().complete_json(system="", user=OTHER_ENCOUNTER)
    assert "hematochezia" not in repr(out).lower()


def test_generic_tasks_are_not_gated_by_the_encounter_check():
    """`leading_questions` returns generic prompts, not claims about a patient,
    so it is not encounter-specific and must keep working on any input."""
    out = MockLLM().complete_json(system="", user="TASK: leading_questions\n\nanything")
    assert out, "the dialogue-RAG path must not be gated by the encounter guard"


# --------------------------------------------------------------------------- #
# Degradation reporting
# --------------------------------------------------------------------------- #
def test_demo_encounter_is_not_marked_degraded():
    result = BillingOrchestrator().process(SAMPLE_VISIT)
    assert result.degraded is None
    assert result.inference["inferenceMode"] == "mock"


def test_unsupported_encounter_is_marked_degraded_with_a_reason():
    from cosmaneura_billing.corpus.acibench import load_acibench

    bundle = load_acibench("train")[0]
    result = BillingOrchestrator().process(bundle)
    assert result.degraded is not None, (
        "an empty result from the offline model must carry its reason")
    assert result.degraded.reason == "offline_model_unsupported_encounter"
    assert "Azure OpenAI managed-identity" in result.degraded.detail


# --------------------------------------------------------------------------- #
# API surfacing
# --------------------------------------------------------------------------- #
def test_health_reports_the_inference_path():
    body = TestClient(app).get("/api/health").json()
    assert body["inferenceMode"] == "mock"
    assert body["inferenceModel"] == "offline-demo"


def test_coder_view_carries_inference_mode_and_degraded():
    client = TestClient(app)
    ids = [e["id"] for e in client.get("/api/encounters").json()]

    demo = client.get(f"/api/encounters/{SAMPLE_VISIT.encounter_id}/coder-view").json()
    assert demo["inferenceMode"] == "mock"
    assert demo["degraded"] is None
    assert demo["em"]["code"] == "99214"

    other = next(i for i in ids if i != SAMPLE_VISIT.encounter_id)
    view = client.get(f"/api/encounters/{other}/coder-view").json()
    assert view["degraded"]["reason"] == "offline_model_unsupported_encounter"
    assert view["em"] is None and view["dx"] == []
    # and no fabricated diagnoses in the withheld list either
    assert not any(r["code"].startswith("K6") for r in view["withheld"])


# --------------------------------------------------------------------------- #
# The production adapter's terminal stop reasons
# --------------------------------------------------------------------------- #
def test_production_adapter_reports_configured_azure_model():
    client = AzureOpenAILLM(
        endpoint="https://cab-openai.openai.azure.com",
        deployment="cab-gpt",
        model="gpt-4.1-mini",
        client=SimpleNamespace(),
    )
    assert describe_client(client) == {
        "inferenceMode": "azure_openai",
        "inferenceModel": "gpt-4.1-mini",
    }


def test_refusal_is_terminal_not_retried():
    """A refusal returns HTTP 200 with unusable content. Retrying it five times
    and reporting 'no valid response' hides the actual reason."""
    payload = {
        "status": "completed",
        "output_text": "",
        "output": [{
            "type": "message",
            "content": [{
                "type": "refusal",
                "refusal": "private provider refusal text",
            }],
        }],
    }
    try:
        AzureOpenAILLM._extract(payload, {"type": "object"})
    except TerminalLLMError as e:
        assert "declined" in str(e)
        assert "private provider refusal text" not in str(e)
    else:
        raise AssertionError("a refusal must raise TerminalLLMError")


def test_truncation_is_diagnosed_as_truncation():
    """An output-limit response is terminal rather than malformed JSON."""
    payload = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output_text": "{",
    }
    try:
        AzureOpenAILLM._extract(payload, {"type": "object"})
    except TerminalLLMError as e:
        assert "max_output_tokens" in str(e)
    else:
        raise AssertionError("truncation must raise TerminalLLMError")
